"""Frozen-model synthetic sensitivity checks; never select favorable scenarios.

Run: python -m ml.check_robustness
Outputs are isolated under ml/artifacts/robustness. No LightGBM fitting.
"""

import json
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

from .baseline import fit_baseline
from .evaluate_model import file_hash
from .evaluate_overdue import PREPARED, RUN, predict_baseline, predict_model
from .evaluate_receipts import load_models, outstanding_at, actual_receipts, receipt_metrics
from .generate_invoices_ge import generate, TODAY, next_business_day
from .prepare_data import load_invoices, TAIL_DAYS


OUTPUT = RUN.parent.parent / "robustness"
HORIZONS = (30, 60)
SCENARIOS = [
    ("seed_2027", 2027, "normal"),
    ("seed_2028", 2028, "normal"),
    ("seed_2029", 2029, "normal"),
    ("slower_payments", 2027, "delay"),
    ("disputes_nonpayment", 2027, "dispute"),
    ("shared_disruption", 2027, "shock"),
]


def perturb(frame, kind, seed):
    """Explicit hypothetical 2026 shocks, not estimated real frequencies."""
    inv = frame.copy()
    for col in ("issue_date", "due_date", "paid_date", "writeoff_date"):
        inv[col] = pd.to_datetime(inv[col])
    rng = np.random.default_rng(seed + 10000)
    changed = pd.Series(False, index=inv.index)
    issued_2026 = inv.issue_date >= pd.Timestamp("2026-01-01")
    paid = inv.paid_date.notna()
    if kind == "delay":
        changed = issued_2026 & paid
        inv.loc[changed, "paid_date"] += pd.Timedelta(days=14)
    elif kind == "dispute":
        draw = rng.random(len(inv))
        never = issued_2026 & paid & (draw < .03)
        delayed = issued_2026 & paid & (draw >= .03) & (draw < .09)
        inv.loc[never, "paid_date"] = pd.NaT
        inv.loc[never, "writeoff_date"] = inv.loc[never, "due_date"] + pd.Timedelta(days=90)
        inv.loc[delayed, "paid_date"] += pd.Timedelta(days=45)
        changed = never | delayed
    elif kind == "shock":
        changed = inv.paid_date.between("2026-06-01", "2026-07-15")
        inv.loc[changed, "paid_date"] += pd.Timedelta(days=21)
    elif kind != "normal":
        raise ValueError("Unknown scenario")
    inv.loc[changed & inv.paid_date.notna(), "paid_date"] = (
        inv.loc[changed & inv.paid_date.notna(), "paid_date"].map(next_business_day)
    )
    for col in ("paid_date", "writeoff_date"):
        inv[col] = inv[col].where(inv[col] <= TODAY)
    inv["status"] = np.where(inv.paid_date.notna(), "paid",
        np.where(inv.writeoff_date.notna(), "written_off",
        np.where(inv.due_date >= TODAY, "open_not_due", "open_overdue")))
    # Drop derived target/history/calendar exports. load_invoices rebuilds history.
    columns = ["invoice_id", "client_id", "client_segment", "client_city",
               "issue_date", "due_date", "paid_date", "writeoff_date",
               "status", "amount", "payment_terms"]
    return inv[columns], int(changed.sum())


