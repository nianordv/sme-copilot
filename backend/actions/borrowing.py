"""Covering a shortfall: what each way costs, and a draft request to the bank.

compare_options (green) lists the ways to cover a shortfall of a given size
for a given number of days: remind late customers, wait, overdraft, loan. Each
comes with its total cost in GEL and its yearly rate, cheapest first. Every
figure comes from the books or from arithmetic in backend/bank/products.py on
the terms in backend/data/products.json. None comes from the model.

prepare_credit_request (yellow) saves a filled-in draft after the owner
presses Yes. Before the owner is asked, the amount is checked against the
product's limits and the product's terms are written into the request, so the
rate the owner approves is the rate the draft is saved with, whatever happens
to the terms file in between. A draft is never submitted here: only the owner
can do that, in the bank app.

register_borrowing_actions() puts both in the registry. The server calls it
once at start-up.
"""

import json
from typing import Any

from backend.bank.db import connect, has_table, today
from backend.bank.products import load_products, loan_cost, overdraft_cost

from .lookups import by_customer, gel, largest, money, open_invoices
from .registry import Action, ActionError, register

PRODUCTS = ("overdraft", "term_loan")
LARGEST_AMOUNT = 1_000_000_000
NAMES_SHOWN = 5
PLACEHOLDER_TERMS = "The product terms are placeholders, not a real bank's offer."
ONLY_A_DRAFT = ("It is only a draft: nothing is submitted. "
                "Only you can submit it, in the bank app.")

TABLES = """
CREATE TABLE IF NOT EXISTS credit_request_drafts (
    draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_on TEXT NOT NULL,
    product TEXT NOT NULL,
    amount REAL NOT NULL,
    details TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
);
"""


