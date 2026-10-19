# Payment forecasting

For a plain-language explanation of generation, history, splits, checks, and
limitations, read [How we generate and handle invoice data](../docs/data-generation-and-handling.md).

All data here is synthetic: one simulated Georgian supplier, 500 customers,
GEL amounts, and an end-of-day snapshot on 2026-10-03. Results cannot establish
real-world forecasting accuracy or generalization to other suppliers.

## Setup and commands

From the repository root, create a separate ML environment:

```sh
python3 -m venv ml/.venv
source ml/.venv/bin/activate
python -m pip install -r ml/requirements.txt
python -m ml.generate_invoices_ge
python -m unittest discover -s ml/tests -v
python -m ml.prepare_data
```

The generation/preparation dependencies are pinned in `requirements.txt`.
LightGBM 4.7.0 is pinned alongside the data dependencies. No paid API is used.

## Corrected generation

Adapted from the team's `generate_invoices_ge.py`. The original behavior model
is retained, with these corrections:

- May 17 is a public holiday starting in 2024, not 2023.
- The private never-payment draw is never exported. A write-off becomes
  observable at due date + 90 days, an explicit synthetic policy. Before that
  date it is simply an unpaid invoice. `writeoff_date` is blank until observed.
- History uses payments strictly before issue date. Same-day payment ties use
  invoice ID for a reproducible ordering.
- Amount history and outstanding balances use strictly earlier issue dates.
  Payments/write-offs strictly before issue date close prior invoices.
- History retains full precision; money is generated to two decimal places.
- Importing the generator does not run it. Every generation resets seed 2026.

Output: `data/invoices_georgia.csv` and `data/dataset_metadata.json` (hashes,
versions, snapshot, assumptions). Files are ignored by Git and regenerated
locally. The same seed is reproducible with the same dependency versions.
Changes to holidays can change later random draws and therefore invoice counts.

## Model preparation

Daily binary label: payment on this date, conditional on being unpaid before
this date. Rows start the day after issue and end at the earliest of observed
payment, observation cutoff, or due date + 365 days. An unpaid invoice is
right-censored; it is not assigned an invented payment date or a permanent
default label. Written-off invoices remain payment non-events in this synthetic
full-settlement simulation, with the same finite observation limit.

| Partition | Invoice issue dates | Observe payments through |
| --- | --- | --- |
| Train | 2022–2024 | 2024-12-31 |
| Validation: tuning | January–September 2025 | 2025-09-30 |
| Validation: calibration | October–December 2025 | 2025-12-31 |
| Test | January–September 2026 | 2026-10-03 |

`prepare_data.py` rebuilds history, learns category vocabularies only from
training invoices, and saves `artifacts/prepared/*.npz`. Each contains `X`, `y`,
invoice row references, candidate dates, and invoice IDs for evaluation joins.
`manifest.json` records membership, cutoffs, the source hash, and feature order.
Matching invoice CSVs contain evaluation outcomes; those fields never enter X.

Predictors: invoice amount/terms/issue month, segment/city, prior client payment
history and outstanding exposure, due-date calendar, current invoice age and
days past due, and candidate-date calendar. IDs, final status, observed payment
calendar, write-off date, and outcome targets are excluded from X. There is no
class balancing or negative-row sampling, preserving probability estimation.

The selected model is trained: `artifacts/lightgbm/leaves_16_min_100`, with
500 trees, 16 leaves and minimum 100 rows per leaf. Six preplanned configurations
were compared on tuning data against a censor-aware empirical baseline using
invoice-level 7/14/30/60-day Brier scores. An extended run selected 983 trees
but its small additional benefit did not justify replacing the 500-tree model.
A calibration adjustment was checked on a later held-out calibration period and
was not retained. The unchanged selected model was evaluated on the test split.
Do not use those test results for further model selection.

The scripts are `baseline`, `train`, `tune`, `check_iterations`,
`evaluate_calibration`, `check_calibration_adjustment`, `evaluate_receipts`,
`evaluate_test` and `check_robustness`, all run as `python -m ml.<name>`.
`python -m ml.train --selected` is part of app setup, after `ml.baseline` and
before `ml.score_open_invoices`. It validates and reuses the included selected
model, or trains only that configuration if its folder is absent or empty.
Incomplete runs and mismatched fingerprints stop without overwriting files.
Plain `python -m ml.train` still trains the original configuration into `first_run`.
`ml.tune` and the check scripts rebuild the experiment, rather than app setup.
Preserve the evaluated selected model and its metadata for the demo. On macOS,
install the LightGBM runtime with `brew install libomp` first.

Reproduction was checked on 19 October 2026 in a separate clean checkout using
our pinned ML environment. Generating the data and preparing it validated the
included model. Training again without that temporary model copy took about
80 seconds and matched the evaluated model's source, manifest, training-array,
tuning-array and model SHA-256 fingerprints exactly. Scoring reported
`model_used: leaves_16_min_100` and `fallback: false`. This verifies the rebuild
on our machine; byte-identical results across other operating systems are not
assumed. The evaluated model remains included in the repository.
Exact evaluation results are in the saved JSON reports; lower Brier and receipt
error are better, and improvement is relative to the same baseline and cohort.

Future probabilities are obtained by chaining hazards:
`P(paid within H days) = 1 - product(1 - daily_hazard)`.
Only evaluate horizons with full follow-up, even for invoices paid early.
Receipt evaluation must include the same outstanding invoice cohort in both
prediction and actual receipts. Future invoices are excluded.

Limitations: full payments only, assumed payroll dates, no common economic
shocks, no empirically calibrated Georgian customer behavior. Cash-gap
validation additionally requires balances and dated outflows, absent here.


## App scoring

`python -m ml.score_open_invoices` writes the selected model's full 60-day curves
for invoices outstanding on the synthetic snapshot date, plus metadata and source
hashes. If the selected model files are missing, it explicitly uses the saved
empirical baseline. The backend validates that these curves match its imported
books before using them.

`ml.live_score` is the backend's local scoring worker for a later reporting date.
It scores the frozen predictor again using new invoice ages and calendar dates;
it does not train on test data or send records to an external service. The backend
removes invoices reported fully paid and applies the curve to remaining amounts
for partial receipts. This partial-invoice timing assumption is not separately
validated. Customer-history features retain the issue-date definition used in
training. New-invoice import and a retraining scheduler are outside this demo.

The app's cash ledger, existing loan schedules and future-sales assumption are
separate from ML. The trained model predicts invoice settlement probabilities,
not bank balances, credit approval or an exact guaranteed payment date.
