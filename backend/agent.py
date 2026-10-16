"""Route model tool requests through the server's permission registry."""

import json
import logging
import re

from backend.actions import registry
from backend.llm import request_action_turn


MAX_ACTION_CALLS = 5
MAX_CONVERSATION_CHARS = 24000
MAX_RESULT_CHARS = 8000
logger = logging.getLogger(__name__)

ACTION_LABELS = {
    "forecast_cash": "Calculated the cash forecast",
    "get_receivables": "Looked up unpaid invoices",
    "send_reminder": "Payment reminder",
    "get_customer": "Looked up the customer",
    "get_balance": "Checked recorded cash",
    "get_upcoming_outflows": "Checked upcoming payments",
    "compare_options": "Compared the cost of funding options",
    "preview_funding_plan": "Checked the cash impact of a funding plan",
    "draft_reminder": "Drafted a payment reminder",
    "prepare_credit_request": "Credit request draft",
    "save_plan": "Cash plan",
    "set_alert": "Cash alert",
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


def purchase_needs_date(messages):
    """Don't let a vague next-month purchase become a dated forecast."""
    text = messages[-1]['content'].lower()
    purchase = any(word in text for word in ('stock order', 'purchase', 'buy ', 'extra expense'))
    vague_date = 'next month' in text or "haven't chosen the payment date" in text
    dated = re.search(r'\b\d{4}-\d{2}-\d{2}\b|\bon (?:the )?\d{1,2}(?:st|nd|rd|th)?\b', text)
    # Only this clear missing-date case is handled here; other requests go to Bill.
    return purchase and vague_date and not dated


async def stream_agent(messages, session_id):
    if purchase_needs_date(messages):
        yield {"type": "chunk", "content": "What date will you pay for that purchase? I need it to include the expense in the cash forecast."}
        yield {"type": "done"}
        return
    conversation = [dict(message) for message in messages]
    tools = tool_catalog()
    calls_used = 0
    forecast = None
    compared_forecast = False
    pending_call = None
    window_corrected = False
    request_text = messages[-1]['content'].lower()
    wants_options = any(phrase in request_text for phrase in (
        'options', 'ways to cover', 'should i borrow', 'need the overdraft', 'need an overdraft'))
    general_shortage = any(phrase in request_text for phrase in (
        "i'm short", 'i am short', 'short of cash', 'cash shortage', 'cash shortfall'))
    if wants_options and general_shortage and not re.search(r'\d', request_text):
        # For an unspecified cash shortage, read the default forecast first.
        # Asking the owner to guess the gap would defeat the purpose of Bill.
        # This only schedules a Green read through the normal registry/limit.
        pending_call = {'function': {'name': 'forecast_cash', 'arguments': {'days': 30}}}

    while True:
        check_context(conversation)
        if pending_call:
            reply = {'tool_calls': [pending_call]}
            pending_call = None
        else:
            reply = await request_action_turn(conversation, tools)
        calls = reply.get("tool_calls") or []
        if not isinstance(calls, list):
            raise ValueError("Invalid tool-call list.")
        if not calls and forecast and wants_options and not compared_forecast:
            cautious = forecast.get('cautious', {})
            amount = cautious.get('shortfall', 0)
            days = cautious.get('days_below_zero', 0)
            if amount > 0 and days > 0:
                # A requested comparison must use the calculated gap. This is
                # still a Green registry call, counted against the same limit.
                calls = [{'function': {'name': 'compare_options',
                          'arguments': {'amount': amount, 'days': days}}}]
        if not calls:
            if 'preview' in request_text and any(word in request_text for word in ('loan', 'discount', 'funding')):
                # A fluent explanation is not evidence of a calculated preview.
                # Successful previews return their formatted result below.
                yield {'type': 'chunk', 'content': 'I have not calculated that funding preview yet. Please specify discount only, discount plus overdraft, or discount plus term loan, along with the amount, days and any quoted rate.'}
                yield {'type': 'done'}
                return
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
            if status == 'ok' and name == 'forecast_cash':
                forecast = result['result']
                compared_forecast = False
            elif name == 'compare_options':
                # Do not retry an error or refusal automatically.
                compared_forecast = True
            if name == 'forecast_cash' and status == 'error' and not window_corrected \
                    and 'through' in request_text:
                needed = re.search(r'A forecast through that date needs (\d+) days;',
                                   result.get('message', ''))
                if needed and 1 <= int(needed[1]) <= 60:
                    # The owner requested coverage through this date. Repair
                    # only the window of this read-only call, once.
                    pending_call = {'function': {'name': name, 'arguments':
                                    {**arguments, 'days': int(needed[1])}}}
                    window_corrected = True
            label = ACTION_LABELS.get(name, name.replace("_", " ").capitalize())
            if status != "ok":
                label = f"{name.replace('_', ' ')}: {status.replace('_', ' ')}"
            if status in {"error", "refused"}:
                detail = result.get("message") or result.get("reason")
                if isinstance(detail, str):
                    label += f" — {detail}"
                # Log names only; avoid logging reminder text or financial values.
                logger.warning("Action %r returned %s; argument fields: %s",
                               name, status, sorted(arguments))
            yield {"type": "action", "name": name, "status": status, "label": label}

            if status == "needs_confirmation":
                yield {"type": "confirmation", "confirmation_id": result["confirmation_id"],
                       "summary": result["summary"]}
                yield {"type": "chunk", "content": "This action has not run. Choose Yes to approve it or No to decline."}
                yield {"type": "done"}
                return

            content = json.dumps(result, ensure_ascii=False, allow_nan=False)
            if len(content) > MAX_RESULT_CHARS:
                yield {"type": "error", "message": "The action result is too large to explain safely."}
                return
            conversation.append({"role": "tool", "tool_name": name, "content": content})
            if status == 'ok' and len(parsed) == 1:
                # Preserve exact financial meanings: principal is money borrowed,
                # while interest/fees are its cost. The model chooses the action;
                # these calculated comparisons use a fixed readable presentation.
                from backend.funding_answers import comparison_answer, preview_answer
                value = result['result']
                if name == 'compare_options' and 'collection_plans' in value \
                        and not any(word in request_text for word in ('preview', 'cash impact')):
                    yield {'type': 'chunk', 'content': comparison_answer(value)}
                    yield {'type': 'done'}
                    return
                if name == 'preview_funding_plan' and 'with_plan' in value:
                    yield {'type': 'chunk', 'content': preview_answer(value)}
                    yield {'type': 'done'}
                    return
