"""Evaluate the first LightGBM model on tuning invoices.

Run:
    python -m ml.evaluate_model

Forecasts start at the end of issue date.
No calibration or test data is loaded.
"""

import hashlib
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from .evaluate_baseline import HORIZONS, probability_metrics
from .prepare_data import calendar_features, static_features


ROOT = Path(__file__).resolve().parent
PREPARED = ROOT / "artifacts" / "prepared"
RUN = ROOT / "artifacts" / "lightgbm" / "first_run"
BASELINE_REPORT = (
    ROOT / "artifacts" / "baseline" / "tuning_evaluation.json"
)

BATCH_SIZE = 1000


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def predict_horizons(invoices, model, manifest):
    """Forecast all 60 future days without looking at actual payments."""

    feature_names = manifest["feature_names"]
    base = static_features(invoices, manifest["categories"])

    if model.feature_name() != feature_names:
        raise ValueError("Model feature order differs from preparation.")

    days = np.arange(1, max(HORIZONS) + 1)
    predictions = {
        horizon: np.empty(len(invoices), dtype=np.float64)
        for horizon in HORIZONS
    }

    for start in range(0, len(invoices), BATCH_SIZE):
        stop = min(start + BATCH_SIZE, len(invoices))
        batch = invoices.iloc[start:stop]

        issue = batch["issue_date"].to_numpy(
            dtype="datetime64[D]"
        )
        due = batch["due_date"].to_numpy(
            dtype="datetime64[D]"
        )

        # Make the full forecast grid for every invoice.
        # Do not stop at actual payment or use its date.
        candidate = (
            issue[:, None]
            + days[None, :].astype("timedelta64[D]")
        )

        daily = calendar_features(
            candidate.reshape(-1),
            "candidate",
        )

        daily.insert(
            0,
            "days_past_due",
            (candidate - due[:, None]).astype(int).reshape(-1),
        )
        daily.insert(
            0,
            "invoice_age",
            np.tile(days, len(batch)),
        )

        batch_base = base.iloc[start:stop]
        assembled_names = [
            *batch_base.columns,
            *daily.columns,
        ]

        if assembled_names != feature_names:
            raise ValueError("Prediction features are in the wrong order.")

        x = np.column_stack([
            np.repeat(
                batch_base.to_numpy(
                    dtype=np.float32,
                    na_value=np.nan,
                ),
                len(days),
                axis=0,
            ),
            daily.to_numpy(dtype=np.float32),
        ])

        hazards = model.predict(
            x,
            num_threads=1,
        ).reshape(len(batch), len(days))

        if not (
            np.isfinite(hazards).all()
            and (hazards >= 0).all()
            and (hazards <= 1).all()
        ):
            raise ValueError("Invalid predicted payment probabilities.")

        cumulative_payment = (
            1.0 - np.cumprod(1.0 - hazards, axis=1)
        )

        for horizon in HORIZONS:
            predictions[horizon][start:stop] = (
                cumulative_payment[:, horizon - 1]
            )

    return predictions


