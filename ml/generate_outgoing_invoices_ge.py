"""
Synthetic OUTGOING invoices for the same Georgian supplier: the bills it receives from its own suppliers and pays.
One row per bill. Amounts are in GEL. Companion to generate_invoices_ge.py, which generates the INCOMING side
(the invoices the company issues to its clients).

Run from repository root, after the incoming generator:  python -m ml.generate_outgoing_invoices_ge

Purchases follow sales: each category's bills add up to a fixed share of what the company invoiced, and are larger
in the weeks when it sold more. The sales figures are read from the incoming CSV, so regenerate this file whenever
that one changes.

COLUMN GUIDE
  identifiers : invoice_id, supplier_id, supplier_category, supplier_city
  expense_type: stock (goods for resale) or overhead (rent, utilities, transport, services)
  dates       : issue_date, due_date (= issue_date + payment_terms), paid_date (blank while unpaid)
  status      : paid, open_not_due, open_overdue  (as of the snapshot date)
  invoice     : amount, payment_terms (15/30/60, fixed per supplier)

Salaries and taxes are not invoices, so they are not here.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ml.generate_invoices_ge import (CITIES, END, OUT_DIR, SEED, START, TODAY, is_business_day,
                                     next_business_day, prev_business_day)

BASE_DELAY_DAYS = -2.5                    # days after the due date the company means to pay (negative = before)
CASH_TIGHT_WINDOW = (8, 15)               # the company's own salaries (10th) and taxes (15th) come first
CASH_TIGHT_DELAY_DAYS = 3.0               # extra delay if a bill falls due in that window
PAYMENT_RUN_WEEKDAYS = (1, 4)             # supplier payments go out in batches on Tuesdays and Fridays
PAYMENT_RUN_P = 0.80                      # chance a payment waits for the next batch
DISPUTE_P = 0.02                          # wrong or damaged delivery: payment held back
RESTOCK_WINDOW_DAYS = 28                  # purchases follow sales over the surrounding four weeks
YEARLY_PRICE_RISE = 0.04                  # rent, utilities and services get dearer each year

INVOICES_CSV = OUT_DIR / "invoices_georgia.csv"
OUT_NAME = "outgoing_invoices_georgia.csv"
META_NAME = "outgoing_invoices_metadata.json"
COLUMNS = ["invoice_id", "supplier_id", "supplier_category", "supplier_city", "expense_type",
           "issue_date", "due_date", "paid_date", "status", "amount", "payment_terms"]

# ------------------------------------------------------------------ supplier population
# share     = this category's bills as a share of the company's sales (design assumption)
# n         = number of suppliers;  terms = chance a supplier gives 15 / 30 / 60 days
# per_month = bills per supplier per month, on random working days;  day = one bill a month, around this day
# sigma     = spread of bill amounts;  shift = days the company usually pays this category late
# strict    = paid by direct debit on the due date (late payment means penalties or a cut-off)
CAT = {
    "Beverages":          dict(kind="stock",    share=.210, n=8, per_month=4,  terms=[.10, .50, .40], sigma=.40, shift=1.0),
    "Meat and poultry":   dict(kind="stock",    share=.145, n=6, per_month=8,  terms=[.55, .40, .05], sigma=.40, shift=0.0),
    "Dry goods":          dict(kind="stock",    share=.130, n=7, per_month=3,  terms=[.10, .55, .35], sigma=.40, shift=2.0),
    "Dairy":              dict(kind="stock",    share=.110, n=6, per_month=8,  terms=[.60, .35, .05], sigma=.40, shift=0.0),
    "Produce":            dict(kind="stock",    share=.095, n=8, per_month=10, terms=[.75, .25, .00], sigma=.50, shift=-1.0),
    "Frozen foods":       dict(kind="stock",    share=.055, n=4, per_month=3,  terms=[.20, .60, .20], sigma=.40, shift=1.0),
    "Coffee and tea":     dict(kind="stock",    share=.030, n=3, per_month=2,  terms=[.05, .45, .50], sigma=.40, shift=2.0),
    "Packaging":          dict(kind="stock",    share=.025, n=3, per_month=2,  terms=[.20, .60, .20], sigma=.40, shift=3.0),
    "Fuel and transport": dict(kind="overhead", share=.025, n=3, per_month=2,  terms=[.50, .50, .00], sigma=.30, shift=1.0),
    "Rent":               dict(kind="overhead", share=.010, n=1, day=1,        terms=[1., .00, .00], sigma=.00, strict=True),
    "Utilities":          dict(kind="overhead", share=.006, n=3, day=7,        terms=[1., .00, .00], sigma=.10, strict=True),
    "Services":           dict(kind="overhead", share=.006, n=4, day=25,       terms=[.50, .50, .00], sigma=.05, shift=2.0),
}
STOCK_SHARE = round(sum(c["share"] for c in CAT.values() if c["kind"] == "stock"), 3)
OVERHEAD_SHARE = round(sum(c["share"] for c in CAT.values() if c["kind"] == "overhead"), 3)
SUPPLIER_CITY_P = [.62, .10, .08, .05, .08, .02, .05]                    # suppliers sit mostly in Tbilisi
UTILITY_SEASON = {12: 1.20, 1: 1.20, 2: 1.20, 6: 1.25, 7: 1.25, 8: 1.25}  # heating in winter, cooling in summer


def working_days():
    days = pd.date_range(START, END, freq="D")
    return days[[is_business_day(d) for d in days]]


def sales_profile(invoices_csv=INVOICES_CSV):
    """Total sales, and for every day how busy the surrounding weeks were (1.0 = an average day)."""
    sales = pd.read_csv(invoices_csv, usecols=["issue_date", "amount"], parse_dates=["issue_date"])
    days = pd.date_range(START, END, freq="D")
    daily = sales.groupby("issue_date").amount.sum().reindex(days, fill_value=0.0)
    smooth = daily.rolling(RESTOCK_WINDOW_DAYS, center=True, min_periods=7).mean()
    return float(sales.amount.sum()), smooth / smooth.mean()


def pay_date(rng, sup, issue, due, unusual):
    """The day the company pays one bill. `unusual` is how much bigger than usual the bill is (log scale)."""
    if sup["strict"]:                                                      # direct debit, never late
        return due if is_business_day(due) else prev_business_day(due)
    mu = sup["base_delay"]
    mu += 2.0 * float(np.clip(unusual, -1.5, 1.5))                         # unusually big bill -> paid later
    if CASH_TIGHT_WINDOW[0] <= due.day <= CASH_TIGHT_WINDOW[1]:
        mu += CASH_TIGHT_DELAY_DAYS                                        # own payroll and taxes come first
    if due.month == 1:
        mu += 2.0                                                          # clients pay slowly after the holidays
    delay = rng.normal(mu, sup["delay_sd"])
    if rng.random() < DISPUTE_P:
        delay += rng.uniform(10, 40)
    delay = max(int(round(delay)), -10)
    paid = max(due + pd.Timedelta(days=delay), issue + pd.Timedelta(days=1))
    if rng.random() < PAYMENT_RUN_P:                                       # wait for the next payment batch
        while paid.weekday() not in PAYMENT_RUN_WEEKDAYS or not is_business_day(paid):
            paid += pd.Timedelta(days=1)
        return paid
    return next_business_day(paid)                                         # banks do not move money on days off


def generate(output_dir=OUT_DIR, invoices_csv=INVOICES_CSV):
    rng = np.random.default_rng(SEED)
    out_dir = Path(output_dir)
    invoices_csv = Path(invoices_csv)
    total_sales, busy = sales_profile(invoices_csv)
    open_days = working_days()
    months = pd.date_range(START.replace(day=1), END, freq="MS")

    suppliers = []
    for cat, c in CAT.items():
        for _ in range(c["n"]):
            suppliers.append(dict(
                supplier_id=f"SUP{len(suppliers) + 1:03d}", category=cat, kind=c["kind"],
                city=str(rng.choice(CITIES, p=SUPPLIER_CITY_P)),
                terms=int(rng.choice([15, 30, 60], p=c["terms"])),
                scale=float(np.exp(rng.normal(0, 0.5))),                   # supplier's size within its category
                strict=c.get("strict", False),
                # The company aims to pay a couple of days before the due date; waiting for a
                # payment batch and its own tight days then push many payments past it.
                base_delay=rng.normal(BASE_DELAY_DAYS, 1.5) + c.get("shift", 0.0),
                delay_sd=float(np.clip(rng.gamma(2.0, 1.0), 1.0, 5.0)),
            ))

    # ------------------------------------------------------------------ bills + the company's payment behaviour
    rows = []
    for s in suppliers:
        c = CAT[s["category"]]
        if "day" in c:                                                     # one bill a month, around a fixed day
            dates = [next_business_day(m + pd.Timedelta(days=c["day"] - 1 + int(rng.integers(0, 3)))) for m in months]
        else:                                                              # deliveries on random working days
            n_inv = max(3, int(rng.poisson(c["per_month"] * len(months) * rng.uniform(0.85, 1.15))))
            dates = open_days[rng.integers(0, len(open_days), n_inv)]
        for d in dates:
            if d < START or d > END:
                continue
            noise = rng.normal(0, c["sigma"]) if c["sigma"] else 0.0
            if c["kind"] == "stock" or s["category"] == "Fuel and transport":
                level = busy[d]                                            # buy more when selling more
            else:
                level = (1 + YEARLY_PRICE_RISE) ** (d.year - START.year)
                if s["category"] == "Utilities":
                    level *= UTILITY_SEASON.get(d.month, 1.0)
            due = d + pd.Timedelta(days=s["terms"])
            paid = pay_date(rng, s, d, due, noise)
            rows.append((s["supplier_id"], s["category"], s["city"], s["kind"], d, due, paid,
                         s["scale"] * level * float(np.exp(noise)), s["terms"]))

    inv = pd.DataFrame(rows, columns=["supplier_id", "supplier_category", "supplier_city", "expense_type",
                                      "issue_date", "due_date", "paid_date", "amount", "payment_terms"])
    # Each category's bills add up to its share of sales.
    for cat, c in CAT.items():
        mask = inv.supplier_category == cat
        inv.loc[mask, "amount"] *= c["share"] * total_sales / inv.loc[mask, "amount"].sum()
    inv["amount"] = inv.amount.round(2).clip(lower=20.0)

    inv = inv.sort_values(["issue_date", "supplier_id"], kind="stable").reset_index(drop=True)
    inv.insert(0, "invoice_id", [f"BILL-{i + 1:06d}" for i in range(len(inv))])

    # A payment planned for after the snapshot has not happened yet, so it is not exported.
    inv.loc[inv.paid_date > TODAY, "paid_date"] = pd.NaT
    inv["status"] = np.where(inv.paid_date.notna(), "paid",
                    np.where(inv.due_date >= TODAY, "open_not_due", "open_overdue"))

    # ------------------------------------------------------------------ save
    out = inv[COLUMNS].copy()
    for col in ["issue_date", "due_date", "paid_date"]:
        out[col] = out[col].dt.date
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / OUT_NAME
    out.to_csv(path, index=False)

    paid_rows = inv[inv.status == "paid"]
    days_late = (paid_rows.paid_date - paid_rows.due_date).dt.days
    unpaid = inv[inv.status != "paid"]
    due_30 = unpaid[unpaid.due_date <= TODAY + pd.Timedelta(days=30)].amount.sum()
    print(f"{len(out)} bills, {out.supplier_id.nunique()} suppliers, {out.shape[1]} columns -> {path}")
    print(out.status.value_counts().to_string())
    print("late rate (paid):", round(float((days_late > 0).mean()), 3), "| median days_late:", float(days_late.median()))
    print(f"total billed: GEL {inv.amount.sum():,.2f} = {inv.amount.sum() / total_sales:.1%} of sales "
          f"(stock {STOCK_SHARE:.1%}, overhead {OVERHEAD_SHARE:.1%})")
    print(f"unpaid at snapshot: GEL {unpaid.amount.sum():,.2f} in {len(unpaid)} bills, "
          f"of which GEL {due_30:,.2f} falls due within 30 days")

    metadata = {
        "generator_version": 1, "seed": SEED, "snapshot_date": str(TODAY.date()),
        "snapshot_semantics": "end_of_day", "currency": "GEL", "synthetic": True,
        "rows": len(out), "suppliers": int(out.supplier_id.nunique()),
        "csv_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "incoming_csv_sha256": hashlib.sha256(invoices_csv.read_bytes()).hexdigest(),
        "versions": {"numpy": np.__version__, "pandas": pd.__version__},
        "assumptions": {"stock_share_of_sales": STOCK_SHARE, "overhead_share_of_sales": OVERHEAD_SHARE,
                        "share_of_sales_by_category": {cat: c["share"] for cat, c in CAT.items()},
                        "payment_run_weekdays": list(PAYMENT_RUN_WEEKDAYS),
                        "cash_tight_window": list(CASH_TIGHT_WINDOW)},
        "limitations": ["One simulated company", "Full payments only", "Cost shares are design assumptions",
                        "Payment timing does not react to the company's actual bank balance",
                        "Salaries and taxes are not invoices and are not included",
                        "Not calibrated against real Georgian purchasing records"],
    }
    (out_dir / META_NAME).write_text(json.dumps(metadata, indent=2) + "\n")
    return inv[COLUMNS]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate synthetic outgoing invoices (no real supplier data).")
    parser.add_argument("--output-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--invoices-csv", type=Path, default=INVOICES_CSV,
                        help="the incoming invoices CSV that purchases are sized against")
    args = parser.parse_args()
    generate(args.output_dir, args.invoices_csv)