# ------------------------------------------------------------------ option costs
def compare_options(amount: float, days: int, quoted_annual_rate: float | None = None) -> dict[str, Any]:
    amount, days = money(amount), int(days)
    if amount < 0.01:
        return {
            "currency": "GEL",
            "shortfall": 0.0,
            "options": [],
            "message": "There is no shortfall to cover, so no borrowing is needed.",
        }
    products = load_products()
    loan_terms = dict(products['term_loan'])
    if quoted_annual_rate is not None:
        if not 0 <= quoted_annual_rate <= 100:
            raise ActionError('Annual interest must be between 0% and 100%.')
        loan_terms['yearly_rate_percent'] = quoted_annual_rate
    with connect() as conn:
        all_open = open_invoices(conn, today())
        overdue = [invoice for invoice in all_open if invoice['days_late'] > 0]

    options = [
        _remind_option(amount, overdue),
        {
            "option": "wait",
            "cost": 0.0,
            "yearly_rate_percent": 0.0,
            "covers_amount": False,
            "certain": False,
            "note": "Free, but the shortfall stays unless customers pay in time.",
        },
        _overdraft_option(amount, days, products["overdraft"]),
        _loan_option(amount, loan_terms),
    ]
    if products.get('early_discount'):
        terms = products['early_discount']
        percent = float(terms['percent'])
        if not 0 < percent < 100:
            raise ValueError('Invalid discount terms.')
        # Whole outstanding invoices, largest first. This is a disclosed
        # contact strategy, not an optimization over every possible subset.
        selected=[]
        gross_cents=0
        net_cents=0
        from backend.bank.ledger import cents, money as cash_money
        for invoice in sorted(all_open,key=lambda r:(-r['amount'],r['invoice_id'])):
            if net_cents >= cents(amount): break
            face=cents(invoice['amount'])
            discount=cents(invoice['amount']*percent/100)
            gross_cents+=face
            net_cents+=face-discount
            selected.append(invoice['invoice_id'])
        cost=cash_money(gross_cents-net_cents)
        options.append({'option':'early_payment_discount','cost':cost,
            'yearly_rate_percent':round(percent/(100-percent)*365/days*100,2),
            'rate_basis':'Simple annualized discount cost for the requested period, not a bank APR.',
            'covers_amount':net_cents>=cents(amount),'certain':False,
            'discount_percent':percent,'potential_net_receipts':cash_money(net_cents),
            'invoice_ids':selected[:10],'invoices_not_listed':max(0,len(selected)-10),
            'note':'Conditional on customers agreeing and paying within the gap period. Discount applies to entire selected invoices, largest first; not a guaranteed or optimal collection strategy.'})
    cost_bases = {
        'wait': 'Direct charges only; consequences of late payment are not priced.',
        'remind_late_customers': 'Direct charges only; consequences of late payment are not priced.',
        'early_payment_discount': 'Discount on the full face amount of the selected invoices.',
        'term_loan': 'Full configured product term and fees.',
        'overdraft': 'Requested period and configured fees.',
    }
    for option in options:
        option['cost_basis'] = cost_bases[option['option']]
        if option['option'] in ('overdraft','term_loan'):
            option['subject_to_approval']=True
            option['note'] += ' Availability is conditional on bank approval; Bill cannot approve funding.'
    # Cheapest first. Equal costs keep the order above.
    options.sort(key=lambda option: option["cost"])

    covering = [option for option in options if option["covers_amount"]]
    recommended = covering[0] if covering else None
    # When the cheapest way depends on customers paying, also name the
    # cheapest credit option, still subject to the bank's approval.
    fallback = None
    if recommended is not None and not recommended["certain"]:
        fallback = next((option for option in covering if option["certain"]), None)
    for option in options:
        option["recommended"] = option is recommended
        option["fallback"] = option is fallback

    result: dict[str, Any] = {
        "currency": "GEL",
        "shortfall": amount,
        "days": days,
        "options": options,
        "recommended": recommended["option"] if recommended else None,
        "fallback": fallback["option"] if fallback else None,
        "loan_rate_scenarios": [
            {key: value for key, value in _loan_option(
                amount, {**loan_terms, 'yearly_rate_percent': rate}).items()
             if key in ('yearly_rate_percent', 'principal', 'term_months',
                        'monthly_payment', 'interest', 'fee', 'cost')}
            for rate in sorted(set(products.get('loan_rate_scenarios', [12, 15, 18])
                                   + ([] if quoted_annual_rate is None else [quoted_annual_rate])))
        ],
        "loan_scenario_note": 'Assumed nominal annual rates, not available bank offers or market minimum/maximum rates. Each scenario uses the same principal, term and fees. Costs cover the full loan term; approval is not guaranteed.',
        "quoted_annual_rate": quoted_annual_rate,
    }
    from backend.funding_plans import collection_plans
    result['collection_plans'] = collection_plans(all_open, amount, days, products, loan_terms)
    share = products.get('early_discount', {}).get('minimum_collection_share', 0.2)
    result['collection_note'] = f'Conditional examples, ranked by direct cost. Customers must agree and pay today; borrowing requires approval. Customer choices reflect invoice amounts and cost, not a prediction that a discount will change behavior. Combinations must collect at least {share:.0%} of the stated gap under the demo selection policy. Only whole invoices and up to three-invoice bundles are considered; this is not a global optimum. Use preview_funding_plan before claiming a plan removes a forecast shortfall.'
    if products.get("placeholder"):
        result["note"] = PLACEHOLDER_TERMS
    return result


def _remind_option(amount: float, overdue: list[dict[str, Any]]) -> dict[str, Any]:
    late = by_customer(overdue)
    late = largest(late, "overdue", len(late))
    # The fewest late customers, largest first, who owe the shortfall between them.
    names: list[str] = []
    they_owe = 0.0
    for row in late:
        if they_owe >= amount:
            break
        names.append(row["customer"])
        they_owe += row["overdue"]
    they_owe = money(they_owe)
    covers = they_owe >= amount

    if not late:
        note = "No customer is late, so there is nobody to remind."
    elif covers:
        who = "1 late customer owes" if len(names) == 1 else f"{len(names)} late customers owe"
        note = f"{who} {gel(they_owe)}. Free, but it only works if they pay in time."
    else:
        note = f"All late customers together owe {gel(they_owe)}, less than the shortfall."
    return {
        "option": "remind_late_customers",
        "cost": 0.0,
        "yearly_rate_percent": 0.0,
        "covers_amount": covers,
        "certain": False,
        "customers_to_remind": names[:NAMES_SHOWN],
        "customers_not_listed": max(len(names) - NAMES_SHOWN, 0),
        "they_owe": they_owe,
        "note": note,
    }


