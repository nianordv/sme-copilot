"""Green look-up actions: plain functions over the books.

Each one can be called and tested with no AI model running. The model reads
every result and has a small memory, so results are summaries: totals, counts
by age band and a short list of the largest rows, never a full invoice list.

Amounts are GEL rounded to 2 decimals, dates are ISO strings, and "today" is
the snapshot date of the books.

register_lookup_actions() puts them in the registry. The server calls it once
at start-up, next to register_demo_actions().
"""

import difflib
import sqlite3
import statistics
from datetime import date, timedelta
from typing import Any

from backend.actions.registry import Action, register
from backend.bank.db import connect, today

TOP_ROWS = 10
MAX_OUTFLOW_DAYS = 60
# (label, fewest days late, most days late)
AGE_BANDS = (("1-30", 1, 30), ("31-60", 31, 60), ("over_60", 61, None))


def money(value: float | None) -> float:
    return round(float(value or 0), 2)


def gel(amount: float) -> str:
    """An amount as people read it: GEL 1,500.50."""
    return f"GEL {amount:,.2f}"


# The registry does not check argument types yet (that is phase A3), and a
# small model often sends "true" or "30" as text. Read both forms the same way,
# so that "false" is never taken for yes.
def as_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "yes", "1"}
    return bool(value)


