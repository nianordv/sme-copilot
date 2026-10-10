"""Evaluate outstanding-invoice receipts on the tuning period."""

import json

import lightgbm as lgb
import numpy as np
import pandas as pd

from .evaluate_baseline import BASELINE
from .evaluate_model import file_hash
from .evaluate_overdue import (
    PREPARED,
    RUN,
    predict_baseline,
    predict_model,
)
from .tune import validate_existing_run


HORIZONS = (30, 60)


def load_invoices(partition="validation_tuning", *, allow_test=False):
    allowed = {"validation_tuning", "validation_calibration"}
    if allow_test:
        allowed.add("test")
    if partition not in allowed:
        raise ValueError("Only tuning and calibration evaluation are allowed.")
    manifest = json.loads(
        (PREPARED / "manifest.json").read_text()
    )

    frames = []

    # Older invoices can still be outstanding during 2025.
    partitions = ["train", "validation_tuning"]
    if partition in ("validation_calibration", "test"):
        partitions.append("validation_calibration")
    if partition == "test":
        partitions.append("test")
    for name in partitions:
        invoices = pd.read_csv(
            PREPARED / f"{name}_invoices.csv",
            dtype={"invoice_id": str, "client_id": str},
            parse_dates=[
                "issue_date", "due_date",
                "paid_date", "writeoff_date",
            ],
        )

        expected_ids = manifest["splits"][name]["invoice_ids"]
        if invoices.invoice_id.tolist() != expected_ids:
            raise ValueError(f"{name} invoice membership changed.")

        frames.append(invoices)

    invoices = pd.concat(frames, ignore_index=True)

    cutoff = pd.Timestamp(
        manifest["splits"][partition]["observations_through"]
    )

    # Events after the tuning cutoff are unavailable.
    for column in ("paid_date", "writeoff_date"):
        invoices[column] = invoices[column].where(
            invoices[column] <= cutoff
        )

    return invoices, manifest, cutoff


def outstanding_at(invoices, anchor):
    """Invoices known, unpaid and not written off at day's end."""

    eligible = (
        (invoices.issue_date <= anchor)
        & (
            invoices.paid_date.isna()
            | (invoices.paid_date > anchor)
        )
        & (
            invoices.writeoff_date.isna()
            | (invoices.writeoff_date > anchor)
        )
    )

    return invoices.loc[eligible].reset_index(drop=True)


def actual_receipts(outstanding, anchor, horizon):
    """Full payments received after the anchor, through the deadline."""

    deadline = anchor + pd.Timedelta(days=horizon)
    paid_in_window = (
        outstanding.paid_date.notna()
        & (outstanding.paid_date > anchor)
        & (outstanding.paid_date <= deadline)
    )

    return float(outstanding.loc[paid_in_window, "amount"].sum())


def load_models(manifest):
    """Load the selected model and baseline after integrity checks."""

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

    model = lgb.Booster(model_file=str(RUN / "model.txt"))

    with np.load(baseline_path, allow_pickle=False) as saved:
        baseline = {name: saved[name].copy() for name in saved.files}

    return model, baseline


def receipt_metrics(actual, predicted):
    """Errors across forecast windows; positive bias is overestimation."""

    errors = predicted - actual
    actual_total = float(actual.sum())
    return {
        "forecast_windows": len(actual),
        "mae_gel": float(np.abs(errors).mean()),
        "mean_signed_error_gel": float(errors.mean()),
        "wape": (
            float(np.abs(errors).sum() / actual_total)
            if actual_total > 0 else None
        ),
    }


