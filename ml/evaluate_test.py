"""Record frozen selection, then evaluate the final chronological test set.

Run: python -m ml.evaluate_test
No training or calibration fitting is performed.
"""

import json
from datetime import datetime, timezone

from .evaluate_calibration import main as evaluate
from .evaluate_model import file_hash
from .evaluate_overdue import PREPARED, RUN
from .evaluate_baseline import BASELINE
from .evaluate_receipts import load_models


def main():
    manifest = json.loads((PREPARED / "manifest.json").read_text())
    load_models(manifest)
    metadata = json.loads((RUN / "metadata.json").read_text())
    selection = {
        "run": RUN.name,
        "parameters": metadata["parameters"],
        "selected_iterations": int(metadata.get("best_iteration", 500)),
        "calibrated": False,
        "calibration_decision": "October-fitted daily adjustment rejected after later-invoice checks.",
        "model_sha256": file_hash(RUN / "model.txt"),
        "baseline_sha256": file_hash(BASELINE / "hazards.npz"),
        "prepared_manifest_sha256": file_hash(PREPARED / "manifest.json"),
        "test_arrays_sha256": file_hash(PREPARED / "test.npz"),
        "test_invoices_sha256": file_hash(PREPARED / "test_invoices.csv"),
        "evaluation": "7/14/30/60-day probabilities at issue/due/due+14; weekly 30/60-day receipts",
        "policy": "Test outcomes are reported, not used to select configuration or calibration.",
    }
    path = RUN / "final_selection.json"
    if path.exists():
        previous = json.loads(path.read_text())
        previous.pop("recorded_at_utc", None)
        if previous != selection:
            raise ValueError("Frozen selection or test inputs changed. Preserve the previous evaluation.")
    else:
        path.write_text(json.dumps({**selection,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n")
    print("Frozen selection recorded: 16 leaves, minimum 100, 500 trees; no adjustment.", flush=True)
    evaluate("test", allow_test=True)


if __name__ == "__main__":
    main()