def scenario_baseline(ledger, directory):
    """Same baseline algorithm; fit only this population's 2022–24 history."""
    training = ledger.loc[ledger.issue_date < "2025-01-01"].reset_index(drop=True)
    issue = training.issue_date.to_numpy(dtype="datetime64[D]")
    due = training.due_date.to_numpy(dtype="datetime64[D]")
    paid = training.paid_date.to_numpy(dtype="datetime64[D]")
    end = np.minimum(due + np.timedelta64(TAIL_DAYS, "D"), np.datetime64("2024-12-31"))
    observed = ~np.isnat(paid) & (paid <= end)
    end = np.where(observed, paid, end)
    counts = np.maximum((end - issue).astype(int), 0)
    index = np.repeat(np.arange(len(training)), counts)
    age = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts) + 1
    candidates = issue[index] + age.astype("timedelta64[D]")
    y = (observed[index] & (candidates == paid[index])).astype(np.uint8)
    # Baseline needs only daily age, labels and invoice references, not 36 features.
    with tempfile.TemporaryDirectory() as temporary:
        p = Path(temporary)
        training.to_csv(p / "train_invoices.csv", index=False)
        np.savez_compressed(p / "train.npz", X=age[:, None].astype(np.float32),
            y=y, invoice_index=index, invoice_id=training.invoice_id.to_numpy(dtype=str))
        manifest = {"feature_names": ["invoice_age"], "tail_days_past_due": TAIL_DAYS,
            "source_sha256": file_hash(directory / "scenario_invoices.csv"),
            "splits": {"train": {"invoices": len(training), "observations_through": "2024-12-31"}}}
        (p / "manifest.json").write_text(json.dumps(manifest))
        fit_baseline(prepared=p, output=directory / "baseline")
        # Preserve the lightweight training manifest referenced by baseline metadata.
        (directory / "baseline" / "training_manifest.json").write_text(json.dumps(manifest, indent=2))
    with np.load(directory / "baseline" / "hazards.npz", allow_pickle=False) as saved:
        return {key: saved[key].copy() for key in saved.files}


def block_interval(table, horizon):
    """Approximate paired moving-block bootstrap; weekly windows overlap."""
    s = table[(table.portfolio_size == "all") & (table.horizon_days == horizon)]
    errors = np.column_stack([s.model_error_gel.abs(), s.baseline_error_gel.abs()])
    rng = np.random.default_rng(31415)
    results = {}
    for block in (5, 9):
        draws = []
        for _ in range(1000):
            starts = rng.integers(0, len(errors) - block + 1,
                                  size=int(np.ceil(len(errors) / block)))
            sample = np.concatenate([errors[start:start + block] for start in starts])[:len(errors)]
            mean = sample.mean(axis=0)
            if mean[1] > 0:
                draws.append(1 - mean[0] / mean[1])
        results[str(block)] = np.quantile(draws, [.025, .975]).tolist()
    return results