def _overdraft_option(amount: float, days: int, terms: dict[str, Any]) -> dict[str, Any]:
    used = min(amount, terms["limit"])
    cost = overdraft_cost(used, days, terms)
    note = (f"{terms['yearly_rate_percent']:g}% a year on {gel(used)} for {days} "
            f"{'day' if days == 1 else 'days'}. Limit {gel(terms['limit'])}.")
    if cost["fee"]:
        note += f" Includes a fee of {gel(cost['fee'])}."
    return {
        "option": "overdraft",
        "name": terms["name"],
        "cost": cost["total"],
        "yearly_rate_percent": float(terms["yearly_rate_percent"]),
        "covers_amount": amount <= terms["limit"],
        "certain": True,
        "note": note,
    }


def _loan_option(amount: float, terms: dict[str, Any]) -> dict[str, Any]:
    # A loan cannot be smaller than the bank's minimum or larger than its maximum.
    borrowed = min(max(amount, terms["min_amount"]), terms["max_amount"])
    cost = loan_cost(borrowed, terms)
    note = (f"{gel(borrowed)} repaid in {terms['term_months']} monthly payments of "
            f"{gel(cost['monthly_payment'])}: {gel(cost['interest'])} interest plus a "
            f"fee of {gel(cost['fee'])}. Runs the whole term even if the gap closes sooner.")
    if borrowed > amount:
        note += f" The smallest loan is {gel(terms['min_amount'])}, more than is needed."
    return {
        "option": "term_loan",
        "name": terms["name"],
        "cost": cost["total"],
        "yearly_rate_percent": float(terms["yearly_rate_percent"]),
        "covers_amount": borrowed >= amount,
        "principal": borrowed,
        "term_months": terms['term_months'],
        "monthly_payment": cost['monthly_payment'],
        "interest": cost['interest'],
        "fee": cost['fee'],
        "certain": True,
        "note": note,
    }


# --------------------------------------------------------------- credit requests
def prepare_request(product: str, amount: float) -> dict[str, Any]:
    terms = load_products()[product]
    amount = money(amount)
    if amount < 0.01:
        raise ActionError("The amount must be at least GEL 0.01.")
    if product == "overdraft" and amount > terms["limit"]:
        raise ActionError(f"The most an overdraft can be is {gel(terms['limit'])}.")
    if product == "term_loan" and not terms["min_amount"] <= amount <= terms["max_amount"]:
        raise ActionError(f"A loan must be between {gel(terms['min_amount'])} and "
                          f"{gel(terms['max_amount'])}.")
    final: dict[str, Any] = {
        "product": product,
        "amount": amount,
        "name": terms["name"],
        "yearly_rate_percent": float(terms["yearly_rate_percent"]),
        "fee_percent": float(terms.get("fee_percent", 0)),
    }
    if "term_months" in terms:
        final["term_months"] = int(terms["term_months"])
    return final


def describe_request(product: str, amount: float, name: str, yearly_rate_percent: float,
                     fee_percent: float, term_months: int | None = None) -> str:
    # Every value here is ours: the product comes from a fixed list, the amount
    # was rebuilt by prepare_request and the terms come from the terms file.
    price = f"{yearly_rate_percent:g}% a year"
    if fee_percent:
        price += f" plus a {fee_percent:g}% fee"
    if term_months is not None:
        price += f", over {term_months} months"
    return f"Save a draft request to the bank: {name}, {gel(amount)}, {price}?\n{ONLY_A_DRAFT}"


