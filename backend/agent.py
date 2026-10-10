"""Route model tool requests through the server's permission registry."""

import json
import logging

from backend.actions import registry
from backend.llm import request_action_turn


MAX_ACTION_CALLS = 5
MAX_CONVERSATION_CHARS = 24000
MAX_RESULT_CHARS = 8000
logger = logging.getLogger(__name__)

ACTION_LABELS = {
    "get_receivables": "Looked up unpaid invoices",
    "send_reminder": "Payment reminder",
}


def check_context(messages):
    if len(json.dumps(messages, ensure_ascii=False)) > MAX_CONVERSATION_CHARS:
        raise ValueError("Conversation is too large. Start a new chat.")


def tool_catalog():
    tools = []
    for action in registry.list_actions():
        description = action["description"]
        schema = action["parameters"]
        if schema.get("properties") == {} and schema.get("additionalProperties") is False:
            description += " This action takes no arguments. Call it with an empty object: {}."
        tools.append({"type": "function", "function": {
            "name": action["name"], "description": description, "parameters": schema,
        }})
    return tools


def parse_call(call):
    if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
        raise ValueError("Invalid action request.")
    function = call["function"]
    name = function.get("name")
    arguments = function.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, dict):
        raise ValueError("Invalid action name or arguments.")
    return name, arguments


async def stream_agent(messages, session_id):
    conversation = [dict(message) for message in messages]
    tools = tool_catalog()
    calls_used = 0

    while True:
        check_context(conversation)
        reply = await request_action_turn(conversation, tools)
        calls = reply.get("tool_calls") or []
        if not isinstance(calls, list):
            raise ValueError("Invalid tool-call list.")
        if not calls:
            # This round already contains the answer. Do not generate it again.
            content = reply.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("The model returned an empty answer.")
            yield {"type": "chunk", "content": content}
            yield {"type": "done"}
            return

        parsed = [parse_call(call) for call in calls]
        conversation.append({"role": "assistant", "content": "", "tool_calls": calls})
        for name, arguments in parsed:
            if calls_used >= MAX_ACTION_CALLS:
                yield {"type": "error", "message": "Stopped at five action calls. No further action ran."}
                return
            calls_used += 1
            result = registry.execute(name, arguments, session_id)
            status = result["status"]
            label = ACTION_LABELS.get(name, name.replace("_", " ").capitalize())
            if status != "ok":
                label = f"{name.replace('_', ' ')}: {status.replace('_', ' ')}"
            if status in {"error", "refused"}:
                detail = result.get("message") or result.get("reason")
                if isinstance(detail, str):
                    label += f" — {detail}"
                # Log names only; avoid logging reminder text or financial values.
                logger.warning("Action %s returned %s; argument fields: %s",
                               name, status, sorted(arguments))
            yield {"type": "action", "name": name, "status": status, "label": label}

            if status == "needs_confirmation":
                yield {"type": "confirmation", "confirmation_id": result["confirmation_id"],
                       "summary": result["summary"]}
                yield {"type": "chunk", "content": "This action is waiting for owner confirmation; it has not run. Approval buttons will be added in the next phase."}
                yield {"type": "done"}
                return

            content = json.dumps(result, ensure_ascii=False, allow_nan=False)
            if len(content) > MAX_RESULT_CHARS:
                yield {"type": "error", "message": "The action result is too large to explain safely."}
                return
            conversation.append({"role": "tool", "tool_name": name, "content": content})
