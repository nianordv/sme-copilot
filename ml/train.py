"""Train one LightGBM daily payment-hazard configuration.
"""

import hashlib
import argparse
import json
import platform
import time
from importlib.metadata import version
from pathlib import Path

import lightgbm as lgb
import numpy as np


ROOT = Path(__file__).resolve().parent
PREPARED = ROOT / "artifacts" / "prepared"
CONFIG_PATH = ROOT / "model_config.json"
OUTPUT = ROOT / "artifacts" / "lightgbm" / "first_run"
SELECTED_OUTPUT = OUTPUT.parent / 'leaves_16_min_100'

EARLY_STOPPING_ROUNDS = 30


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_existing_run(run, expected, manifest, prepared=None):
    """Reuse only a model with matching settings, data and file fingerprints."""
    run = Path(run)
    prepared = PREPARED if prepared is None else Path(prepared)
    metadata = json.loads((run / 'metadata.json').read_text())
    if metadata['parameters'] != expected:
        raise ValueError(f'{run.name}: saved parameters differ from this configuration.')
    if metadata['source_sha256'] != manifest['source_sha256']:
        raise ValueError(f'{run.name}: saved model uses a different dataset.')
    if metadata['prepared_manifest_sha256'] != file_hash(prepared / 'manifest.json'):
        raise ValueError(f'{run.name}: preparation changed after training.')
    for partition, key in [('train', 'training_arrays_sha256'),
                           ('validation_tuning', 'tuning_arrays_sha256')]:
        if metadata[key] != file_hash(prepared / f'{partition}.npz'):
            raise ValueError(f'{run.name}: {partition} arrays changed.')
    if metadata['model_sha256'] != file_hash(run / 'model.txt'):
        raise ValueError(f'{run.name}: saved model changed.')
    if metadata['feature_names'] != manifest['feature_names']:
        raise ValueError(f'{run.name}: feature definitions changed.')
    return metadata


def train_selected():
    config = json.loads(CONFIG_PATH.read_text())
    config.pop('model', None)
    overrides = {'num_leaves': 16, 'min_child_samples': 100}
    expected = {**config, **overrides}
    if expected['n_estimators'] != 500:
        raise ValueError('The selected submission configuration requires a 500-tree limit.')
    manifest = json.loads((PREPARED / 'manifest.json').read_text())
    if SELECTED_OUTPUT.exists() and any(SELECTED_OUTPUT.iterdir()):
        if not all((SELECTED_OUTPUT / name).is_file() for name in ('model.txt', 'metadata.json')):
            raise FileExistsError(f'{SELECTED_OUTPUT} contains an incomplete run. Inspect it before continuing; no files were overwritten.')
        metadata = validate_existing_run(SELECTED_OUTPUT, expected, manifest)
        print(f'Reusing validated selected model: {SELECTED_OUTPUT / "model.txt"}')
        return metadata
    return train(output=SELECTED_OUTPUT, overrides=overrides)


def load_partition(name, manifest):
    path = PREPARED / f"{name}.npz"

    with np.load(path, allow_pickle=False) as data:
        x = data["X"]
        y = data["y"]
        candidate_dates = data["candidate_date"]
        invoice_ids = data["invoice_id"]

    split = manifest["splits"][name]
    feature_names = manifest["feature_names"]

    if x.shape != (split["daily_rows"], len(feature_names)):
        raise ValueError(f"{name}: unexpected predictor dimensions.")

    if len(y) != len(x):
        raise ValueError(f"{name}: predictors and labels differ in length.")

    if invoice_ids.tolist() != split["invoice_ids"]:
        raise ValueError(f"{name}: invoice membership or order differs.")

    if not np.isin(y, [0, 1]).all():
        raise ValueError(f"{name}: labels must be zero or one.")

    if len(np.unique(y)) != 2:
        raise ValueError(f"{name}: both payment classes are required.")

    if int(y.sum()) != split["payment_events"]:
        raise ValueError(f"{name}: payment-event count differs.")

    if np.isinf(x).any():
        raise ValueError(f"{name}: predictors contain infinite values.")

    cutoff = np.datetime64(split["observations_through"], "D")

    if np.isnat(candidate_dates).any():
        raise ValueError(f"{name}: missing observation dates.")

    if (candidate_dates > cutoff).any():
        raise ValueError(f"{name}: observations exceed the cutoff.")

    # Missing history values are expected and remain NaN.
    return x, y


