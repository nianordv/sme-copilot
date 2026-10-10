"""Compare overdue payment forecasts on the tuning period.

Run:
    python -m ml.evaluate_overdue

Uses the selected 500-tree model.
Calibration and test data are not loaded.
"""

import json

import lightgbm as lgb
import numpy as np
import pandas as pd

from .evaluate_baseline import (
    BASELINE,
    HORIZONS,
    probability_metrics,
)
from .evaluate_model import file_hash
from .prepare_data import calendar_features, static_features
from .tune import PREPARED, RUNS, validate_existing_run


RUN = RUNS / "leaves_16_min_100"
BATCH_SIZE = 1000
ANCHORS = {
    "due_date": 0,
    "14_days_overdue": 14,
}


def predict_model_curves(invoices, anchors, model, manifest):
    """Predict payment after the anchor, conditional on still unpaid."""

    base = static_features(invoices, manifest["categories"])
    names = manifest["feature_names"]

    if model.feature_name() != names:
        raise ValueError("Model and prepared feature order differ.")

    days = np.arange(1, max(HORIZONS) + 1)
    predictions = np.empty((len(invoices), len(days)), dtype=float)

    for start in range(0, len(invoices), BATCH_SIZE):
        stop = min(start + BATCH_SIZE, len(invoices))
        batch = invoices.iloc[start:stop]

        issue = batch.issue_date.to_numpy(dtype="datetime64[D]")
        due = batch.due_date.to_numpy(dtype="datetime64[D]")
        anchor = anchors.iloc[start:stop].to_numpy(
            dtype="datetime64[D]"
        )

        candidate = (
            anchor[:, None]
            + days[None, :].astype("timedelta64[D]")
        )

        daily = calendar_features(
            candidate.reshape(-1), "candidate"
        )
        daily.insert(
            0,
            "days_past_due",
            (candidate - due[:, None]).astype(int).reshape(-1),
        )
        daily.insert(
            0,
            "invoice_age",
            (candidate - issue[:, None]).astype(int).reshape(-1),
        )

        batch_base = base.iloc[start:stop]

        if [*batch_base.columns, *daily.columns] != names:
            raise ValueError("Incorrect prediction feature order.")

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
            x, num_threads=1
        ).reshape(len(batch), len(days))

        if not (
            np.isfinite(hazards).all()
            and (hazards >= 0).all()
            and (hazards <= 1).all()
        ):
            raise ValueError("Invalid model probabilities.")

        cumulative = 1.0 - np.cumprod(1.0 - hazards, axis=1)

        predictions[start:stop] = cumulative

    return predictions


def predict_model(invoices, anchors, model, manifest):
    curves = predict_model_curves(invoices, anchors, model, manifest)
    return {horizon: curves[:, horizon - 1] for horizon in HORIZONS}


def predict_baseline_curves(invoices, anchors, baseline):
    """Use baseline hazards at the invoice's age after the anchor."""

    term_lookup = {
        int(term): index
        for index, term in enumerate(baseline["terms"])
    }
    client_lookup = {
        str(client): index
        for index, client in enumerate(baseline["clients"])
    }

    term_index = invoices.payment_terms.map(term_lookup)

    if term_index.isna().any():
        raise ValueError("Unsupported payment terms.")

    term_index = term_index.to_numpy(dtype=int)
    client_index = (
        invoices.client_id.map(client_lookup)
        .fillna(-1)
        .to_numpy(dtype=int)
    )

    anchor_age = (
        anchors - invoices.issue_date
    ).dt.days.to_numpy()

    if invoices.empty:
        return np.empty((0, max(HORIZONS)), dtype=float)

    ages = (
        anchor_age[:, None]
        + np.arange(1, max(HORIZONS) + 1)[None, :]
    )

    if ages.max() >= baseline["term_hazard"].shape[1]:
        raise ValueError("Forecast exceeds baseline age support.")

    hazards = baseline["term_hazard"][
        term_index[:, None], ages
    ].copy()

    known = client_index >= 0
    hazards[known] = baseline["client_hazard"][
        client_index[known, None],
        term_index[known, None],
        ages[known],
    ]

    if not (
        np.isfinite(hazards).all()
        and (hazards >= 0).all()
        and (hazards <= 1).all()
    ):
        raise ValueError("Invalid baseline probabilities.")

    cumulative = 1.0 - np.cumprod(1.0 - hazards, axis=1)

    return cumulative


def predict_baseline(invoices, anchors, baseline):
    curves = predict_baseline_curves(invoices, anchors, baseline)
    return {horizon: curves[:, horizon - 1] for horizon in HORIZONS}


