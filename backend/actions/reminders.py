"""Payment reminders: a draft the model can read, and a send the owner approves.

draft_reminder (green) fills a fixed template from the books: the overdue
invoices and their total. No figure in it comes from the model.

send_reminder (yellow) runs only after the owner presses Yes. Before the owner
is asked, the request is pinned down: the customer becomes the one ID it
names, and with no text given, the standard draft is written in. The owner is
shown the customer as the books name it, all of the text, and whether the text
is the standard one. Nothing leaves the database: a sent reminder is a row in
the outbox table.

register_reminder_actions() puts both in the registry. The server calls it
once at start-up.
"""

import sqlite3
from datetime import date
from typing import Any

from backend.bank.db import connect, today

from .lookups import TOP_ROWS, find_customer, gel, money, no_single_customer, open_invoices
from .registry import Action, ActionError, quoted, register

CANDIDATES_NAMED = 5

TABLES = """
CREATE TABLE IF NOT EXISTS outbox (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_on TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    text TEXT NOT NULL
);
"""


def reminder_draft(conn: sqlite3.Connection, customer: sqlite3.Row, as_of: date) -> dict[str, Any] | None:
    """The standard reminder for one customer's overdue invoices, or None when
    nothing is overdue."""
    overdue = [invoice for invoice in open_invoices(conn, as_of, customer["customer_id"])
               if invoice["days_late"] > 0]
    if not overdue:
        return None
    overdue.sort(key=lambda invoice: (invoice["due_date"], invoice["invoice_id"]))
    company = conn.execute("SELECT name FROM company").fetchone()["name"]
    total = money(sum(invoice["amount"] for invoice in overdue))

    lines = [
        f"- {invoice['invoice_id']}, due {invoice['due_date']}, {gel(invoice['amount'])}, "
        f"{invoice['days_late']} {'day' if invoice['days_late'] == 1 else 'days'} overdue"
        for invoice in overdue[:TOP_ROWS]
    ]
    if len(overdue) > TOP_ROWS:
        lines.append(f"- and {len(overdue) - TOP_ROWS} more")
    count = "1 invoice is" if len(overdue) == 1 else f"{len(overdue)} invoices are"
    text = "\n".join([
        f"Dear {customer['name']} team,",
        "",
        f"This is a friendly reminder that {count} past the due date:",
        "",
        *lines,
        "",
        f"Total overdue: {gel(total)}",
        "",
        "If you have already paid, please ignore this message. Otherwise we "
        "would be grateful if you could pay in the next few days.",
        "",
        "Thank you,",
        company,
    ])
    return {
        "found": True,
        "customer": customer["name"],
        "customer_id": customer["customer_id"],
        "total_overdue": total,
        "overdue_invoices": len(overdue),
        "text": text,
    }


def draft_reminder(customer: str) -> dict[str, Any]:
    with connect() as conn:
        match, candidates = find_customer(conn, customer)
        if match is None:
            return no_single_customer(customer, candidates)
        draft = reminder_draft(conn, match, today())
    if draft is None:
        return {
            "found": True,
            "customer": match["name"],
            "customer_id": match["customer_id"],
            "total_overdue": 0.0,
            "overdue_invoices": 0,
            "message": f"{match['name']} has no overdue invoices, so no reminder is needed.",
        }
    return draft


# ------------------------------------------------------------------- sending one
def one_customer(conn: sqlite3.Connection, customer: str) -> sqlite3.Row:
    """The customer a request names, or an ActionError the model can act on.
    The messages name customers from the books, never the text that was sent."""
    match, candidates = find_customer(conn, customer)
    if match is not None:
        return match
    if candidates:
        names = ", ".join(candidate["name"] for candidate in candidates[:CANDIDATES_NAMED])
        raise ActionError(f"That name could be several customers: {names}. "
                          "Ask the owner which one they mean.")
    raise ActionError("No customer matches that name. Ask the owner for the name.")


def prepare_reminder(customer: str, text: str | None = None) -> dict[str, Any]:
    with connect() as conn:
        match = one_customer(conn, customer)
        draft = reminder_draft(conn, match, today())
    if draft is None:
        raise ActionError(f"{match['name']} has no overdue invoices, so there is "
                          "nothing to remind them about.")
    written = (text or "").strip()
    return {"customer": match["customer_id"], "text": written or draft["text"]}


STANDARD_TEXT = "This is the standard text, filled in from your books."
OTHER_TEXT = "This is not the standard text. Read it before you approve."


def describe_reminder(customer: str, text: str) -> str:
    with connect() as conn:
        match = one_customer(conn, customer)
        draft = reminder_draft(conn, match, today())
    # The name and ID are the books' own. The text may be the model's, with
    # figures of its own, so the owner is told when it is not the template's.
    # All of it is shown, marked as a message.
    standard = draft is not None and text == draft["text"]
    return (f"Send this payment reminder to {match['name']} ({match['customer_id']})?\n"
            f"{STANDARD_TEXT if standard else OTHER_TEXT}\n\n{quoted(text)}")


def send_reminder(customer: str, text: str) -> dict[str, Any]:
    with connect() as conn:
        conn.executescript(TABLES)
        match = one_customer(conn, customer)
        with conn:
            message_id = conn.execute(
                "INSERT INTO outbox (created_on, customer_id, customer_name, text) "
                "VALUES (?, ?, ?, ?)",
                (today().isoformat(), match["customer_id"], match["name"], text),
            ).lastrowid
    return {
        "saved_to_outbox": True,
        "sent_externally": False,
        "customer": match["name"],
        "customer_id": match["customer_id"],
        "message_id": message_id,
        "message": "The reminder is saved in the outbox. This demo sends nothing outside the app.",
    }


# ------------------------------------------------------------------ registration
def register_reminder_actions() -> None:
    register(Action(
        name="draft_reminder",
        description=(
            "Use when the owner wants to see a payment reminder for one customer "
            "before deciding. Returns a polite standard text naming that "
            "customer's overdue invoices and their total. It sends nothing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "customer": {"type": "string", "description": "Customer name or ID."},
            },
            "required": ["customer"],
            "additionalProperties": False,
        },
        level="green",
        handler=draft_reminder,
    ))
    register(Action(
        name="send_reminder",
        description=(
            "Use when the owner asks to send a payment reminder to one customer "
            "with overdue invoices. The owner is shown the full text and must "
            "approve it on screen. Leave text out to send the standard reminder."
        ),
        parameters={
            "type": "object",
            "properties": {
                "customer": {"type": "string", "description": "Customer name or ID."},
                "text": {
                    "type": "string",
                    "description": "The owner's own wording. Leave out for the standard text.",
                },
            },
            "required": ["customer"],
            "additionalProperties": False,
        },
        level="yellow",
        handler=send_reminder,
        summarize=describe_reminder,
        prepare=prepare_reminder,
    ))
