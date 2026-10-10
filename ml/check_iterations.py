"""Compare the winning 500-iteration model with a 1,500-iteration ceiling.

Run:
    python -m ml.check_iterations

Uses training and tuning data only.
"""

import json

from .evaluate_model import evaluate
from .train import CONFIG_PATH, train
from .tune import PREPARED, RUNS, validate_existing_run


def main():
    original = RUNS / "leaves_16_min_100"
    extended = RUNS / "leaves_16_min_100_max_1500"

    manifest = json.loads(
        (PREPARED / "manifest.json").read_text()
    )
    config = json.loads(CONFIG_PATH.read_text())
    config.pop("model", None)

    original_parameters = {
        **config,
        "num_leaves": 16,
        "min_child_samples": 100,
        "n_estimators": 500,
    }

    # Confirm the original experiment matches our comparison.
    validate_existing_run(
        original, original_parameters, manifest
    )

    overrides = {
        "num_leaves": 16,
        "min_child_samples": 100,
        "n_estimators": 1500,
    }
    extended_parameters = {**config, **overrides}

    if (
        (extended / "model.txt").exists()
        and (extended / "metadata.json").exists()
    ):
        print("Reusing the completed extended run.")
    else:
        if extended.exists() and any(extended.iterdir()):
            raise RuntimeError(
                f"{extended} contains an incomplete run. "
                "Inspect it before continuing."
            )

        train(output=extended, overrides=overrides)

    metadata = validate_existing_run(
        extended, extended_parameters, manifest
    )

    # Evaluate both models with exactly the same scoring procedure.
    original_report = evaluate(run=original)
    extended_report = evaluate(run=extended)

    original_score = original_report["mean_horizon_brier"]
    extended_score = extended_report["mean_horizon_brier"]

    improvement = (
        (original_score - extended_score) / original_score
    )

    print("\n=== Iteration-limit comparison ===")
    print(
        "Extended run selected iterations:",
        metadata["best_iteration"],
    )
    print(f"500-limit mean Brier:   {original_score:.6f}")
    print(f"1500-limit mean Brier:  {extended_score:.6f}")
    print(
        "Improvement over the 500-limit model:",
        f"{improvement:+.2%}",
    )

    print("\nHorizon | 500-limit Brier | 1500-limit Brier")

    for horizon in [7, 14, 30, 60]:
        old = original_report["horizons"][str(horizon)]["brier_score"]
        new = extended_report["horizons"][str(horizon)]["brier_score"]

        print(f"{horizon:7d} | {old:.6f}        | {new:.6f}")

    comparison = {
        "original_run": original.name,
        "extended_run": extended.name,
        "extended_best_iteration": metadata["best_iteration"],
        "original_mean_brier": original_score,
        "extended_mean_brier": extended_score,
        "relative_improvement_over_original": improvement,
        "partition": "validation_tuning",
        "final_model_locked": False,
    }

    (extended / "iteration_comparison.json").write_text(
        json.dumps(comparison, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()