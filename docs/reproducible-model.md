# Making the trained model reproducible

Oct 19, 2026. For nianordv, from Roni.

After `release/submission` was merged, a review with Codex found two problems:

1. The setup instructions never train the selected LightGBM model, so anyone who follows them gets the baseline forecast instead.
2. The README said scikit-learn is not pinned, but `ml/requirements.txt` pins it.

The second is already fixed on this branch (commit `edb5768`). The first is yours, and this note says what to do.

## How to use this note

- **Work on this branch.** `fix/reproducible-model` starts from `release/submission` and already holds the scikit-learn fix. When you are done, delete this note and merge the branch into `release/submission`.
- **The prompt below can be pasted into a coding assistant as it stands.** Its "Done when" line is your check, so you do not have to trust the assistant's summary.
- **The deadline** is 20 October, 24:00 China time.

## What we found

| Finding | Where |
| --- | --- |
| Step 2 runs `ml.baseline` and then `ml.score_open_invoices`. Nothing trains the selected model, so scoring prints "selected model files missing" and uses the baseline. | `README.md`, Step 2 |
| Scoring only looks in `ml/artifacts/lightgbm/leaves_16_min_100` (16 leaves, at least 100 rows per leaf, 500 trees). But `python -m ml.train` trains a different configuration (8 leaves, 200 rows) into `first_run`, and `python -m ml.tune` trains all six. No command trains only the selected one. | `ml/score_open_invoices.py`, `ml/train.py`, `ml/tune.py` |
| As far as we know, the only trained copy is on your computer: Git ignores `ml/artifacts/`. | `.gitignore` |
| Retraining should give the same model: `random_state` is 42, `deterministic` is true, training uses one thread, and the versions are pinned. | `ml/model_config.json`, `ml/requirements.txt` |
| On a Mac, LightGBM needs `libomp`, and the README never says so. Without it, `ml.score_open_invoices` crashes at `import lightgbm` with an `OSError`, so there is no forecast at all, not even the baseline. We found this on Roni's Mac; Codex did not mention it. | `README.md`, Step 1 |

## The prompt

```text
Read README.md (section 4, "Install and run"), ml/README.md, ml/train.py, ml/tune.py and ml/score_open_invoices.py. Work on the branch fix/reproducible-model.

Someone with a fresh clone who follows the README must end up with the selected LightGBM model, not the baseline. Make the setup train it before scoring.

- ml/train.py: add a --selected option. python -m ml.train --selected trains only the selected configuration (ml/model_config.json with num_leaves 16 and min_child_samples 100) into ml/artifacts/lightgbm/leaves_16_min_100, the folder ml.score_open_invoices reads. Plain python -m ml.train keeps doing what it does now.
- If that folder already holds a complete run, do not train again: check it the way validate_existing_run in ml/tune.py does, and say that it is reused. An incomplete run still stops with a clear message.
- README.md, Step 1: on a Mac, also run brew install libomp, because LightGBM needs it.
- README.md, Step 2: add python -m ml.train --selected after python -m ml.baseline and before python -m ml.score_open_invoices. Replace the paragraph that begins "ml.score_open_invoices uses the trained model" with what happens now, including how long training took on your machine.
- ml/README.md: the paragraph about train and tune says they are not for starting the app. Change it: ml.train --selected is now part of setup, while ml.tune and the check scripts rebuild the experiment.
- backend/forecast/inputs.py: the error "Cannot refresh payment curves..." should also say that a Mac needs brew install libomp.

Do not change the model's settings, the data or any evaluation result.

Done when: in a fresh clone (not your working folder, which holds the evaluated model), README Step 2 runs without errors; ml/artifacts/forecast/metadata.json says "model_used": "leaves_16_min_100" and "fallback": false; the new ml/artifacts/lightgbm/leaves_16_min_100/metadata.json has the same source_sha256, prepared_manifest_sha256, training_arrays_sha256 and model_sha256 as the metadata.json of your saved, evaluated model; and the tests in README section 5 pass.
```

## If the fingerprints do not match

Matching fingerprints prove that the README rebuilds the exact model behind our published results. If they do not match, the first one that differs tells you where:

- **`source_sha256`:** the generated invoices differ between the two machines, so the README's "The same seed gives the same file every time" is not true. Compare the versions recorded in both copies of `ml/data/dataset_metadata.json`.
- **`prepared_manifest_sha256` or `training_arrays_sha256`:** the invoices match, but `ml.prepare_data` gives different files.
- **Only `model_sha256`:** training does not give a bit-identical model on another machine. Then also commit your evaluated folder with `git add -f ml/artifacts/lightgbm/leaves_16_min_100`, and say in the README that the included model is the evaluated one and that `ml.train --selected` rebuilds a close copy.