def evaluate(run=RUN):
    run = Path(run)
    manifest_path = PREPARED / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    metadata = json.loads((run / "metadata.json").read_text())
    baseline = json.loads(BASELINE_REPORT.read_text())

    if metadata["source_sha256"] != manifest["source_sha256"]:
        raise ValueError("Model and prepared data use different datasets.")

    if file_hash(manifest_path) != metadata["prepared_manifest_sha256"]:
        raise ValueError("Preparation changed after training.")

    model_path = run / "model.txt"

    if file_hash(model_path) != metadata["model_sha256"]:
        raise ValueError("Saved model differs from its metadata.")

    split = manifest["splits"]["validation_tuning"]
    cutoff = pd.Timestamp(split["observations_through"])

    if (
        baseline["source_sha256"] != manifest["source_sha256"]
        or baseline["partition"] != "validation_tuning"
        or baseline["forecast_anchor"] != "end_of_issue_date"
        or baseline["observations_through"] != str(cutoff.date())
    ):
        raise ValueError("Baseline evaluation is not comparable.")

    invoices = pd.read_csv(
        PREPARED / "validation_tuning_invoices.csv",
        dtype={"invoice_id": str, "client_id": str},
        parse_dates=["issue_date", "due_date", "paid_date"],
    )

    if invoices["invoice_id"].tolist() != split["invoice_ids"]:
        raise ValueError("Tuning invoice membership or order changed.")

    observed_paid = invoices["paid_date"].where(
        invoices["paid_date"] <= cutoff
    )

    model = lgb.Booster(model_file=str(model_path))

    print("Predicting tuning payment horizons...", flush=True)
    predictions = predict_horizons(invoices, model, manifest)

    report = {
        "model": "LightGBM daily payment hazard",
        "run_name": metadata["run_name"],
        "partition": "validation_tuning",
        "forecast_anchor": "end_of_issue_date",
        "observations_through": str(cutoff.date()),
        "source_sha256": manifest["source_sha256"],
        "model_sha256": metadata["model_sha256"],
        "calibrated": False,
        "horizons": {},
    }

    prediction_table = invoices[
        ["invoice_id", "issue_date", "amount"]
    ].copy()

    for horizon in HORIZONS:
        deadline = (
            invoices["issue_date"]
            + pd.Timedelta(days=horizon)
        )
        eligible = (deadline <= cutoff).to_numpy()

        actual = (
            observed_paid.notna()
            & (observed_paid > invoices["issue_date"])
            & (observed_paid <= deadline)
        ).to_numpy(dtype=np.float64)

        if not eligible.any():
            raise ValueError(f"No eligible {horizon}-day invoices.")

        predicted = predictions[horizon]
        metrics = probability_metrics(
            actual[eligible],
            predicted[eligible],
        )

        baseline_metrics = baseline["horizons"][str(horizon)]

        if metrics["invoices"] != baseline_metrics["invoices"]:
            raise ValueError("Model and baseline cohorts differ in size.")

        baseline_brier = baseline_metrics["brier_score"]
        improvement = (
            (baseline_brier - metrics["brier_score"])
            / baseline_brier
        )

        metrics["baseline_brier"] = baseline_brier
        metrics["relative_brier_improvement"] = float(improvement)
        report["horizons"][str(horizon)] = metrics

        prediction_table[f"probability_{horizon}d"] = predicted
        prediction_table[f"eligible_{horizon}d"] = eligible

        # No outcome label for invoices lacking complete follow-up.
        prediction_table[f"actual_paid_{horizon}d"] = np.where(
            eligible, actual, np.nan
        )

        print(
            f"{horizon:2d} days | "
            f"invoices={metrics['invoices']:,} | "
            f"Brier={metrics['brier_score']:.4f} | "
            f"baseline={baseline_brier:.4f} | "
            f"improvement={improvement:+.1%} | "
            f"predicted={metrics['mean_predicted_probability']:.1%} | "
            f"actual={metrics['actual_payment_fraction']:.1%}"
        )

    mean_brier = float(np.mean([
        metrics["brier_score"]
        for metrics in report["horizons"].values()
    ]))
    baseline_mean = float(baseline["mean_horizon_brier"])
    improvement = (baseline_mean - mean_brier) / baseline_mean

    report["mean_horizon_brier"] = mean_brier
    report["baseline_mean_horizon_brier"] = baseline_mean
    report["relative_mean_brier_improvement"] = float(improvement)

    (run / "tuning_evaluation.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    prediction_table.to_csv(
        run / "tuning_predictions.csv",
        index=False,
    )

    print(f"\nModel mean Brier: {mean_brier:.6f}")
    print(f"Baseline mean Brier: {baseline_mean:.6f}")
    print(f"Relative improvement: {improvement:+.2%}")
    print("Saved evaluation and invoice predictions to:", run)

    return report


if __name__ == "__main__":
    evaluate()