def evaluate():
    manifest = json.loads(
        (PREPARED / "manifest.json").read_text()
    )
    metadata = json.loads((RUN / "metadata.json").read_text())

    expected = {
        **metadata["parameters"],
        "num_leaves": 16,
        "min_child_samples": 100,
        "n_estimators": 500,
    }
    validate_existing_run(RUN, expected, manifest)

    baseline_metadata = json.loads(
        (BASELINE / "metadata.json").read_text()
    )
    baseline_path = BASELINE / "hazards.npz"

    if baseline_metadata["source_sha256"] != manifest["source_sha256"]:
        raise ValueError("Baseline uses a different dataset.")

    if baseline_metadata["prepared_manifest_sha256"] != file_hash(
        PREPARED / "manifest.json"
    ):
        raise ValueError("Preparation changed after baseline fitting.")

    if baseline_metadata["hazards_sha256"] != file_hash(baseline_path):
        raise ValueError("Baseline arrays changed.")

    split = manifest["splits"]["validation_tuning"]
    cutoff = pd.Timestamp(split["observations_through"])

    invoices = pd.read_csv(
        PREPARED / "validation_tuning_invoices.csv",
        dtype={"invoice_id": str, "client_id": str},
        parse_dates=[
            "issue_date", "due_date", "paid_date", "writeoff_date"
        ],
    )

    if invoices.invoice_id.tolist() != split["invoice_ids"]:
        raise ValueError("Tuning invoice membership changed.")

    # Reference CSVs retain later events: explicitly hide them.
    invoices["paid_date"] = invoices.paid_date.where(
        invoices.paid_date <= cutoff
    )
    invoices["writeoff_date"] = invoices.writeoff_date.where(
        invoices.writeoff_date <= cutoff
    )

    model = lgb.Booster(model_file=str(RUN / "model.txt"))
    results = []
    prediction_tables = []

    with np.load(baseline_path, allow_pickle=False) as baseline:
        for anchor_name, overdue_days in ANCHORS.items():
            anchor_dates = (
                invoices.due_date
                + pd.Timedelta(days=overdue_days)
            )

            # At the end of the anchor date:
            # still unpaid, not yet written off, and observable.
            eligible_anchor = (
                (anchor_dates <= cutoff)
                & (
                    invoices.paid_date.isna()
                    | (invoices.paid_date > anchor_dates)
                )
                & (
                    invoices.writeoff_date.isna()
                    | (invoices.writeoff_date > anchor_dates)
                )
            )

            cohort = invoices.loc[eligible_anchor].reset_index(
                drop=True
            )
            anchors = anchor_dates.loc[eligible_anchor].reset_index(
                drop=True
            )

            if cohort.empty:
                raise ValueError(f"No invoices for {anchor_name}.")

            print(
                f"\n=== {anchor_name}: "
                f"{len(cohort):,} unpaid invoices at anchor ===",
                flush=True,
            )

            model_predictions = predict_model(
                cohort, anchors, model, manifest
            )
            baseline_predictions = predict_baseline(
                cohort, anchors, baseline
            )

            table = cohort[
                ["invoice_id", "client_id", "amount"]
            ].copy()
            table["anchor_type"] = anchor_name
            table["anchor_date"] = anchors

            for horizon in HORIZONS:
                deadline = anchors + pd.Timedelta(days=horizon)
                complete = (deadline <= cutoff).to_numpy()

                actual = (
                    cohort.paid_date.notna()
                    & (cohort.paid_date > anchors)
                    & (cohort.paid_date <= deadline)
                ).to_numpy(dtype=float)

                if not complete.any():
                    raise ValueError(
                        f"No complete follow-up for "
                        f"{anchor_name}, {horizon} days."
                    )

                model_probability = model_predictions[horizon]
                baseline_probability = baseline_predictions[horizon]

                model_metrics = probability_metrics(
                    actual[complete], model_probability[complete]
                )
                baseline_metrics = probability_metrics(
                    actual[complete], baseline_probability[complete]
                )

                baseline_brier = baseline_metrics["brier_score"]
                improvement = (
                    (
                        baseline_brier
                        - model_metrics["brier_score"]
                    )
                    / baseline_brier
                    if baseline_brier > 0 else None
                )

                results.append({
                    "anchor": anchor_name,
                    "horizon_days": horizon,
                    "model": model_metrics,
                    "baseline": baseline_metrics,
                    "relative_brier_improvement": improvement,
                })

                table[f"model_probability_{horizon}d"] = (
                    model_probability
                )
                table[f"baseline_probability_{horizon}d"] = (
                    baseline_probability
                )
                table[f"eligible_{horizon}d"] = complete
                table[f"actual_paid_{horizon}d"] = np.where(
                    complete, actual, np.nan
                )

                improvement_text = (
                    f"{improvement:+.1%}"
                    if improvement is not None else "undefined"
                )

                print(
                    f"{horizon:2d} days | "
                    f"invoices={model_metrics['invoices']:,} | "
                    f"model Brier={model_metrics['brier_score']:.4f} | "
                    f"baseline={baseline_brier:.4f} | "
                    f"improvement={improvement_text} | "
                    f"predicted={model_metrics['mean_predicted_probability']:.1%} | "
                    f"actual={model_metrics['actual_payment_fraction']:.1%}"
                )

            prediction_tables.append(table)

    report = {
        "run": RUN.name,
        "partition": "validation_tuning",
        "observations_through": str(cutoff.date()),
        "source_sha256": manifest["source_sha256"],
        "model_sha256": metadata["model_sha256"],
        "history_definition": "frozen_at_issue",
        "calibrated": False,
        "results": results,
    }

    (RUN / "overdue_evaluation.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    pd.concat(prediction_tables, ignore_index=True).to_csv(
        RUN / "overdue_predictions.csv",
        index=False,
    )

    print("\nSaved overdue evaluation and predictions to:", RUN)


if __name__ == "__main__":
    evaluate()
