"""Explicitly fictional actions for Phase 0; no database or external sends."""

from .registry import Action, register


def get_receivables():
    return {"demo": True, "currency": "GEL", "snapshot_date": "2026-10-03",
            "invoice_count": 2, "total_overdue": 1500.0,
            "invoices": [
                {"invoice_id": "FAKE-001", "customer": "Demo Cafe",
                 "amount": 500.0, "due_date": "2026-09-20"},
                {"invoice_id": "FAKE-002", "customer": "Demo Hotel",
                 "amount": 1000.0, "due_date": "2026-09-25"},
            ]}


def send_reminder(customer, text):
    return {"demo": True, "simulated": True, "sent_externally": False,
            "customer": customer, "text": text,
            "message": "Fake reminder simulated; no message was sent."}


def register_demo_actions():
    register(Action(
        name="get_receivables",
        description="FAKE demo: get two fictional overdue invoices and their total; not real company data.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        level="green", handler=get_receivables,
    ))
    register(Action(
        name="send_reminder",
        description="FAKE demo: simulate a reminder after owner confirmation; nothing is sent externally.",
        parameters={"type": "object", "properties": {
            "customer": {"type": "string"}, "text": {"type": "string"}},
            "required": ["customer", "text"], "additionalProperties": False},
        level="yellow", handler=send_reminder,
    ))
