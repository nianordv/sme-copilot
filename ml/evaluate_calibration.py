"""Diagnose the frozen model on calibration data, without fitting anything.

Run: python -m ml.evaluate_calibration
No test records are loaded. Existing tuning reports are preserved.
"""

import json

import numpy as np
import pandas as pd

from .evaluate_baseline import HORIZONS, probability_metrics
from .evaluate_model import file_hash
from .evaluate_overdue import RUN, predict_baseline, predict_model
from .evaluate_receipts import load_invoices, load_models, main as receipts


PARTITION = "validation_calibration"


def main(partition=PARTITION, *, allow_test=False):
    ledger, manifest, cutoff = load_invoices(partition, allow_test=allow_test)
    label = "Test" if partition == "test" else "Calibration"
    prefix = "test" if partition == "test" else "calibration"
    model, baseline = load_models(manifest)
    split = manifest["splits"][partition]
    invoices = ledger.loc[
        ledger.invoice_id.isin(split["invoice_ids"])
    ].reset_index(drop=True)
    if invoices.invoice_id.tolist() != split["invoice_ids"]:
        raise ValueError(f"{label} invoice membership changed.")

    results = []
    tables = []
    anchor_definitions = {
        "issue_date": invoices.issue_date,
        "due_date": invoices.due_date,
        "14_days_overdue": invoices.due_date + pd.Timedelta(days=14),
    }

    for name, dates in anchor_definitions.items():
        eligible = (
            (dates <= cutoff)
            & (invoices.paid_date.isna() | (invoices.paid_date > dates))
            & (invoices.writeoff_date.isna() | (invoices.writeoff_date > dates))
        )
        cohort = invoices.loc[eligible].reset_index(drop=True)
        anchors = dates.loc[eligible].reset_index(drop=True)
        if cohort.empty:
            continue

        print(f"\n=== {label}: {name}, {len(cohort):,} invoices ===", flush=True)
        predictions = predict_model(cohort, anchors, model, manifest)
        baseline_predictions = predict_baseline(cohort, anchors, baseline)
        table = cohort[["invoice_id", "client_id", "amount"]].copy()
        table["anchor_type"] = name
        table["anchor_date"] = anchors

        for horizon in HORIZONS:
            deadline = anchors + pd.Timedelta(days=horizon)
            complete = (deadline <= cutoff).to_numpy()
            actual = (
                cohort.paid_date.notna()
                & (cohort.paid_date > anchors)
                & (cohort.paid_date <= deadline)
            ).to_numpy(dtype=float)
            table[f"eligible_{horizon}d"] = complete
            table[f"model_probability_{horizon}d"] = predictions[horizon]
            table[f"baseline_probability_{horizon}d"] = baseline_predictions[horizon]
            table[f"actual_paid_{horizon}d"] = np.where(complete, actual, np.nan)
            if not complete.any():
                results.append({"anchor": name, "horizon_days": horizon,
                                "status": "no_complete_follow_up"})
                continue

            metrics = probability_metrics(actual[complete], predictions[horizon][complete])
            baseline_metrics = probability_metrics(
                actual[complete], baseline_predictions[horizon][complete]
            )
            baseline_brier = baseline_metrics["brier_score"]
            improvement = (
                1 - metrics["brier_score"] / baseline_brier
                if baseline_brier > 0 else None
            )
            metrics["calibration_gap"] = (
                metrics["mean_predicted_probability"] - metrics["actual_payment_fraction"]
            )
            metrics["binned_calibration_error"] = sum(
                b["invoices"] * abs(b["mean_predicted_probability"] - b["actual_payment_fraction"])
                for b in metrics["calibration_bins"]
            ) / metrics["invoices"]
            results.append({"anchor": name, "horizon_days": horizon,
                            "model": metrics, "baseline": baseline_metrics,
                            "relative_brier_improvement": improvement})
            print(
                f"{horizon:2d} days | n={metrics['invoices']:,} | "
                f"Brier={metrics['brier_score']:.4f} | "
                f"baseline={baseline_metrics['brier_score']:.4f} | "
                f"predicted={metrics['mean_predicted_probability']:.1%} | "
                f"actual={metrics['actual_payment_fraction']:.1%} | "
                f"gap={100 * metrics['calibration_gap']:+.2f} percentage points"
            )
        tables.append(table)

    report = {
        "partition": partition,
        "run": RUN.name,
        "observations_through": str(cutoff.date()),
        "source_sha256": manifest["source_sha256"],
        "model_sha256": file_hash(RUN / "model.txt"),
        "calibrated": False,
        "history_definition": "frozen_at_issue",
        "purpose": "Diagnostics of unchanged model; no calibration adjustment fitted.",
        "limitations": [
            "Late-period invoices lack complete follow-up at longer horizons.",
            "Calibration bins with few invoices are uncertain.",
            "Synthetic outcomes do not establish real-world performance.",
        ],
        "results": results,
    }
    issue_results = [r for r in results if r["anchor"] == "issue_date" and "model" in r]
    if issue_results:
        mean_model = float(np.mean([r["model"]["brier_score"] for r in issue_results]))
        mean_baseline = float(np.mean([r["baseline"]["brier_score"] for r in issue_results]))
        report["issue_date_summary"] = {
            "mean_horizon_brier": mean_model,
            "baseline_mean_horizon_brier": mean_baseline,
            "relative_improvement": 1 - mean_model / mean_baseline if mean_baseline > 0 else None,
        }
        print(f"\nIssue-date mean Brier: model={mean_model:.6f}, baseline={mean_baseline:.6f}")
    (RUN / f"{prefix}_diagnostics.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    pd.concat(tables, ignore_index=True).to_csv(
        RUN / f"{prefix}_predictions.csv", index=False
    )
    print(f"\nSaved unchanged-model {label.lower()} diagnostics to:", RUN)
    receipts(partition, allow_test=allow_test)


if __name__ == "__main__":
    main()