def main(partition="validation_tuning", *, allow_test=False):
    invoices, manifest, cutoff = load_invoices(partition, allow_test=allow_test)
    model, baseline = load_models(manifest)
    print("Loaded selected LightGBM model and baseline.")

    anchors = pd.date_range(
        start=manifest["splits"][partition]["start_inclusive"],
        end=cutoff - pd.Timedelta(days=min(HORIZONS)),
        freq="7D",
    )

    print("Partition:", partition)
    print("Observation cutoff:", cutoff.date())
    print("Weekly forecast dates:", len(anchors))
    rows = []

    for anchor in anchors:
        outstanding = outstanding_at(invoices, anchor)
        invoice_anchors = pd.Series(
            anchor, index=outstanding.index
        )

        model_probabilities = predict_model(
            outstanding, invoice_anchors, model, manifest
        )
        baseline_probabilities = predict_baseline(
            outstanding, invoice_anchors, baseline
        )

        amounts = outstanding.amount.to_numpy(dtype=float)

        print(
            f"\n{anchor.date()} | "
            f"outstanding invoices={len(outstanding):,} | "
            f"outstanding amount={outstanding.amount.sum():,.2f} GEL"
        )

        for horizon in HORIZONS:
            # Require full follow-up separately for each horizon.
            # Never score a truncated 60-day window as a complete one.
            if anchor + pd.Timedelta(days=horizon) > cutoff:
                continue
            actual = actual_receipts(outstanding, anchor, horizon)
            model_receipts = float(
                np.dot(amounts, model_probabilities[horizon])
            )
            baseline_receipts = float(
                np.dot(amounts, baseline_probabilities[horizon])
            )
            rows.append({
                "anchor_date": str(anchor.date()),
                "horizon_days": horizon,
                "outstanding_invoices": len(outstanding),
                "outstanding_amount_gel": float(amounts.sum()),
                "model_receipts_gel": model_receipts,
                "baseline_receipts_gel": baseline_receipts,
                "actual_receipts_gel": actual,
                "model_error_gel": model_receipts - actual,
                "baseline_error_gel": baseline_receipts - actual,
            })
            print(
                f"  {horizon} days | "
                f"model={model_receipts:,.2f} | "
                f"baseline={baseline_receipts:,.2f} | "
                f"actual={actual:,.2f} GEL"
            )

    table = pd.DataFrame(rows)
    report = {
        "run": RUN.name,
        "partition": partition,
        "currency": "GEL",
        "observations_through": str(cutoff.date()),
        "source_sha256": manifest["source_sha256"],
        "prepared_manifest_sha256": file_hash(PREPARED / "manifest.json"),
        "model_sha256": file_hash(RUN / "model.txt"),
        "baseline_sha256": file_hash(BASELINE / "hazards.npz"),
        "forecast_anchor": "end_of_day",
        "first_anchor": str(anchors[0].date()),
        "last_anchor": str(anchors[-1].date()),
        "anchor_spacing_days": 7,
        "follow_up_rule": "anchor plus horizon must not exceed observation cutoff",
        "history_definition": "frozen_at_issue",
        "calibrated": False,
        "cohort": "issued, unpaid and not written off at each anchor",
        "limitations": [
            "Synthetic data and one pooled business portfolio.",
            "Future-issued invoices are excluded from receipts.",
            "Overlapping windows are not independent observations.",
            "WAPE sums receipts across windows, not unique period collections.",
        ],
        "horizons": {},
    }

    print("\n=== Receipt evaluation summary ===")
    for horizon in HORIZONS:
        subset = table.loc[table.horizon_days == horizon]
        actual = subset.actual_receipts_gel.to_numpy(dtype=float)
        model_metrics = receipt_metrics(
            actual, subset.model_receipts_gel.to_numpy(dtype=float)
        )
        baseline_metrics = receipt_metrics(
            actual, subset.baseline_receipts_gel.to_numpy(dtype=float)
        )
        baseline_mae = baseline_metrics["mae_gel"]
        improvement = (
            (baseline_mae - model_metrics["mae_gel"]) / baseline_mae
            if baseline_mae > 0 else None
        )
        report["horizons"][str(horizon)] = {
            "first_anchor": subset.anchor_date.iloc[0],
            "last_anchor": subset.anchor_date.iloc[-1],
            "model": model_metrics,
            "baseline": baseline_metrics,
            "relative_mae_improvement": improvement,
        }

        improvement_text = (
            f"{improvement:+.2%}" if improvement is not None else "undefined"
        )
        print(f"\n{horizon} days | windows={len(subset)}")
        for name, metrics in (("Model", model_metrics), ("Baseline", baseline_metrics)):
            wape_text = (
                f"{metrics['wape']:.2%}"
                if metrics["wape"] is not None else "undefined"
            )
            print(
                f"  {name}: MAE={metrics['mae_gel']:,.2f} GEL | "
                f"bias={metrics['mean_signed_error_gel']:+,.2f} GEL | "
                f"WAPE={wape_text}"
            )
        print(f"  MAE improvement: {improvement_text}")

    common_dates = set.intersection(*[
        set(table.loc[table.horizon_days == horizon, "anchor_date"])
        for horizon in HORIZONS
    ])
    report["matched_dates"] = {
        "anchor_dates": sorted(common_dates),
        "horizons": {},
    }
    print("\n=== Same starting dates: horizon comparison ===")
    for horizon in HORIZONS:
        subset = table.loc[
            (table.horizon_days == horizon)
            & table.anchor_date.isin(common_dates)
        ]
        actual = subset.actual_receipts_gel.to_numpy(dtype=float)
        model_metrics = receipt_metrics(
            actual, subset.model_receipts_gel.to_numpy(dtype=float)
        )
        baseline_metrics = receipt_metrics(
            actual, subset.baseline_receipts_gel.to_numpy(dtype=float)
        )
        reduction = baseline_metrics["mae_gel"] - model_metrics["mae_gel"]
        improvement = (
            reduction / baseline_metrics["mae_gel"]
            if baseline_metrics["mae_gel"] > 0 else None
        )
        report["matched_dates"]["horizons"][str(horizon)] = {
            "model": model_metrics,
            "baseline": baseline_metrics,
            "mae_reduction_gel": reduction,
            "relative_mae_improvement": improvement,
        }
        improvement_text = (
            f"{improvement:+.2%}" if improvement is not None else "undefined"
        )
        print(
            f"{horizon} days | windows={len(subset)} | "
            f"model MAE={model_metrics['mae_gel']:,.2f} GEL | "
            f"baseline MAE={baseline_metrics['mae_gel']:,.2f} GEL | "
            f"reduction={reduction:+,.2f} GEL ({improvement_text})"
        )

    prefix = {"validation_tuning": "", "validation_calibration": "calibration_",
              "test": "test_"}[partition]
    table.to_csv(RUN / f"{prefix}receipt_predictions.csv", index=False)
    (RUN / f"{prefix}receipt_evaluation.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    print("\nSaved receipt predictions and evaluation to:", RUN)


if __name__ == "__main__":
    main()
