"""Save payment curves for invoices outstanding at the dataset snapshot.

Run from the repository root: python -m ml.score_open_invoices
This scores the saved model; it does not train, tune or evaluate it.
"""

import argparse
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from .evaluate_overdue import predict_baseline_curves, predict_model_curves
from .prepare_data import load_invoices
from .train import file_hash

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "data" / "invoices_georgia.csv"
PREPARED = ROOT / "artifacts" / "prepared"
RUN = ROOT / "artifacts" / "lightgbm" / "leaves_16_min_100"
BASELINE = ROOT / "artifacts" / "baseline"
OUTPUT = ROOT / "artifacts" / "forecast"
DAYS = 60


def outstanding_at(invoices, snapshot):
    """Use ledger dates, not the exported status or outcome columns."""
    mask = (
        (invoices.issue_date <= snapshot)
        & (invoices.paid_date.isna() | (invoices.paid_date > snapshot))
        & (invoices.writeoff_date.isna() | (invoices.writeoff_date > snapshot))
    )
    return invoices.loc[mask].reset_index(drop=True)


def validate_curves(curves, count):
    if curves.shape != (count, DAYS):
        raise ValueError("Unexpected payment-curve dimensions.")
    if not np.isfinite(curves).all() or (curves < 0).any() or (curves > 1).any():
        raise ValueError("Payment probabilities must be finite and between zero and one.")
    if (np.diff(curves, axis=1) < 0).any():
        raise ValueError("Cumulative payment probabilities must never decrease.")


def validate_artifact(metadata, manifest, manifest_path, artifact, hash_key):
    if metadata["source_sha256"] != manifest["source_sha256"]:
        raise ValueError("Saved predictor was fitted on a different source dataset.")
    if metadata["prepared_manifest_sha256"] != file_hash(manifest_path):
        raise ValueError("Preparation changed after predictor fitting.")
    if metadata[hash_key] != file_hash(artifact):
        raise ValueError(f"Saved artifact changed: {artifact.name}.")


def load_model(run, manifest, manifest_path):
    model_path = run / "model.txt"
    metadata_path = run / "metadata.json"
    if not model_path.exists() or not metadata_path.exists():
        return None, None
    metadata = json.loads(metadata_path.read_text())
    validate_artifact(metadata, manifest, manifest_path, model_path, "model_sha256")
    if metadata["feature_names"] != manifest["feature_names"]:
        raise ValueError("Model feature definitions do not match preparation.")
    parameters = metadata["parameters"]
    if (parameters["num_leaves"], parameters["min_child_samples"], parameters["n_estimators"]) != (16, 100, 500):
        raise ValueError("This script expects the selected 16-leaf, minimum-100, 500-limit run.")
    return lgb.Booster(model_file=str(model_path)), metadata


def score(source=SOURCE, prepared=PREPARED, run=RUN, baseline_dir=BASELINE, output=OUTPUT):
    source, prepared, run = Path(source), Path(prepared), Path(run)
    baseline_dir, output = Path(baseline_dir), Path(output)
    dataset = json.loads(source.with_name("dataset_metadata.json").read_text())
    manifest_path = prepared / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    source_hash = file_hash(source)
    if source_hash != dataset["csv_sha256"] or source_hash != manifest["source_sha256"]:
        raise ValueError("Source CSV, generation metadata and preparation do not match.")
    snapshot = pd.Timestamp(dataset["snapshot_date"])
    if dataset["snapshot_date"] != manifest["snapshot_date"]:
        raise ValueError("Snapshot dates do not match.")

    # Rebuild issue-date history from ledger events, ignoring exported features.
    invoices = load_invoices(source)
    for column in ("issue_date", "paid_date", "writeoff_date"):
        if (invoices[column] > snapshot).any():
            raise ValueError(f"{column} contains events after the snapshot.")
    if not np.isfinite(invoices.amount).all():
        raise ValueError("Invoice amounts must be finite.")
    cohort = outstanding_at(invoices, snapshot)
    anchors = pd.Series(snapshot, index=cohort.index)

    baseline_path = baseline_dir / "hazards.npz"
    baseline_metadata = json.loads((baseline_dir / "metadata.json").read_text())
    validate_artifact(baseline_metadata, manifest, manifest_path, baseline_path, "hazards_sha256")
    if baseline_metadata["fitted_partition"] != "train":
        raise ValueError("Baseline must have been fitted on training data only.")
    with np.load(baseline_path, allow_pickle=False) as baseline:
        baseline_curves = predict_baseline_curves(cohort, anchors, baseline)
    validate_curves(baseline_curves, len(cohort))

    model, model_metadata = load_model(run, manifest, manifest_path)
    fallback = model is None
    curves = baseline_curves if fallback else predict_model_curves(cohort, anchors, model, manifest)
    validate_curves(curves, len(cohort))

    table = cohort[["invoice_id", "client_id", "amount"]].copy()
    table = pd.concat([table, pd.DataFrame(curves, columns=[f"day_{day}" for day in range(1, DAYS + 1)])], axis=1)
    amounts = cohort.amount.to_numpy(dtype=float)
    expected = float(amounts @ curves[:, 29])
    baseline_expected = float(amounts @ baseline_curves[:, 29])
    output.mkdir(parents=True, exist_ok=True)
    csv_path = output / "open_invoice_curves.csv"
    table.to_csv(csv_path, index=False, float_format="%.10g")
    metadata = {
        "snapshot_date": dataset["snapshot_date"], "snapshot_semantics": "end_of_day",
        "currency": dataset["currency"], "synthetic": dataset["synthetic"],
        "horizon_days": DAYS, "invoice_count": len(cohort),
        "outstanding_amount": float(amounts.sum()),
        "model_used": "empirical_baseline" if fallback else run.name,
        "fallback": fallback,
        "fallback_reason": "Selected model or model metadata is missing." if fallback else None,
        "model_sha256": None if fallback else model_metadata["model_sha256"],
        "baseline_sha256": baseline_metadata["hazards_sha256"],
        "source_csv_sha256": source_hash, "prepared_manifest_sha256": file_hash(manifest_path),
        "forecast_csv_sha256": file_hash(csv_path),
        "history_definition": "frozen_at_issue", "calibrated": False,
        "expected_receipts_30d": expected, "baseline_expected_receipts_30d": baseline_expected,
        "scope": "Full payments of invoices outstanding at snapshot; future invoices excluded.",
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2, allow_nan=False) + "\n")
    print("Snapshot:", snapshot.date())
    print(f"Outstanding invoices: {len(cohort):,}")
    print("Predictor:", metadata["model_used"])
    if fallback:
        print("WARNING: selected model files missing; using the empirical baseline.")
    print(f"Expected receipts within 30 days: {expected:,.2f} {dataset['currency']}")
    print(f"Baseline comparison: {baseline_expected:,.2f} {dataset['currency']}")
    print("Saved:", csv_path)
    return table, metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--prepared", type=Path, default=PREPARED)
    parser.add_argument("--run", type=Path, default=RUN)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    score(args.source, args.prepared, args.run, args.baseline, args.output)