def train(output=OUTPUT, overrides=None):
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(
            f"{output} already contains a run. "
            "Use a different output directory for another experiment."
        )

    manifest_path = PREPARED / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    config = json.loads(CONFIG_PATH.read_text())

    # This entry describes the model but is not a LightGBM parameter.
    config.pop("model", None)

    if overrides:
        allowed = {"num_leaves", "min_child_samples", "n_estimators"}
        unexpected = set(overrides) - allowed
        if unexpected:
            raise ValueError(f"Unsupported parameter overrides: {unexpected}")
        config.update(overrides)

    feature_names = manifest["feature_names"]
    categorical_indices = [
        feature_names.index(name)
        for name in manifest["categorical_features"]
    ]

    print("Loading training and tuning data...", flush=True)

    x_train, y_train = load_partition("train", manifest)
    x_tuning, y_tuning = load_partition(
        "validation_tuning", manifest
    )

    print(
        f"Training: {len(y_train):,} rows, "
        f"{x_train.shape[1]} predictors, "
        f"{int(y_train.sum()):,} payments",
        flush=True,
    )
    print(
        f"Tuning: {len(y_tuning):,} rows, "
        f"{int(y_tuning.sum()):,} payments",
        flush=True,
    )
    print(
        "Categorical predictors:",
        manifest["categorical_features"],
        flush=True,
    )

    history = {}
    model = lgb.LGBMClassifier(**config)

    started = time.perf_counter()

    model.fit(
        x_train,
        y_train,
        feature_name=feature_names,
        categorical_feature=categorical_indices,
        eval_set=[(x_tuning, y_tuning)],
        eval_names=["tuning"],
        eval_metric="binary_logloss",
        callbacks=[
            lgb.early_stopping(
                stopping_rounds=EARLY_STOPPING_ROUNDS,
                first_metric_only=True,
            ),
            lgb.log_evaluation(period=25),
            lgb.record_evaluation(history),
        ],
    )

    elapsed = time.perf_counter() - started
    best_iteration = int(
        model.best_iteration_ or model.n_estimators_
    )
    tuning_log_loss = float(
        model.best_score_["tuning"]["binary_logloss"]
    )

    output.mkdir(parents=True, exist_ok=True)
    model_path = output / "model.txt"

    # Save only the selected number of boosting iterations.
    model.booster_.save_model(
        str(model_path),
        num_iteration=best_iteration,
    )

    importance = model.booster_.feature_importance(
        importance_type="gain",
        iteration=best_iteration,
    )

    importance_rows = sorted(
        [
            {"feature": name, "gain": float(gain)}
            for name, gain in zip(feature_names, importance)
        ],
        key=lambda row: row["gain"],
        reverse=True,
    )

    metadata = {
        "model": "LightGBM discrete daily payment hazard",
        "run_name": output.name,
        "parameters": config,
        "early_stopping_rounds": EARLY_STOPPING_ROUNDS,
        "best_iteration": best_iteration,
        "tuning_daily_log_loss": tuning_log_loss,
        "fit_seconds": elapsed,
        "training_rows": len(y_train),
        "tuning_rows": len(y_tuning),
        "feature_names": feature_names,
        "categorical_features": manifest["categorical_features"],
        "source_sha256": manifest["source_sha256"],
        "prepared_manifest_sha256": file_hash(manifest_path),
        "training_arrays_sha256": file_hash(
            PREPARED / "train.npz"
        ),
        "tuning_arrays_sha256": file_hash(
            PREPARED / "validation_tuning.npz"
        ),
        "model_sha256": file_hash(model_path),
        "versions": {
            "python": platform.python_version(),
            "numpy": version("numpy"),
            "pandas": version("pandas"),
            "lightgbm": version("lightgbm"),
            "scikit-learn": version("scikit-learn"),
        },
        "calibrated": False,
        "test_evaluated": False,
    }

    for filename, contents in [
        ("metadata.json", metadata),
        ("learning_history.json", history),
        ("feature_importance.json", importance_rows),
    ]:
        (output / filename).write_text(
            json.dumps(contents, indent=2) + "\n"
        )

    print("\nTraining complete.")
    print(f"Selected boosting iterations: {best_iteration}")
    print(f"Tuning daily log loss: {tuning_log_loss:.6f}")
    print(f"Fit time: {elapsed:.1f} seconds")
    print(f"Saved model: {model_path}")
    print(
        "Next: evaluate invoice-level payment horizons "
        "against the baseline."
    )
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selected', action='store_true',
                        help='Train only the selected submission configuration, or validate and reuse it.')
    args = parser.parse_args()
    train_selected() if args.selected else train()
