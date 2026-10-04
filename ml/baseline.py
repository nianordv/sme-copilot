"""Fit a censor-aware empirical daily payment baseline.

Run from the repository root:
    python -m ml.baseline

Uses training data only. Does not evaluate validation or test data.
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
PREPARED = ROOT / "artifacts" / "prepared"
OUTPUT = ROOT / "artifacts" / "baseline"

TERMS = np.array([15, 30, 60], dtype=np.int16)

# Smoothing choices are fixed before inspecting validation results.
GLOBAL_PRIOR_STRENGTH = 20.0
TERM_PRIOR_STRENGTH = 20.0
CLIENT_SHRINKAGE = 5.0
MIN_CLIENT_PAYMENTS = 3


def fit_baseline():
    manifest_path = PREPARED / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    training = np.load(
        PREPARED / "train.npz",
        allow_pickle=False,
    )
    invoices = pd.read_csv(
        PREPARED / "train_invoices.csv",
        dtype={"invoice_id": str, "client_id": str},
    )

    # Ensure daily-row references point to the expected invoice table.
    expected_ids = training["invoice_id"].tolist()
    if invoices["invoice_id"].tolist() != expected_ids:
        raise ValueError("Training invoice order does not match the arrays.")

    if len(invoices) != manifest["splits"]["train"]["invoices"]:
        raise ValueError("Training invoice count differs from the manifest.")

    feature_names = manifest["feature_names"]
    age_column = feature_names.index("invoice_age")

    age = training["X"][:, age_column].astype(np.int32)
    label = training["y"].astype(np.float64)
    invoice_index = training["invoice_index"]

    tail_days = int(manifest["tail_days_past_due"])
    max_age = int(TERMS.max()) + tail_days

    if len(age) == 0:
        raise ValueError("Training data contains no daily observations.")

    if age.min() < 1 or age.max() > max_age:
        raise ValueError("Invoice ages exceed the prepared observation range.")

    if not np.isin(label, [0, 1]).all():
        raise ValueError("Payment labels must be zero or one.")

    # Stable numerical identifiers for clients and payment terms.
    clients = sorted(invoices["client_id"].unique().tolist())
    client_lookup = {client: i for i, client in enumerate(clients)}
    term_lookup = {int(term): i for i, term in enumerate(TERMS)}

    invoice_client = (
        invoices["client_id"].map(client_lookup).to_numpy(dtype=np.int32)
    )
    invoice_term = (
        invoices["payment_terms"].map(term_lookup)
    )

    if invoice_term.isna().any():
        raise ValueError("Unexpected payment terms.")

    invoice_term = invoice_term.to_numpy(dtype=np.int32)
    row_client = invoice_client[invoice_index]
    row_term = invoice_term[invoice_index]

    # Each daily row means this invoice was unpaid before that day.
    global_risk = np.bincount(
        age, minlength=max_age + 1
    ).astype(np.float64)
    global_events = np.bincount(
        age, weights=label, minlength=max_age + 1
    )

    # A small prior prevents sparse ages producing exact 0% or 100%.
    overall_rate = float(label.mean())
    global_hazard = (
        global_events + GLOBAL_PRIOR_STRENGTH * overall_rate
    ) / (
        global_risk + GLOBAL_PRIOR_STRENGTH
    )
    global_hazard[0] = 0.0

    term_shape = (len(TERMS), max_age + 1)
    term_risk = np.zeros(term_shape, dtype=np.float64)
    term_events = np.zeros(term_shape, dtype=np.float64)

    np.add.at(term_risk, (row_term, age), 1.0)
    np.add.at(term_events, (row_term, age), label)

    term_hazard = (
        term_events + TERM_PRIOR_STRENGTH * global_hazard[None, :]
    ) / (
        term_risk + TERM_PRIOR_STRENGTH
    )

    client_shape = (len(clients), len(TERMS), max_age + 1)
    client_risk = np.zeros(client_shape, dtype=np.float64)
    client_events = np.zeros(client_shape, dtype=np.float64)

    np.add.at(client_risk, (row_client, row_term, age), 1.0)
    np.add.at(client_events, (row_client, row_term, age), label)

    # No exposure at a particular age: use the term-level estimate.
    raw_client_hazard = np.broadcast_to(
        term_hazard[None, :, :], client_shape
    ).copy()
    np.divide(
        client_events,
        client_risk,
        out=raw_client_hazard,
        where=client_risk > 0,
    )

    client_invoice_counts = np.bincount(
        invoice_client, minlength=len(clients)
    )
    client_payment_counts = np.bincount(
        row_client,
        weights=label,
        minlength=len(clients),
    )

    # Clients with fewer than three observed training payments
    # receive the term-level estimate.
    weight = client_invoice_counts / (
        client_invoice_counts + CLIENT_SHRINKAGE
    )
    weight[client_payment_counts < MIN_CLIENT_PAYMENTS] = 0.0

    client_hazard = (
        weight[:, None, None] * raw_client_hazard
        + (1.0 - weight[:, None, None]) * term_hazard[None, :, :]
    )

    # No payment on issue day, and no extrapolation beyond our tail cap.
    term_hazard[:, 0] = 0.0
    client_hazard[:, :, 0] = 0.0

    for term_index, term in enumerate(TERMS):
        stop = int(term) + tail_days
        term_hazard[term_index, stop + 1:] = 0.0
        client_hazard[:, term_index, stop + 1:] = 0.0

    if not (
        np.isfinite(client_hazard).all()
        and (client_hazard >= 0).all()
        and (client_hazard <= 1).all()
    ):
        raise ValueError("Invalid baseline probabilities.")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    arrays_path = OUTPUT / "hazards.npz"

    np.savez_compressed(
        arrays_path,
        terms=TERMS,
        clients=np.array(clients, dtype=str),
        term_hazard=term_hazard,
        client_hazard=client_hazard,
        term_risk=term_risk,
        client_risk=client_risk,
    )

    metadata = {
        "baseline": "empirical_daily_payment_hazard",
        "fitted_partition": "train",
        "observations_through": (
            manifest["splits"]["train"]["observations_through"]
        ),
        "source_sha256": manifest["source_sha256"],
        "prepared_manifest_sha256": hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest(),
        "hazards_sha256": hashlib.sha256(
            arrays_path.read_bytes()
        ).hexdigest(),
        "tail_days_past_due": tail_days,
        "global_prior_strength": GLOBAL_PRIOR_STRENGTH,
        "term_prior_strength": TERM_PRIOR_STRENGTH,
        "client_shrinkage": CLIENT_SHRINKAGE,
        "minimum_client_payments": MIN_CLIENT_PAYMENTS,
        "training_invoices": len(invoices),
        "training_daily_rows": len(label),
        "observed_training_payments": int(label.sum()),
        "training_clients": len(clients),
        "overall_daily_payment_rate": overall_rate,
    }

    (OUTPUT / "metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )

    print("Baseline fitted using training data only.")
    print(f"Training invoices: {len(invoices):,}")
    print(f"Daily observations: {len(label):,}")
    print(f"Observed payments: {int(label.sum()):,}")
    print(f"Clients: {len(clients):,}")
    print(f"Saved to: {OUTPUT}")


if __name__ == "__main__":
    fit_baseline()