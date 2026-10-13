"""What the owner asks Bill to keep: an alert and a plan.

Both are yellow: nothing is stored until the owner presses Yes. Before the
owner is asked, the request is tidied and checked. A date becomes a real date
after today, an amount is rounded to two decimals, an empty note is dropped,
and the owner is shown exactly that. A date in an earlier year is moved to the
next time that day comes round, because the model does not know today's date.

An alert is for a date, for the bank balance falling below an amount, or for
both. A plan is a short text the owner has agreed to. Each is one row in its
own table. This demo only stores them: nothing watches an alert or sends a
notification, and the replies say so.

register_planning_actions() puts both in the registry. The server calls it
once at start-up.
"""

import datetime
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from backend.bank.db import connect, today

from .lookups import gel, money
from .registry import Action, ActionError, one_line, quoted, register

LARGEST_BALANCE = 1_000_000_000
NOTHING_SENT = "The alert is saved in the app. This demo does not send notifications."

TABLES = """
CREATE TABLE IF NOT EXISTS alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_on TEXT NOT NULL,
    alert_date TEXT,
    balance_below REAL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_on TEXT NOT NULL,
    text TEXT NOT NULL
);
"""


@contextmanager
def _store() -> Iterator[sqlite3.Connection]:
    """A connection with the two tables in place. Saves on leaving."""
    with connect() as conn:
        conn.executescript(TABLES)
        with conn:
            yield conn


# ------------------------------------------------------------------------ alerts
def prepare_alert(date: str | None = None, balance_below: float | None = None,
                  note: str | None = None) -> dict[str, Any]:
    if date is None and balance_below is None:
        raise ActionError("An alert needs a date or a balance to watch for. Ask the owner which.")
    final: dict[str, Any] = {}
    if date is not None:
        now = today()
        try:
            day = datetime.date.fromisoformat(date.strip())
        except ValueError:
            # The example carries the right year: the model does not know today's date.
            example = (now + datetime.timedelta(days=7)).isoformat()
            raise ActionError(f"The alert date must look like {example}.") from None
        day = _coming(day, now)
        if day <= now:
            raise ActionError(f"The alert date must be after today, {now.isoformat()}.")
        final["date"] = day.isoformat()
    if balance_below is not None:
        amount = money(balance_below)
        if amount < 0.01:
            raise ActionError("The balance to watch for must be at least GEL 0.01.")
        final["balance_below"] = amount
    if note and note.strip():
        final["note"] = note.strip()
    return final


def _coming(day: datetime.date, now: datetime.date) -> datetime.date:
    """The day the owner most likely means. The model does not know today's
    date, so for a day given without its year it sends a past year. Such a
    date is moved to the next time that day comes round, and the owner sees
    the full date before approving. A date earlier in this same year is left
    alone, and is turned down: it could mean next year or be a slip."""
    if day.year >= now.year:
        return day
    for year in (now.year, now.year + 1):
        try:
            again = day.replace(year=year)
        except ValueError:  # 29 February in a year without one.
            continue
        if again > now:
            return again
    return day


def describe_alert(date: str | None = None, balance_below: float | None = None,
                   note: str | None = None) -> str:
    # The date and the amount were rebuilt by prepare_alert. The note is the
    # model's wording, so it stays inside quotation marks on one line.
    when = []
    if date is not None:
        when.append(f"on {date}")
    if balance_below is not None:
        when.append(f"for when the bank balance falls below {gel(balance_below)}")
    question = f"Set an alert {' and '.join(when)}?"
    return f"{question}\nNote: {one_line(note)}" if note else question


def set_alert(date: str | None = None, balance_below: float | None = None,
              note: str | None = None) -> dict[str, Any]:
    created_on = today().isoformat()
    with _store() as conn:
        alert_id = conn.execute(
            "INSERT INTO alerts (created_on, alert_date, balance_below, note) VALUES (?, ?, ?, ?)",
            (created_on, date, balance_below, note),
        ).lastrowid
    alert: dict[str, Any] = {"alert_id": alert_id, "status": "saved"}
    if date is not None:
        alert["date"] = date
    if balance_below is not None:
        alert["balance_below"] = balance_below
    alert["message"] = NOTHING_SENT
    return alert


# ------------------------------------------------------------------------- plans
def prepare_plan(text: str) -> dict[str, Any]:
    written = text.strip()
    if not written:
        raise ActionError("There is no plan text to save.")
    return {"text": written}


def describe_plan(text: str) -> str:
    return f"Save this plan?\n\n{quoted(text)}"


def save_plan(text: str) -> dict[str, Any]:
    saved_on = today().isoformat()
    with _store() as conn:
        plan_id = conn.execute(
            "INSERT INTO plans (created_on, text) VALUES (?, ?)", (saved_on, text),
        ).lastrowid
    return {"plan_id": plan_id, "saved_on": saved_on, "status": "saved"}


# ------------------------------------------------------------------ registration
def register_planning_actions() -> None:
    register(Action(
        name="set_alert",
        description=(
            "Use when the owner asks to be alerted on a date, or when the bank "
            "balance falls below an amount. Give a date, a balance, or both. "
            "The owner must approve it on screen. This demo stores the alert; "
            "it does not send notifications."
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "maxLength": 20,
                    "description": "The day to alert on, as YYYY-MM-DD.",
                },
                "balance_below": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": LARGEST_BALANCE,
                    "description": "Alert when the bank balance falls below this amount, in GEL.",
                },
                "note": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "A few words on what the alert is about.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        level="yellow",
        handler=set_alert,
        summarize=describe_alert,
        prepare=prepare_alert,
    ))
    register(Action(
        name="save_plan",
        description=(
            "Use when the owner has agreed to a plan and asks to keep it. "
            "Saves the plan text after the owner approves it on screen."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1000,
                    "description": "The plan, in a few sentences.",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        level="yellow",
        handler=save_plan,
        summarize=describe_plan,
        prepare=prepare_plan,
    ))