def as_whole_number(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    return int(float(value))


# ----------------------------------------------------------- finding a customer
def find_customer(
    conn: sqlite3.Connection, text: str
) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    """The one customer the text names, or None plus the candidates to offer.

    Tried in order: exact ID, exact name, then every typed word appearing in
    the name. A near-miss spelling is only ever offered as a candidate."""
    wanted = " ".join(str(text).split()).casefold()
    customers = conn.execute(
        "SELECT customer_id, name, segment, city FROM customers ORDER BY name"
    ).fetchall()
    if not wanted:
        return None, []

    for column in ("customer_id", "name"):
        exact = [c for c in customers if c[column].casefold() == wanted]
        if len(exact) == 1:
            return exact[0], []

    words = wanted.split()
    partial = [c for c in customers if all(w in c["name"].casefold() for w in words)]
    if len(partial) == 1:
        return partial[0], []
    if partial:
        return None, partial

    by_name = {c["name"].casefold(): c for c in customers}
    close = difflib.get_close_matches(wanted, by_name, n=5, cutoff=0.6)
    return None, [by_name[name] for name in close]


def no_single_customer(text: str, candidates: list[sqlite3.Row]) -> dict[str, Any]:
    """What an action returns when a name matches several customers or none."""
    if candidates:
        message = (
            f"'{text}' could be several customers. "
            "Ask the owner which one they mean."
        )
    else:
        message = f"No customer matches '{text}'. Ask the owner for the name."
    return {
        "found": False,
        "message": message,
        "candidates_total": len(candidates),
        "candidates": [
            {"customer_id": c["customer_id"], "name": c["name"], "city": c["city"]}
            for c in candidates[:TOP_ROWS]
        ],
    }


# ------------------------------------------------------------------ receivables
def open_invoices(
    conn: sqlite3.Connection, as_of: date, customer_id: str | None = None
) -> list[dict[str, Any]]:
    """Every unpaid invoice with its days late: positive once the due date has
    passed, zero or negative while it has not."""
    sql = (
        "SELECT i.invoice_id, i.customer_id, c.name, i.issue_date, i.due_date, "
        "i.amount FROM invoices i JOIN customers c USING (customer_id) "
        "WHERE i.paid_date IS NULL AND i.writeoff_date IS NULL"
    )
    parameters: tuple = ()
    if customer_id is not None:
        sql += " AND i.customer_id = ?"
        parameters = (customer_id,)
    from backend.bank.ledger import cents, money as from_cents, payments_by_invoice
    paid = payments_by_invoice(conn, as_of)
    result = []
    for row in conn.execute(sql, parameters):
        if row['issue_date'] > as_of.isoformat():
            continue
        remaining = cents(row['amount']) - paid.get(row['invoice_id'], 0)
        if remaining > 0:
            result.append({**dict(row), 'original_amount': row['amount'], 'amount': from_cents(remaining),
                           'days_late': (as_of - date.fromisoformat(row['due_date'])).days})
    return result



def total(invoices: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "amount": money(sum(i["amount"] for i in invoices)),
        "invoices": len(invoices),
        "customers": len({i["customer_id"] for i in invoices}),
    }


def by_customer(invoices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for invoice in invoices:
        row = rows.setdefault(invoice["customer_id"], {
            "customer": invoice["name"],
            "customer_id": invoice["customer_id"],
            "owed": 0.0, "overdue": 0.0, "invoices": 0, "oldest_days_late": 0,
        })
        row["owed"] += invoice["amount"]
        row["invoices"] += 1
        if invoice["days_late"] > 0:
            row["overdue"] += invoice["amount"]
            row["oldest_days_late"] = max(row["oldest_days_late"], invoice["days_late"])
    for row in rows.values():
        row["owed"], row["overdue"] = money(row["owed"]), money(row["overdue"])
    return list(rows.values())


def largest(rows: list[dict[str, Any]], key: str, count: int) -> list[dict[str, Any]]:
    # The ID breaks ties, so the same books always give the same list.
    ranked = sorted(rows, key=lambda row: (-row[key], row["customer_id"]))
    return [row for row in ranked if row[key] > 0][:count]


def invoice_line(invoice: dict[str, Any]) -> dict[str, Any]:
    return {
        "invoice_id": invoice["invoice_id"],
        "issued": invoice["issue_date"],
        "due": invoice["due_date"],
        "amount": money(invoice["amount"]),
        "days_late": max(invoice["days_late"], 0),
    }


def get_receivables(
    only_overdue: bool = False,
    min_days_overdue: int = 0,
    customer: str | None = None,
) -> dict[str, Any]:
    as_of = today()
    only_overdue = as_flag(only_overdue)
    min_days_overdue = max(as_whole_number(min_days_overdue, 0), 0)
    with connect() as conn:
        match = None
        if customer:
            match, candidates = find_customer(conn, customer)
            if match is None:
                return no_single_customer(customer, candidates)
        invoices = open_invoices(conn, as_of, match["customer_id"] if match else None)

    if min_days_overdue > 0:
        invoices = [i for i in invoices if i["days_late"] >= min_days_overdue]
        showing = f"unpaid invoices at least {min_days_overdue} days late"
    elif only_overdue:
        invoices = [i for i in invoices if i["days_late"] > 0]
        showing = "unpaid invoices that are overdue"
    else:
        showing = "all unpaid invoices"
    overdue = [i for i in invoices if i["days_late"] > 0]

    result: dict[str, Any] = {
        "found": True,
        "as_of": as_of.isoformat(),
        "currency": "GEL",
        "showing": showing + (f" of {match['name']}" if match else ""),
        "owed": total(invoices),
        "of_which_overdue": total(overdue),
        "overdue_by_days_late": {
            label: {
                "amount": money(sum(i["amount"] for i in band)),
                "invoices": len(band),
            }
            for label, low, high in AGE_BANDS
            for band in [[
                i for i in overdue
                if i["days_late"] >= low and (high is None or i["days_late"] <= high)
            ]]
        },
    }

    if match:
        # One customer: list their invoices, oldest due date first.
        ordered = sorted(invoices, key=lambda i: (i["due_date"], i["invoice_id"]))
        result["invoices"] = [invoice_line(i) for i in ordered[:TOP_ROWS]]
        result["invoices_not_listed"] = max(len(ordered) - TOP_ROWS, 0)
    elif only_overdue or min_days_overdue > 0:
        # Only overdue invoices were counted, so "owed" would repeat "overdue".
        result["largest_overdue"] = [
            {key: value for key, value in row.items() if key != "owed"}
            for row in largest(by_customer(invoices), "overdue", TOP_ROWS)
        ]
    else:
        customers = by_customer(invoices)
        result["largest_owed"] = largest(customers, "owed", TOP_ROWS // 2)
        result["largest_overdue"] = largest(customers, "overdue", TOP_ROWS // 2)
    return result


# --------------------------------------------------------------------- customer
def payment_habit(
    conn: sqlite3.Connection, customer_id: str, as_of: date
) -> dict[str, Any]:
    """How late the customer usually pays, from invoices paid in the last 12
    months, or from all history when there are none. Negative means early."""
    paid = conn.execute(
        "SELECT due_date, paid_date FROM invoices "
        "WHERE customer_id = ? AND paid_date IS NOT NULL",
        (customer_id,),
    ).fetchall()
    from backend.bank.db import has_table
    from backend.bank.ledger import payments_by_invoice, cents
    paid = [dict(row) for row in paid if row['paid_date'] <= as_of.isoformat()]
    if has_table(conn, 'cash_entries'):
        net = payments_by_invoice(conn, as_of)
        for row in conn.execute('SELECT invoice_id,due_date,amount FROM invoices WHERE customer_id=? AND paid_date IS NULL', (customer_id,)):
            if net.get(row['invoice_id'],0) >= cents(row['amount']):
                closed = conn.execute('SELECT MAX(event_date) FROM cash_entries WHERE invoice_id=? AND event_date<=?', (row['invoice_id'],as_of.isoformat())).fetchone()[0]
                paid.append({'due_date':row['due_date'],'paid_date':closed})
    year_ago = (as_of - timedelta(days=365)).isoformat()
    recent = [row for row in paid if row["paid_date"] > year_ago]
    period, rows = ("last 12 months", recent) if recent else ("all history", paid)
    delays = [
        (date.fromisoformat(row["paid_date"]) - date.fromisoformat(row["due_date"])).days
        for row in rows
    ]
    if not delays:
        return {"period": period, "invoices_paid": 0}
    return {
        "period": period,
        "invoices_paid": len(delays),
        "median_days_late": round(statistics.median(delays), 1),
        "average_days_late": round(statistics.mean(delays), 1),
        "share_paid_late": round(sum(d > 0 for d in delays) / len(delays), 2),
    }


def get_customer(customer: str) -> dict[str, Any]:
    as_of = today()
    with connect() as conn:
        match, candidates = find_customer(conn, customer)
        if match is None:
            return no_single_customer(customer, candidates)
        invoices = open_invoices(conn, as_of, match["customer_id"])
        habit = payment_habit(conn, match["customer_id"], as_of)
        written_off = conn.execute(
            "SELECT COUNT(*) AS invoices, COALESCE(SUM(amount), 0) AS amount "
            "FROM invoices WHERE customer_id = ? AND writeoff_date IS NOT NULL",
            (match["customer_id"],),
        ).fetchone()

    overdue = [i for i in invoices if i["days_late"] > 0]
    ordered = sorted(invoices, key=lambda i: (i["due_date"], i["invoice_id"]))
    return {
        "found": True,
        "as_of": as_of.isoformat(),
        "currency": "GEL",
        "customer": dict(match),
        "owed": {"amount": money(sum(i["amount"] for i in invoices)), "invoices": len(invoices)},
        "of_which_overdue": {
            "amount": money(sum(i["amount"] for i in overdue)),
            "invoices": len(overdue),
            "oldest_days_late": max((i["days_late"] for i in overdue), default=0),
        },
        "open_invoices": [invoice_line(i) for i in ordered[:TOP_ROWS]],
        "open_invoices_not_listed": max(len(ordered) - TOP_ROWS, 0),
        "payment_habit": habit,
        "written_off": {
            "amount": money(written_off["amount"]),
            "invoices": written_off["invoices"],
        },
    }


# ------------------------------------------------------- the company's own money
def get_balance() -> dict[str, Any]:
    with connect() as conn:
        company = conn.execute(
            "SELECT name, currency, opening_balance, balance_date FROM company"
        ).fetchone()
        from backend.bank.ledger import balance
        current = balance(conn)
    return {
        "as_of": current["as_of"],
        "currency": company["currency"],
        "balance": current["balance"],
        "account_holder": company["name"],
    }


def get_upcoming_outflows(days: int = 30) -> dict[str, Any]:
    as_of = today()
    days = min(max(as_whole_number(days, 30), 1), MAX_OUTFLOW_DAYS)
    last = as_of + timedelta(days=days)
    with connect() as conn:
        from backend.bank.ledger import obligations
        payments = [row for row in obligations(conn) if as_of.isoformat() < row['due_date'] <= last.isoformat()]

    by_category: dict[str, float] = {}
    for payment in payments:
        by_category[payment["category"]] = (
            by_category.get(payment["category"], 0.0) + payment["amount"]
        )
    listed = payments[:TOP_ROWS * 2]
    return {
        "as_of": as_of.isoformat(),
        "currency": "GEL",
        "from": (as_of + timedelta(days=1)).isoformat(),
        "to": last.isoformat(),
        "days": days,
        "total": money(sum(p["amount"] for p in payments)),
        "by_category": {name: money(amount) for name, amount in by_category.items()},
        "payments": [
            {"date": p["due_date"], "label": p["label"], "amount": money(p["amount"])}
            for p in listed
        ],
        "payments_not_listed": len(payments) - len(listed),
    }


# ------------------------------------------------------------------ registration
def register_lookup_actions() -> None:
    register(Action(
        name="get_receivables",
        description=(
            "Use when the owner asks who owes them money, who is late, or how "
            "much is overdue. Returns the total owed, the overdue part split by "
            "days late, and the customers who owe the most. With a customer, "
            "returns that customer's unpaid invoices."
        ),
        parameters={
            "type": "object",
            "properties": {
                "only_overdue": {
                    "type": "boolean",
                    "description": "True to count only invoices past their due date.",
                },
                "min_days_overdue": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Count only invoices at least this many days late.",
                },
                "customer": {
                    "type": "string",
                    "description": "Customer name or ID, to look at one customer.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        level="green",
        handler=get_receivables,
    ))

    register(Action(
        name="get_customer",
        description=(
            "Use when the owner asks about one customer. Finds the customer by "
            "name or ID and returns what they owe, their unpaid invoices and how "
            "late they usually pay (negative days mean early). If the name fits "
            "several customers or none, returns candidates: ask the owner which."
        ),
        parameters={
            "type": "object",
            "properties": {
                "customer": {
                    "type": "string",
                    "description": "Customer name or ID, as the owner wrote it.",
                },
            },
            "required": ["customer"],
            "additionalProperties": False,
        },
        level="green",
        handler=get_customer,
    ))

    register(Action(
        name="get_balance",
        description=(
            "Use when the owner asks how much money is in the bank account. "
            "Returns today's balance."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        level="green",
        handler=get_balance,
    ))

    register(Action(
        name="get_upcoming_outflows",
        description=(
            "Use when the owner asks what they have to pay soon. Returns the "
            "salaries, taxes and stock purchases due in the next days, with "
            "dates, and their total. This lists obligations only. For whether "
            "the owner can afford them, use forecast_cash instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_OUTFLOW_DAYS,
                    "description": "How many days ahead to look. Default 30.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        level="green",
        handler=get_upcoming_outflows,
    ))
