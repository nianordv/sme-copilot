"""Prepare chronological invoice splits and binary daily-hazard training arrays.

Run from the repository root: python -m ml.prepare_data
No model fitting or test-score inspection happens here.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .generate_invoices_ge import (
    PAYROLL_WINDOW, SALARY_DAY, build_history_features, georgia_holidays,
    next_business_day,
)

ROOT = Path(__file__).resolve().parent
SPLITS = {
    "train": ("2022-01-01", "2025-01-01", "2024-12-31"),
    "validation_tuning": ("2025-01-01", "2025-10-01", "2025-09-30"),
    "validation_calibration": ("2025-10-01", "2026-01-01", "2025-12-31"),
    "test": ("2026-01-01", "2026-10-01", "2026-10-03"),
}
TAIL_DAYS = 365
CATEGORICAL = ["client_segment", "client_city"]
HISTORY = [
    "amount_vs_client_usual", "client_num_past_invoices", "client_avg_delay",
    "client_median_delay", "client_delay_spread", "last_delay",
    "avg_of_last_3_delays", "delay_trend", "client_pct_paid_late",
    "client_other_open_invoices", "client_total_outstanding",
]
STATIC = ["amount", "payment_terms", "issue_month", *HISTORY]


def calendar_features(dates, prefix):
    dates = pd.Series(pd.to_datetime(dates)).reset_index(drop=True)
    holidays = {
        pd.Timestamp(d) for year in dates.dt.year.unique()
        for d in georgia_holidays(int(year))
    }
    next_salary = (dates + pd.offsets.MonthBegin(1)).dt.normalize()
    next_salary += pd.Timedelta(days=SALARY_DAY - 1)
    salary_distance = np.where(
        dates.dt.day <= SALARY_DAY, SALARY_DAY - dates.dt.day,
        (next_salary - dates).dt.days,
    )
    # Compute business-day gaps once per distinct date, not per daily row.
    gaps = {d: (next_business_day(pd.Timestamp(d)) - pd.Timestamp(d)).days
            for d in dates.unique()}
    return pd.DataFrame({
        f"{prefix}_weekday": dates.dt.weekday,
        f"{prefix}_month": dates.dt.month,
        f"{prefix}_is_weekend": (dates.dt.weekday >= 5).astype(int),
        f"{prefix}_is_holiday": dates.isin(holidays).astype(int),
        f"{prefix}_is_month_end": (dates.dt.day >= dates.dt.days_in_month - 2).astype(int),
        f"{prefix}_is_salary_day": (dates.dt.day == SALARY_DAY).astype(int),
        f"{prefix}_days_to_salary_day": salary_distance,
        f"{prefix}_in_payroll_window": dates.dt.day.between(*PAYROLL_WINDOW).astype(int),
        f"{prefix}_gap_to_business_day": dates.map(gaps),
    })


def load_invoices(path):
    inv = pd.read_csv(path, parse_dates=["issue_date", "due_date", "paid_date", "writeoff_date"])
    inv = inv.sort_values(["issue_date", "invoice_id"]).reset_index(drop=True)
    if inv.invoice_id.duplicated().any() or inv.client_id.isna().any():
        raise ValueError("Invoice IDs must be unique and client IDs present.")
    if (inv.amount <= 0).any() or not inv.payment_terms.isin([15, 30, 60]).all():
        raise ValueError("Invalid amount or payment terms.")
    if not ((inv.due_date - inv.issue_date).dt.days == inv.payment_terms).all():
        raise ValueError("Due dates disagree with payment terms.")
    if (inv.paid_date.notna() & (inv.paid_date <= inv.issue_date)).any():
        raise ValueError("Payment must follow issue date.")
    # Ignore exported history and targets: rebuild from observable ledger events.
    inv["days_late"] = (inv.paid_date - inv.due_date).dt.days
    history = build_history_features(inv)
    for col in HISTORY:
        inv[col] = history[col]
    inv["issue_month"] = inv.issue_date.dt.month
    return inv


def fit_categories(training):
    return {col: sorted(training[col].dropna().unique().tolist()) for col in CATEGORICAL}


def static_features(inv, categories):
    features = inv[STATIC].reset_index(drop=True).copy()
    for col in CATEGORICAL:
        # LightGBM treats negative category codes as missing/unseen.
        features[col] = pd.Categorical(inv[col], categories=categories[col]).codes
    return pd.concat([features, calendar_features(inv.due_date, "due")], axis=1)


def daily_rows(inv, cutoff):
    """Include event day, censor at cutoff, and never add rows after payment."""
    issue = inv.issue_date.to_numpy(dtype="datetime64[D]")
    due = inv.due_date.to_numpy(dtype="datetime64[D]")
    paid = inv.paid_date.to_numpy(dtype="datetime64[D]")
    cutoff = np.datetime64(cutoff, "D")
    end = np.minimum(due + np.timedelta64(TAIL_DAYS, "D"), cutoff)
    event_observed = ~np.isnat(paid) & (paid <= end)
    end = np.where(event_observed, paid, end)
    counts = np.maximum((end - issue).astype(int), 0)
    invoice_index = np.repeat(np.arange(len(inv), dtype=np.int32), counts)
    offsets = np.repeat(np.cumsum(counts) - counts, counts)
    age = np.arange(counts.sum()) - offsets + 1
    candidate = issue[invoice_index] + age.astype("timedelta64[D]")
    label = (event_observed[invoice_index] & (candidate == paid[invoice_index])).astype(np.uint8)
    daily = calendar_features(candidate, "candidate")
    daily.insert(0, "days_past_due", (candidate - due[invoice_index]).astype(int))
    daily.insert(0, "invoice_age", age)
    return invoice_index, candidate, daily, label


def prepare(source, output):
    metadata_path = source.with_name("dataset_metadata.json")
    metadata = json.loads(metadata_path.read_text())
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if source_hash != metadata["csv_sha256"]:
        raise ValueError("CSV differs from its generation metadata. Regenerate or review it first.")
    inv = load_invoices(source)
    snapshot = pd.Timestamp(metadata["snapshot_date"])
    for col in ["issue_date", "paid_date", "writeoff_date"]:
        if (inv[col] > snapshot).any():
            raise ValueError(f"{col} contains an event after the snapshot.")
    output.mkdir(parents=True, exist_ok=True)
    train_start, train_end, _ = SPLITS["train"]
    train = inv[inv.issue_date.ge(train_start) & inv.issue_date.lt(train_end)]
    categories = fit_categories(train)
    all_static = static_features(inv, categories)
    manifest = {"source_sha256": source_hash, "snapshot_date": metadata["snapshot_date"],
                "tail_days_past_due": TAIL_DAYS, "categories": categories, "splits": {}}
    assigned = set()
    for name, (start, stop, cutoff) in SPLITS.items():
        if pd.Timestamp(cutoff) > snapshot:
            raise ValueError("Split cutoff exceeds the available snapshot.")
        mask = inv.issue_date.ge(start) & inv.issue_date.lt(stop)
        part = inv.loc[mask].reset_index(drop=True)
        ids = part.invoice_id.tolist()
        if assigned.intersection(ids):
            raise ValueError("An invoice belongs to multiple splits.")
        assigned.update(ids)
        invoice_index, candidate, daily, y = daily_rows(part, cutoff)
        base = all_static.loc[mask].reset_index(drop=True)
        feature_names = [*base.columns, *daily.columns]
        x = np.column_stack([
            base.to_numpy(dtype=np.float32, na_value=np.nan)[invoice_index],
            daily.to_numpy(dtype=np.float32),
        ])
        np.savez_compressed(output / f"{name}.npz", X=x, y=y,
                            invoice_index=invoice_index, candidate_date=candidate,
                            invoice_id=np.array(ids, dtype=str))
        # Useful for evaluation joins; outcome fields never enter X.
        part.to_csv(output / f"{name}_invoices.csv", index=False)
        summary = {"start_inclusive": start, "stop_exclusive": stop,
                   "observations_through": cutoff, "invoices": len(part),
                   "daily_rows": len(y), "payment_events": int(y.sum()),
                   "invoice_ids": ids}
        manifest["splits"][name] = summary
        print(f"{name}: {len(part):,} invoices; {len(y):,} daily rows; {y.sum():,} payments")
    if len(assigned) != len(inv):
        raise ValueError("Some invoices were not assigned to a split.")
    manifest["feature_names"] = feature_names
    manifest["categorical_features"] = CATEGORICAL
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/invoices_georgia.csv")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/prepared")
    args = parser.parse_args()
    prepare(args.source, args.output)
