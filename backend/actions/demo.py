"""Explicitly fictional actions for Phase 0; no database or external sends."""

from .registry import Action, register


def send_reminder(customer, text):
    return {"demo": True, "simulated": True, "sent_externally": False,
            "customer": customer, "text": text,
            "message": "Fake reminder simulated; no message was sent."}


def register_demo_actions():
    register(Action(
        name="send_reminder",
        description="FAKE demo: simulate a reminder after owner confirmation; nothing is sent externally.",
        parameters={"type": "object", "properties": {
            "customer": {"type": "string"}, "text": {"type": "string"}},
            "required": ["customer", "text"], "additionalProperties": False},
        level="yellow", handler=send_reminder,
    ))
