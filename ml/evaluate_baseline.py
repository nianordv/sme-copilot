"""Evaluate the baseline on tuning invoices at their issue date.

Run:
    python -m ml.evaluate_baseline

Does not read calibration or test data.
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
PREPARED = ROOT / "artifacts" / "prepared"
BASELINE = ROOT / "artifacts" / "baseline"

HORIZONS = [7, 14, 30, 60]


def predict_payment_probabilities(invoices, baseline):
    """Return payment-within-H probabilities for each invoice."""

    terms = baseline["terms"]
    clients = baseline["clients"]
    term_hazard = baseline["term_hazard"]
    client_hazard = baseline["client_hazard"]

    term_lookup = {
        int(term): index for index, term in enumerate(terms)
    }
    client_lookup = {
        str(client): index for index, client in enumerate(clients)
    }

    term_index = invoices["payment_terms"].map(term_lookup)

    if term_index.isna().any():
        raise ValueError("Unsupported payment terms.")

    term_index = term_index.to_numpy(dtype=np.int32)

    client_index = (
        invoices["client_id"]
        .map(client_lookup)
        .fillna(-1)
        .to_numpy(dtype=np.int32)
    )

    # We forecast the days after issue: ages 1 through 60.
    ages = np.arange(1, max(HORIZONS) + 1)

    # Start with term-level hazards for every invoice.
    hazards = term_hazard[
        term_index[:, None],
        ages[None, :],
    ].copy()

    # Replace with customer estimates when training history exists.
    known_client = client_index >= 0

    hazards[known_client] = client_hazard[
        client_index[known_client, None, None],
        term_index[known_client, None, None],
        ages[None, :, None],
    ].squeeze(-1)

    if not (
        np.isfinite(hazards).all()
        and (hazards >= 0).all()
        and (hazards <= 1).all()
    ):
        raise ValueError("Invalid daily probabilities.")

    # Surviving unpaid through day H requires no payment on every day.
    survival = np.cumprod(1.0 - hazards, axis=1)
    cumulative_payment = 1.0 - survival

    return {
        horizon: cumulative_payment[:, horizon - 1]
        for horizon in HORIZONS
    }


def probability_metrics(actual, predicted):
    """Probability-quality metrics, rather than classification accuracy."""

    # Clip only for logarithms; preserve original probabilities for Brier.
    safe = np.clip(predicted, 1e-6, 1.0 - 1e-6)

    brier = np.mean((predicted - actual) ** 2)
    log_loss = -np.mean(
        actual * np.log(safe)
        + (1.0 - actual) * np.log(1.0 - safe)
    )

    bins = []

    for index in range(10):
        lower = index / 10
        upper = (index + 1) / 10

        selected = predicted >= lower

        if index == 9:
            selected &= predicted <= upper
        else:
            selected &= predicted < upper

        if selected.any():
            bins.append({
                "probability_range": [lower, upper],
                "invoices": int(selected.sum()),
                "mean_predicted_probability": float(
                    predicted[selected].mean()
                ),
                "actual_payment_fraction": float(
                    actual[selected].mean()
                ),
            })

    return {
        "invoices": len(actual),
        "brier_score": float(brier),
        "log_loss": float(log_loss),
        "mean_predicted_probability": float(predicted.mean()),
        "actual_payment_fraction": float(actual.mean()),
        "calibration_bins": bins,
    }


def evaluate():
    manifest_path = PREPARED / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    baseline_metadata = json.loads(
        (BASELINE / "metadata.json").read_text()
    )
    baseline_path = BASELINE / "hazards.npz"

    if baseline_metadata["source_sha256"] != manifest["source_sha256"]:
        raise ValueError("Baseline and prepared data use different datasets.")

    manifest_hash = hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()

    if manifest_hash != baseline_metadata["prepared_manifest_sha256"]:
        raise ValueError("Preparation changed after baseline fitting.")

    baseline_hash = hashlib.sha256(
        baseline_path.read_bytes()
    ).hexdigest()

    if baseline_hash != baseline_metadata["hazards_sha256"]:
        raise ValueError("Baseline arrays differ from their metadata.")

    split = manifest["splits"]["validation_tuning"]
    cutoff = pd.Timestamp(split["observations_through"])

    invoices = pd.read_csv(
        PREPARED / "validation_tuning_invoices.csv",
        dtype={"invoice_id": str, "client_id": str},
        parse_dates=["issue_date", "paid_date"],
    )

    if invoices["invoice_id"].tolist() != split["invoice_ids"]:
        raise ValueError("Tuning invoice membership or order has changed.")

    # Reference CSVs can contain later payments. Hide them explicitly.
    observed_paid = invoices["paid_date"].where(
        invoices["paid_date"] <= cutoff
    )

    with np.load(baseline_path, allow_pickle=False) as baseline:
        predictions = predict_payment_probabilities(
            invoices, baseline
        )

    report = {
        "model": "empirical_daily_payment_baseline",
        "partition": "validation_tuning",
        "forecast_anchor": "end_of_issue_date",
        "observations_through": str(cutoff.date()),
        "source_sha256": manifest["source_sha256"],
        "horizons": {},
    }

    for horizon in HORIZONS:
        deadline = invoices["issue_date"] + pd.Timedelta(
            days=horizon
        )

        # Require complete follow-up, even if payment occurred early.
        eligible = deadline <= cutoff

        actual = (
            observed_paid.notna()
            & (observed_paid > invoices["issue_date"])
            & (observed_paid <= deadline)
        ).to_numpy(dtype=np.float64)

        predicted = predictions[horizon]

        if not eligible.any():
            raise ValueError(
                f"No invoices have complete {horizon}-day follow-up."
            )

        selected = eligible.to_numpy()
        metrics = probability_metrics(
            actual[selected],
            predicted[selected],
        )

        report["horizons"][str(horizon)] = metrics

        print(
            f"{horizon:2d} days | "
            f"invoices={metrics['invoices']:,} | "
            f"Brier={metrics['brier_score']:.4f} | "
            f"log loss={metrics['log_loss']:.4f} | "
            f"predicted={metrics['mean_predicted_probability']:.1%} | "
            f"actual={metrics['actual_payment_fraction']:.1%}"
        )

    report["mean_horizon_brier"] = float(np.mean([
        metrics["brier_score"]
        for metrics in report["horizons"].values()
    ]))

    path = BASELINE / "tuning_evaluation.json"
    path.write_text(json.dumps(report, indent=2) + "\n")

    print(
        "\nMean horizon Brier:",
        f"{report['mean_horizon_brier']:.4f}",
    )
    print("Saved:", path)


if __name__ == "__main__":
    evaluate()