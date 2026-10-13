"""The bank's products and what they cost.

The terms come from backend/data/products.json and nowhere else. The functions
here are plain arithmetic, with no database and no AI model.
"""

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_PRODUCTS = Path(__file__).resolve().parent.parent / "data" / "products.json"


def load_products() -> dict[str, Any]:
    # Read on every call, so an edit to the file shows up without a restart.
    return json.loads(Path(os.environ.get("RIGHTSIZE_PRODUCTS", DEFAULT_PRODUCTS)).read_text())


def overdraft_cost(amount: float, days: int, terms: dict[str, Any]) -> dict[str, float]:
    """Interest on the amount used, for the days it is used, on a 365-day
    year, plus the fee if there is one.

    GEL 8,000 for 21 days at 16% a year: 8,000 x 0.16 x 21 / 365 = 73.64."""
    interest = round(amount * terms["yearly_rate_percent"] / 100 * days / 365, 2)
    fee = round(amount * terms.get("fee_percent", 0) / 100, 2)
    return {"interest": interest, "fee": fee, "total": round(interest + fee, 2)}


def loan_cost(amount: float, terms: dict[str, Any]) -> dict[str, float]:
    """A loan repaid in equal monthly payments over its whole term, plus a fee
    taken at the start.

    The payment is amount x r / (1 - (1 + r)^-n), with r the monthly rate and
    n the number of months. GEL 30,000 over 12 months at 15% a year is 2,707.75
    a month, so 2,493.00 of interest, plus a 1% fee of 300.00: 2,793.00."""
    months = terms["term_months"]
    monthly_rate = terms["yearly_rate_percent"] / 100 / 12
    if monthly_rate == 0:
        payment = amount / months
    else:
        payment = amount * monthly_rate / (1 - (1 + monthly_rate) ** -months)
    payment = round(payment, 2)
    interest = round(payment * months - amount, 2)
    fee = round(amount * terms.get("fee_percent", 0) / 100, 2)
    return {
        "monthly_payment": payment,
        "interest": interest,
        "fee": fee,
        "total": round(interest + fee, 2),
    }