def prepare_credit_request(product: str, amount: float, name: str, yearly_rate_percent: float,
                           fee_percent: float, term_months: int | None = None) -> dict[str, Any]:
    prepared_on = today().isoformat()
    with connect() as conn:
        conn.executescript(TABLES)
        details = {
            "applicant": conn.execute("SELECT name FROM company").fetchone()["name"],
            "product": name,
            "amount": amount,
            "currency": "GEL",
            "yearly_rate_percent": yearly_rate_percent,
            "fee_percent": fee_percent,
            "prepared_on": prepared_on,
        }
        if term_months is not None:
            details["term_months"] = term_months
        with conn:
            draft_id = conn.execute(
                "INSERT INTO credit_request_drafts (created_on, product, amount, details) "
                "VALUES (?, ?, ?, ?)",
                (prepared_on, product, amount, json.dumps(details)),
            ).lastrowid
    return {
        "draft_id": draft_id,
        "product": name,
        "amount": amount,
        "status": "draft, not submitted",
        "submitted": False,
        "message": "The draft is saved. Only the owner can submit it, in the bank app.",
    }


def saved_credit_requests(limit: int = 10) -> dict[str, Any]:
    """The newest draft requests, newest first, for the dashboard. Not an
    action, and it only reads. Every value is ours: the terms were written
    into the draft from the terms file when the owner approved it."""
    with connect() as conn:
        if not has_table(conn, "credit_request_drafts"):
            return {"items": [], "not_listed": 0}
        rows = conn.execute(
            "SELECT draft_id, created_on, product, amount, details, status "
            "FROM credit_request_drafts ORDER BY draft_id DESC LIMIT ?", (limit,)).fetchall()
        saved = conn.execute("SELECT COUNT(*) FROM credit_request_drafts").fetchone()[0]
    items = []
    for row in rows:
        details = json.loads(row["details"])
        items.append({
            "draft_id": row["draft_id"], "saved_on": row["created_on"], "product": row["product"],
            "product_name": details["product"], "amount": money(row["amount"]), "currency": details["currency"],
            "yearly_rate_percent": details["yearly_rate_percent"], "fee_percent": details["fee_percent"],
            "term_months": details.get("term_months"), "status": row["status"],
        })
    return {"items": items, "not_listed": saved - len(rows)}


# ------------------------------------------------------------------ registration
def register_borrowing_actions() -> None:
    register(Action(
        name="compare_options",
        description=(
            "Use when the owner is short of cash and asks what they can do. Give "
            "the shortfall and how many days it lasts. Returns each way to cover "
            "it (remind late customers, wait, overdraft, loan) with its total "
            "cost in GEL and yearly rate, cheapest first, with one recommended "
            "and a fallback subject to bank approval. Also returns customer-named collection_plans: "
            "discount-only or discount plus borrowing the remaining gap. Explain their conditions "
            "and costs; use preview_funding_plan to assess the cash impact."
        ),
        parameters={
            "type": "object",
            "properties": {
                "amount": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": LARGEST_AMOUNT,
                    "description": "The shortfall to cover, in GEL.",
                },
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 365,
                    "description": "How many days the shortfall lasts.",
                },
                "quoted_annual_rate": {
                    "type": "number", "minimum": 0, "maximum": 100,
                    "description": "Optional nominal annual loan rate explicitly quoted by the owner. Never invent a quote.",
                },
            },
            "required": ["amount", "days"],
            "additionalProperties": False,
        },
        level="green",
        handler=compare_options,
    ))
    register(Action(
        name="prepare_credit_request",
        description=(
            "Use when the owner has chosen an overdraft or a loan and asks to "
            "prepare the request. Saves a filled-in draft after the owner "
            "approves it on screen. The draft is not submitted: only the owner "
            "can submit it, in the bank app."
        ),
        parameters={
            "type": "object",
            "properties": {
                "product": {
                    "type": "string",
                    "enum": list(PRODUCTS),
                    "description": "Which product to request.",
                },
                "amount": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": LARGEST_AMOUNT,
                    "description": "The amount to request, in GEL.",
                },
            },
            "required": ["product", "amount"],
            "additionalProperties": False,
        },
        level="yellow",
        handler=prepare_credit_request,
        summarize=describe_request,
        prepare=prepare_request,
    ))
