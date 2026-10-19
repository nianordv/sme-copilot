"""Compare six preplanned LightGBM configurations on tuning data.

Run: python -m ml.tune
Calibration and test data are not loaded.
"""

import json
from pathlib import Path

import pandas as pd

from .evaluate_model import evaluate
from .train import file_hash, train, validate_existing_run


ROOT = Path(__file__).resolve().parent
PREPARED = ROOT / "artifacts" / "prepared"
RUNS = ROOT / "artifacts" / "lightgbm"
CONFIG_PATH = ROOT / "model_config.json"

LEAF_OPTIONS = [8, 16]
MIN_SAMPLE_OPTIONS = [100, 200, 400]
MIN_RELATIVE_IMPROVEMENT = 0.02


def tune():
    manifest = json.loads((PREPARED / "manifest.json").read_text())
    base_config = json.loads(CONFIG_PATH.read_text())
    base_config.pop("model", None)

    results = []

    for leaves in LEAF_OPTIONS:
        for minimum in MIN_SAMPLE_OPTIONS:
            # Preserve and reuse the configuration already trained.
            if leaves == 8 and minimum == 200:
                run = RUNS / "first_run"
            else:
                run = RUNS / f"leaves_{leaves}_min_{minimum}"

            overrides = {
                "num_leaves": leaves,
                "min_child_samples": minimum,
            }
            expected = {**base_config, **overrides}

            print(
                f"\n=== Leaves: {leaves}; "
                f"minimum leaf samples: {minimum} ===",
                flush=True,
            )

            model_path = run / "model.txt"
            metadata_path = run / "metadata.json"

            if model_path.exists() and metadata_path.exists():
                print(f"Reusing saved run: {run.name}", flush=True)
            else:
                # Do not silently overwrite an incomplete experiment.
                if run.exists() and any(run.iterdir()):
                    raise RuntimeError(
                        f"{run} contains an incomplete run. "
                        "Inspect it before continuing."
                    )
                train(output=run, overrides=overrides)

            metadata = validate_existing_run(run, expected, manifest)
            report = evaluate(run=run)

            row = {
                "run": run.name,
                "num_leaves": leaves,
                "min_child_samples": minimum,
                "best_iteration": metadata["best_iteration"],
                "fit_seconds": metadata["fit_seconds"],
                "mean_brier": report["mean_horizon_brier"],
                "relative_improvement": report["relative_mean_brier_improvement"],
            }

            for horizon in [7, 14, 30, 60]:
                metrics = report["horizons"][str(horizon)]
                row[f"brier_{horizon}d"] = metrics["brier_score"]
                row[f"log_loss_{horizon}d"] = metrics["log_loss"]

            results.append(row)

    # Exact ties favor fewer leaves, then a larger minimum leaf size.
    ranked = sorted(
        results,
        key=lambda row: (
            row["mean_brier"],
            row["num_leaves"],
            -row["min_child_samples"],
        ),
    )
    winner = ranked[0]

    RUNS.mkdir(parents=True, exist_ok=True)
    table = pd.DataFrame(ranked)
    table.to_csv(RUNS / "tuning_comparison.csv", index=False)

    summary = {
        "partition": "validation_tuning",
        "forecast_anchor": "end_of_issue_date",
        "source_sha256": manifest["source_sha256"],
        "selection_metric": "mean_7_14_30_60_day_brier",
        "planned_configurations": 6,
        "ranked_results": ranked,
        "provisional_best_run": winner["run"],
        "passes_issue_horizon_improvement_gate": bool(
            winner["relative_improvement"] >= MIN_RELATIVE_IMPROVEMENT
        ),
        "final_model_locked": False,
        "remaining_checks": [
            "Overdue forecast evaluation",
            "Receipt forecast evaluation",
            "Subgroup and calibration review",
        ],
        "calibration_data_used": False,
        "test_data_used": False,
    }

    (RUNS / "tuning_comparison.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )

    print("\n=== Ranked configuration comparison ===")
    print(
        table[
            [
                "run", "best_iteration", "mean_brier", "relative_improvement",
                "brier_7d", "brier_14d", "brier_30d", "brier_60d",
            ]
        ].to_string(index=False, float_format=lambda x: f"{x:.6f}")
    )
    print("\nProvisional best run:", winner["run"])
    print("Final selection still requires the remaining checks.")


if __name__ == "__main__":
    tune()
