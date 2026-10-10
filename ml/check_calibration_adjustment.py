"""Trial daily-logit calibration: October fit, later invoices for checking.

Run: python -m ml.check_calibration_adjustment
Does not retrain LightGBM, read test records, or activate the adjustment.
"""

import json

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from .evaluate_baseline import HORIZONS, probability_metrics
from .evaluate_model import file_hash
from .evaluate_overdue import PREPARED, RUN, predict_model
from .evaluate_receipts import load_invoices, load_models


PARTITION = "validation_calibration"
FIT_CUTOFF = pd.Timestamp("2025-10-31")


def logits(probabilities):
    safe = np.clip(probabilities, 1e-6, 1 - 1e-6)
    return np.log(safe / (1 - safe))


class AdjustedModel:
    """Apply one monotonic logistic correction to each daily hazard."""

    def __init__(self, model, adjustment):
        self.model = model
        self.adjustment = adjustment

    def feature_name(self):
        return self.model.feature_name()

    def predict(self, x, **kwargs):
        raw = self.model.predict(x, **kwargs)
        return self.adjustment.predict_proba(logits(raw).reshape(-1, 1))[:, 1]


def main():
    ledger, manifest, cutoff = load_invoices(PARTITION)
    model, _ = load_models(manifest)
    ids = manifest["splits"][PARTITION]["invoice_ids"]
    invoices = ledger.loc[ledger.invoice_id.isin(ids)].reset_index(drop=True)
    if invoices.invoice_id.tolist() != ids:
        raise ValueError("Calibration invoice order changed.")

    path = PREPARED / f"{PARTITION}.npz"
    with np.load(path, allow_pickle=False) as data:
        if data["invoice_id"].tolist() != ids:
            raise ValueError("Calibration arrays have different invoices.")
        x = data["X"]
        y = data["y"]
        index = data["invoice_index"]
        dates = data["candidate_date"]

    issue_dates = invoices.issue_date.to_numpy(dtype="datetime64[D]")
    fit_cutoff = np.datetime64(FIT_CUTOFF.date(), "D")
    fit = (issue_dates[index] <= fit_cutoff) & (dates <= fit_cutoff)
    check = issue_dates[index] > fit_cutoff
    if not fit.any() or not check.any():
        raise ValueError("Empty fitting or checking cohort.")
    if set(index[fit]) & set(index[check]):
        raise ValueError("An invoice appears in both cohorts.")

    print("Predicting calibration daily hazards...", flush=True)
    raw = model.predict(x, num_threads=1)
    adjustment = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000)
    adjustment.fit(logits(raw[fit]).reshape(-1, 1), y[fit])
    slope = float(adjustment.coef_[0, 0])
    intercept = float(adjustment.intercept_[0])
    if adjustment.n_iter_[0] >= adjustment.max_iter or slope <= 0:
        raise ValueError("Calibration did not converge to a positive slope.")
    corrected = adjustment.predict_proba(logits(raw[check]).reshape(-1, 1))[:, 1]
    daily_raw = probability_metrics(y[check], raw[check])
    daily_adjusted = probability_metrics(y[check], corrected)
    # The shared helper calls rows 'invoices'; these are daily observations.
    for metrics in (daily_raw, daily_adjusted):
        metrics["daily_observations"] = metrics.pop("invoices")
        metrics.pop("calibration_bins")

    print(f"October fit: {fit.sum():,} daily rows, {int(y[fit].sum()):,} payments")
    print(f"Adjustment: slope={slope:.4f}, intercept={intercept:+.4f}")
    print(
        f"Held-out daily log loss: raw={daily_raw['log_loss']:.6f}, "
        f"adjusted={daily_adjusted['log_loss']:.6f}"
    )

    holdout = invoices.loc[invoices.issue_date > FIT_CUTOFF].reset_index(drop=True)
    wrapper = AdjustedModel(model, adjustment)
    results = []
    tables = []
    for anchor_name, anchor_dates in {
        "issue_date": holdout.issue_date,
        "due_date": holdout.due_date,
    }.items():
        selected = (
            (anchor_dates <= cutoff)
            & (holdout.paid_date.isna() | (holdout.paid_date > anchor_dates))
            & (holdout.writeoff_date.isna() | (holdout.writeoff_date > anchor_dates))
        )
        cohort = holdout.loc[selected].reset_index(drop=True)
        anchors = anchor_dates.loc[selected].reset_index(drop=True)
        if cohort.empty:
            continue
        unadjusted = predict_model(cohort, anchors, model, manifest)
        adjusted = predict_model(cohort, anchors, wrapper, manifest)
        table = cohort[["invoice_id", "client_id", "amount"]].copy()
        table["anchor_type"] = anchor_name
        table["anchor_date"] = anchors
        print(f"\n=== Held-out {anchor_name} ===", flush=True)
        for horizon in HORIZONS:
            deadline = anchors + pd.Timedelta(days=horizon)
            eligible = (deadline <= cutoff).to_numpy()
            actual = (
                cohort.paid_date.notna() & (cohort.paid_date > anchors)
                & (cohort.paid_date <= deadline)
            ).to_numpy(dtype=float)
            table[f"raw_probability_{horizon}d"] = unadjusted[horizon]
            table[f"adjusted_probability_{horizon}d"] = adjusted[horizon]
            table[f"actual_paid_{horizon}d"] = np.where(eligible, actual, np.nan)
            if not eligible.any():
                continue
            before = probability_metrics(actual[eligible], unadjusted[horizon][eligible])
            after = probability_metrics(actual[eligible], adjusted[horizon][eligible])
            results.append({"anchor": anchor_name, "horizon_days": horizon,
                            "raw": before, "adjusted": after})
            print(
                f"{horizon:2d} days | n={int(eligible.sum()):,} | "
                f"Brier raw={before['brier_score']:.5f}, adjusted={after['brier_score']:.5f} | "
                f"predicted raw={before['mean_predicted_probability']:.1%}, "
                f"adjusted={after['mean_predicted_probability']:.1%} | "
                f"actual={before['actual_payment_fraction']:.1%}"
            )
        tables.append(table)

    report = {
        "status": "experimental_not_activated",
        "model_sha256": file_hash(RUN / "model.txt"),
        "prepared_manifest_sha256": file_hash(PREPARED / "manifest.json"),
        "calibration_arrays_sha256": file_hash(path),
        "fit_observations_through": str(FIT_CUTOFF.date()),
        "check_invoice_issue_dates": "2025-11-01 through 2025-12-31",
        "check_observations_through": str(cutoff.date()),
        "method": "logistic daily hazard correction; C=1; no class weighting",
        "slope": slope,
        "intercept": intercept,
        "fit_daily_rows": int(fit.sum()),
        "fit_payment_events": int(y[fit].sum()),
        "daily_check": {"raw": daily_raw, "adjusted": daily_adjusted},
        "horizons": results,
        "limitations": [
            "Checking invoices are disjoint from fitting invoices; customers can overlap.",
            "Long horizons have limited complete follow-up.",
            "No cash-receipt or robustness acceptance decision is made by this script.",
            "This remains development evidence, not final test evidence.",
        ],
    }
    (RUN / "calibration_adjustment_trial.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    pd.concat(tables, ignore_index=True).to_csv(
        RUN / "calibration_adjustment_trial_predictions.csv", index=False
    )
    print("\nSaved experimental adjustment diagnostics; original model unchanged.")


if __name__ == "__main__":
    main()
