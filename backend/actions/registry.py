"""The single execution boundary between model requests and actions.

Action: name, description, parameters (JSON Schema), level, handler(**arguments).
register(action) adds an action; list_actions() returns serialisable descriptions.
execute(name, arguments, session_id) and
confirm(confirmation_id, approved, session_id) return one of:
  {"status": "ok", "result": dict}
  {"status": "needs_confirmation", "confirmation_id": str, "summary": str}
  {"status": "refused", "reason": str}
  {"status": "error", "message": str}

Phase 0: Green runs, Yellow is stored in memory pending confirmation, Red never
runs. Confirmation belongs to one session and is consumed before the handler
starts. Only trusted server code should call confirm; no action exposes it.

Not yet implemented: authentication, schema enforcement, expiry, persistence,
audit logging and session locks. The catalog is process-local; restart loses
pending confirmations. A session ID is a scope marker, not proof of identity.
"""

from copy import deepcopy
from dataclasses import dataclass
import inspect
import json
from threading import RLock
from typing import Callable, Literal
from uuid import uuid4


@dataclass(frozen=True)
class Action:
    name: str
    description: str
    parameters: dict
    level: Literal["green", "yellow", "red"]
    handler: Callable[..., dict]


@dataclass(frozen=True)
class PendingCall:
    action: Action
    arguments: dict
    session_id: str


_actions: dict[str, Action] = {}
_pending: dict[str, PendingCall] = {}
_lock = RLock()


def register(action: Action) -> None:
    if not isinstance(action.name, str) or not action.name.strip():
        raise ValueError("An action needs a nonempty name.")
    if action.level not in {"green", "yellow", "red"}:
        raise ValueError("Unknown permission level.")
    if not callable(action.handler):
        raise ValueError("The handler must be callable.")
    if not isinstance(action.parameters, dict):
        raise ValueError("Parameters must be a JSON Schema dictionary.")
    json.dumps(action.parameters, allow_nan=False)
    with _lock:
        if action.name in _actions:
            raise ValueError(f"Action already registered: {action.name}")
        # Detach the stored schema from the caller's mutable dictionary.
        _actions[action.name] = Action(action.name, action.description,
            deepcopy(action.parameters), action.level, action.handler)


def list_actions() -> list[dict]:
    with _lock:
        return [{"name": a.name, "description": a.description,
                 "parameters": deepcopy(a.parameters), "level": a.level}
                for a in _actions.values()]


def _run(action: Action, arguments: dict) -> dict:
    try:
        result = action.handler(**arguments)
        if not isinstance(result, dict):
            raise TypeError("An action must return a dictionary.")
        json.dumps(result, allow_nan=False)
        return {"status": "ok", "result": result}
    except Exception:
        # Avoid exposing internal exception details to the model/client.
        return {"status": "error", "message": "Action execution failed."}


def execute(name, arguments, session_id) -> dict:
    if not isinstance(session_id, str) or not session_id.strip():
        return {"status": "error", "message": "A session ID is required."}
    if not isinstance(name, str):
        return {"status": "error", "message": "Unknown action."}
    with _lock:
        action = _actions.get(name)
    if action is None:
        return {"status": "error", "message": "Unknown action."}
    if action.level == "red":
        return {"status": "refused", "reason": "This action is prohibited."}
    if not isinstance(arguments, dict) or not all(isinstance(k, str) for k in arguments):
        return {"status": "error", "message": "Arguments must be a JSON object."}
    try:
        # Basic JSON and signature checks; full schema/type validation is A3.
        json.dumps(arguments, allow_nan=False)
        inspect.signature(action.handler).bind(**arguments)
        stored_arguments = deepcopy(arguments)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return {"status": "error", "message": "Invalid action arguments."}
    if action.level == "green":
        return _run(action, stored_arguments)

    confirmation_id = str(uuid4())
    with _lock:
        _pending[confirmation_id] = PendingCall(action, stored_arguments, session_id)
    return {"status": "needs_confirmation", "confirmation_id": confirmation_id,
            "summary": f"Approve {action.name} with these arguments: "
                       + json.dumps(stored_arguments, ensure_ascii=False, allow_nan=False)}


def confirm(confirmation_id, approved, session_id) -> dict:
    if not isinstance(approved, bool):
        return {"status": "error", "message": "Approval must be true or false."}
    if not isinstance(confirmation_id, str) or not isinstance(session_id, str):
        return {"status": "error", "message": "Invalid confirmation request."}
    with _lock:
        pending = _pending.get(confirmation_id)
        if pending is None or pending.session_id != session_id:
            return {"status": "error", "message": "Confirmation unavailable for this session."}
        # Atomic consumption makes duplicate/concurrent approvals at-most-once.
        del _pending[confirmation_id]
    if not approved:
        return {"status": "refused", "reason": "The owner declined this action."}
    return _run(pending.action, pending.arguments)
