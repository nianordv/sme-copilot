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
LightGBM training dependencies will be added with the training stage; the
current scripts do not require LightGBM or make any paid API requests.

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

## Outgoing invoices

`generate_outgoing_invoices_ge.py` generates the other side of the ledger: the
bills the company receives from its own suppliers and pays. It is modelled on
`generate_invoices_ge.py` and shares its seed, date window, snapshot date and
Georgian holiday calendar. Run it after the incoming generator:

```sh
python -m ml.generate_outgoing_invoices_ge
```

Output: `data/outgoing_invoices_georgia.csv` and
`data/outgoing_invoices_metadata.json`, both ignored by Git. One row per bill,
11 ledger columns: `invoice_id` (`BILL-000001`), `supplier_id`,
`supplier_category`, `supplier_city`, `expense_type` (`stock` or `overhead`),
`issue_date`, `due_date`, `paid_date`, `status`, `amount`, `payment_terms`.
Statuses are `paid`, `open_not_due` and `open_overdue`; a payment planned for
after the snapshot is not exported. There are no write-offs and no model
feature columns.

Purchases follow sales. The script reads the incoming CSV, and each category's
bills add up to a fixed share of what the company invoiced, larger in the weeks
when it sold more. Regenerate this file whenever the incoming CSV changes; the
metadata records which incoming file it was sized against.

| Kind | Categories | Share of sales |
| --- | --- | ---: |
| Stock | Beverages, meat and poultry, dry goods, dairy, produce, frozen foods, coffee and tea, packaging | 80.0% |
| Overhead | Fuel and transport, rent, utilities, services | 4.7% |

The remaining 15.3% is what is left for salaries, taxes and profit, none of
which are invoices. These shares are design assumptions set in the `CAT` table
at the top of the script, not measured figures.

How the company pays, also assumptions:

- It means to pay a couple of days before the due date.
- Payments leave in batches on Tuesdays and Fridays, and never on a weekend or
  public holiday.
- A bill due from the 8th to the 15th waits about three days longer, because
  the company's own salaries (10th) and taxes (15th) come first.
- January bills wait about two days longer. Two in a hundred are disputed and
  held back 10 to 40 days. An unusually large bill is paid later.
- Rent and utilities go by direct debit and are never late.

Two things differ on purpose from the incoming generator: bills are spread
evenly over working days instead of piling up on Mondays, and each supplier
keeps one payment term.

Figures from the current run, against the 50,319-invoice incoming file:

| | |
| --- | ---: |
| Bills | 15,274 |
| Suppliers | 56 |
| Total billed | GEL 48,210,132 |
| Paid bills that were paid late | 47.7% |
| Median days late | 0 |
| Unpaid at the snapshot | 208 bills, GEL 828,925 |
| Of which due within 30 days | GEL 623,255 |
| Of which already overdue | 5 bills, GEL 14,982 |

Limitations: payment timing does not react to the company's real bank balance,
so the company never delays a bill because it is actually short. Bills not yet
issued on the snapshot date are not in the file, so known bills cover less of
the outflow the further ahead you look. Supplier names are not generated, only
IDs.

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

The model is not trained yet. `model_config.json` defines the initial CPU
LightGBM settings. The next stage adds a censor-aware empirical baseline,
LightGBM fitting and tuning, calibration, and an untouched test evaluation.
Early stopping: up to 500 trees, patience 30 on tuning-set binary log loss.
Candidate leaves {8,16} and minimum leaf samples {100,200,400}; compare to the
baseline using invoice-level 7/14/30/60-day Brier scores. Calibration uses only
October–December 2025. Test results must not drive model selection.

Future probabilities are obtained by chaining hazards:
`P(paid within H days) = 1 - product(1 - daily_hazard)`.
Only evaluate horizons with full follow-up, even for invoices paid early.
Receipt evaluation must include the same outstanding invoice cohort in both
prediction and actual receipts. Future invoices are excluded.

Limitations: full payments only, assumed payroll dates, no common economic
shocks, no empirically calibrated Georgian customer behavior. Cash-gap
validation additionally requires balances and dated outflows, absent here.