def evaluate_scenario(ledger, model, baseline, manifest):
    clients = ledger.client_id.unique().copy()
    np.random.default_rng(9001).shuffle(clients)
    groups = {size: {cid: i // size for i, cid in enumerate(clients)} for size in (10, 50)}
    rows = []
    for anchor in pd.date_range("2026-01-01", TODAY - pd.Timedelta(days=30), freq="7D"):
        cohort = outstanding_at(ledger, anchor)
        anchors = pd.Series(anchor, index=cohort.index)
        mp = predict_model(cohort, anchors, model, manifest)
        bp = predict_baseline(cohort, anchors, baseline)
        for horizon in HORIZONS:
            if anchor + pd.Timedelta(days=horizon) > TODAY:
                continue
            actual = np.where(cohort.paid_date.notna() & (cohort.paid_date > anchor)
                & (cohort.paid_date <= anchor + pd.Timedelta(days=horizon)), cohort.amount, 0.)
            model_cash = cohort.amount.to_numpy() * mp[horizon]
            baseline_cash = cohort.amount.to_numpy() * bp[horizon]
            selections = [("all", 0, np.ones(len(cohort), dtype=bool))]
            for size, mapping in groups.items():
                codes = cohort.client_id.map(mapping).to_numpy()
                selections.extend((str(size), int(code), codes == code) for code in np.unique(codes))
            for size, group, selected in selections:
                a, m, b = (float(v[selected].sum()) for v in (actual, model_cash, baseline_cash))
                rows.append({"anchor_date": str(anchor.date()), "horizon_days": horizon,
                    "portfolio_size": size, "portfolio_group": group,
                    "outstanding_invoices": int(selected.sum()),
                    "actual_receipts_gel": a, "model_receipts_gel": m, "baseline_receipts_gel": b,
                    "model_error_gel": m-a, "baseline_error_gel": b-a})
        print(f"  Forecasted {anchor.date()}", flush=True)
    table = pd.DataFrame(rows)
    summary = []
    for (size, horizon), s in table.groupby(["portfolio_size", "horizon_days"]):
        a = s.actual_receipts_gel.to_numpy()
        m = receipt_metrics(a, s.model_receipts_gel.to_numpy())
        b = receipt_metrics(a, s.baseline_receipts_gel.to_numpy())
        improvement = 1-m["mae_gel"]/b["mae_gel"] if b["mae_gel"] else None
        summary.append({"portfolio_size": size, "horizon_days": int(horizon),
            "model": m, "baseline": b, "relative_mae_improvement": improvement})
        print(f"  {size} customers | {horizon}d | model MAE={m['mae_gel']:,.2f} | "
              f"baseline MAE={b['mae_gel']:,.2f} | improvement={improvement:+.2%}", flush=True)
    return table, summary, {str(h): block_interval(table, h) for h in HORIZONS}


def main():
    manifest = json.loads((PREPARED / "manifest.json").read_text())
    model, _ = load_models(manifest)
    selection = json.loads((RUN / "final_selection.json").read_text())
    if selection["model_sha256"] != file_hash(RUN / "model.txt") or selection["calibrated"]:
        raise ValueError("Frozen model selection changed.")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = {"model_sha256": selection["model_sha256"], "plan": SCENARIOS,
        "lightgbm_retrained": False,
        "baseline_policy": "Fixed algorithm refitted on each new population's pre-2025 records; no 2026 fitting.",
        "portfolio_policy": "Deterministic disjoint customer groups; subsets, not independently generated businesses.",
        "stress_policy": "Hypothetical 2026 perturbations of observed settlements; original snapshot-censored payments remain unknown.",
        "limitations": ["Same generator family; not real-data validation.",
            "Stress magnitudes are assumptions, not empirical estimates.",
            "Bootstrap intervals are approximate with limited correlated dates; 5- and 9-week sensitivity reported.",
            "Small-portfolio aggregate metrics pool dependent group/date observations."],
        "scenarios": []}
    (OUTPUT / "robustness_plan.json").write_text(json.dumps(report, indent=2) + "\n")
    for name, seed, kind in SCENARIOS:
        directory = OUTPUT / name
        directory.mkdir(exist_ok=True)
        print(f"\n=== {name}: seed {seed}, {kind} ===", flush=True)
        generated = generate(directory / "generated", seed=seed)
        scenario, changed = perturb(generated, kind, seed)
        scenario.to_csv(directory / "scenario_invoices.csv", index=False)
        ledger = load_invoices(directory / "scenario_invoices.csv")
        baseline = scenario_baseline(ledger, directory)
        table, metrics, intervals = evaluate_scenario(ledger, model, baseline, manifest)
        table.to_csv(directory / "receipt_predictions.csv", index=False)
        result = {"scenario": name, "seed": seed, "perturbation": kind,
            "changed_invoice_outcomes": changed,
            "dataset_sha256": file_hash(directory / "scenario_invoices.csv"),
            "metrics": metrics, "full_portfolio_relative_improvement_intervals": intervals}
        (directory / "evaluation.json").write_text(json.dumps(result, indent=2, allow_nan=False)+"\n")
        report["scenarios"].append(result)
        (OUTPUT / "robustness_evaluation.json").write_text(json.dumps(report, indent=2, allow_nan=False)+"\n")
    assert file_hash(RUN / "model.txt") == selection["model_sha256"]
    print("\nAll six scenarios completed. Saved:", OUTPUT / "robustness_evaluation.json")


if __name__ == "__main__":
    main()
