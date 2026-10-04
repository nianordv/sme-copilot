# Rightsize: how we generate and handle invoice data

This document explains the current data pipeline in plain language. It is for
teammates reviewing the project and for preparing our presentation, pitch, or
README. It describes implemented data generation and preparation, rather than
claiming results from a trained model. A separate model document will follow
after training and evaluation are complete.

## 1. What the dataset represents

We simulate one Georgian supplier selling to 500 business customers, including
restaurants, cafes, hotels, bars, catering businesses, and grocery stores.
Amounts are in Georgian lari (GEL). Each CSV row represents one invoice.

The corrected dataset currently contains **50,319 invoices and 39 columns**.
The generation window runs from January 2022 through September 2026, with a
snapshot taken at the end of October 3, 2026. This is nearly five years of data,
not five complete calendar years.

**All records are synthetic.** These are simulated customers and transactions,
not anonymized bank records or invoices collected from real businesses. The
behavioral parameters are design assumptions, not estimates validated against
the Georgian economy. We use this data to develop and test the prototype under
controlled conditions.

The dataset represents a single supplier's customer ledger. Having 500
customers does not mean we have data from 500 independent suppliers.

## 2. How customers and invoices are generated

The generator first creates customers with different business segments,
cities, typical order sizes, preferred payment terms, and payment behavior.
Customers join at different times, and some stop ordering before the end of
the dataset. They do not all have the same number of invoices.

Private simulation characteristics make some customers usually punctual and
others slower or less consistent. These hidden characteristics are used to
create the data, but are not exported as model predictors. The forecasting
system must work from observable invoice and payment history.

For each customer, the generator samples invoice dates across their active
period and moves issue dates onto business days. The expected invoice counts
are defined over the overall generation period, not as annual counts.
Amounts vary around each customer's typical order size, with occasional larger
orders. Amounts are positive and rounded to two decimal places.

Invoices use payment terms of 15, 30, or 60 calendar days. The due date is the
issue date plus those terms; it is not automatically moved to a business day.
An invoice can therefore be due on a weekend or holiday.

Invoice dates are sampled approximately uniformly within a customer's active
period. We model some seasonal payment behavior, but do not currently model a
realistic seasonal sales-volume curve. More invoices in a later year may also
reflect customers joining the supplier.

## 3. What affects simulated payment timing

Payments vary with the customer's underlying behavior and random variation.
The generator also accounts for:

- Invoice size, including how unusually large it is for that customer.
- Payment terms and gradual changes in a customer's behavior over time.
- Business segment and city assumptions.
- Seasonal payment-delay assumptions, such as hotels paying faster in summer.
- An assumed payroll window from the 12th through the 15th of each month.
- Extra pressure near month end.
- Weekend and Georgian public-holiday effects.
- Some customers using automatic payments.
- Occasional unusually long delays and simulated disputes.
- A small chance that an invoice never pays in the simulation.

These factors create varied examples; they do not prove that the chosen
relationships or their sizes match real businesses. Payroll on the 15th is an
explicit assumption, not a rule that applies to every Georgian customer.

Payments may be early or late, but cannot happen on or before the issue date.
Payments planned for nonworking days may move before or after the break; a
small conditional chance allows the planned payment to remain on that day.
That conditional probability is not the percentage of all payments expected
to occur on weekends and holidays.

Every paid invoice is settled in full in one payment. Partial payments,
installments, refunds, and recoveries after write-off are not modeled.

## 4. The snapshot and write-off rules

The CSV represents what is observable by the end of October 3, 2026. A payment
scheduled after that date is not exported as a known payment. Its `paid_date`
and payment targets are blank.

Statuses mean:

| Status | Meaning at the snapshot |
| --- | --- |
| `paid` | Full payment has been observed. |
| `open_not_due` | Unpaid, with a due date on or after the snapshot date. |
| `open_overdue` | Unpaid, due before the snapshot, with no observed write-off. |
| `written_off` | The simulated write-off date has been reached. |

The simulation privately decides that some invoices will never pay. We do not
immediately reveal that future outcome by marking a new invoice written off.
Instead, our synthetic accounting policy records a write-off **90 days after
the due date**. `writeoff_date` is exported only after that date is observable.
Until then, the invoice appears as an ordinary unpaid invoice.

The 90-day policy is a project assumption, not a legal or accounting
requirement. An overdue invoice is not automatically a write-off: only the
invoices selected as never-paying by the simulation follow this policy.

## 5. Corrections to the original teammate-provided generator

We retained the original behavior simulation and corrected these issues:

1. **Historical holiday year:** May 17 is included from 2024 rather than 2023.
2. **Premature write-off information:** write-offs now have an observable date
   instead of revealing a never-payment outcome immediately.
3. **Same-day ordering:** invoices issued on the same day do not enter one
   another's issue-time history. Dates alone cannot establish which came first.
4. **Payment ordering:** payments sharing a date use invoice ID as a consistent
   tie-breaker when calculating recent-payment summaries.
5. **Precision:** customer history retains full numerical precision rather than
   rounding every statistic to two decimals. Invoice money remains rounded to
   two decimal places.
6. **Safe execution:** importing generator helpers does not generate or
   overwrite the CSV. Each explicit generation starts with the same seed.

Correcting a holiday can change subsequent random draws and the resulting
invoice count. The corrected CSV is therefore a new dataset version; results
from the original CSV should not be mixed with it.

## 6. What information is available when an invoice is issued

The main protection against data leakage is to recreate the information that
would actually have been known at the time of a prediction. Data leakage means
giving the model information from the future that would not be available in use.

For an invoice issued on date D, customer history uses events strictly before
D. Same-day events are excluded because we do not have timestamps.

We calculate:

| History measure | Interpretation |
| --- | --- |
| Previously paid invoice count | How much observed payment history is available. |
| Mean and median payment delay | Whether the customer generally pays early or late. |
| Delay spread | How variable their previous delays are. |
| Most recent delay | Their latest observed payment behavior. |
| Recent-three average | Average delay over up to three most recent payments. |
| Delay trend | Recent-three average minus the preceding-three average. |
| Late-payment fraction | Share of previous payments made after the due date. |
| Relative invoice amount | Current amount divided by the median earlier invoice amount. |
| Other open invoice count | Number of earlier invoices still open. |
| Outstanding amount | Total value of those earlier open invoices. |

The code name `client_num_past_invoices` specifically means previously
**paid** invoices, not every invoice ever issued. Payment summaries use only
observed payments; outstanding exposure includes earlier unpaid invoices.
Observed payments and write-offs close exposure only when their dates are
strictly before the current issue date.

A customer with insufficient history has missing values rather than invented
zero delays. Delay spread requires two payments, trend requires six payments,
and relative invoice amount requires three earlier issued invoices. Negative
delay means early payment; positive delay means late payment.

These history measures are frozen at the invoice's issue date. The current
pipeline does not refresh them on every later unpaid day. Future prediction
code must follow the same feature definition.

## 7. Preparing inputs without revealing outcomes

Preparation rebuilds history from ledger dates instead of trusting the
precomputed CSV history columns. It also rebuilds due-date calendar features.
The input columns are explicitly selected.

Predictors include invoice amount, payment terms, issue month, customer segment
and city, observable customer history and exposure, due-date calendar, invoice
age, days past due, and the calendar of the day being evaluated.

The model input excludes final status, actual payment date, write-off date,
actual payment-day calendar, days until payment, final days late, and the final
late-payment flag. Invoice and client IDs are used for grouping and joins, but
are not predictors. Hidden generator characteristics are also excluded.

Actual payment dates are still necessary to construct labels and summarize
previously observed payments. Using them for those purposes is different from
giving the current invoice's future payment date to the model.

Future calendar dates are known information: we can calculate that a candidate
day is a Sunday without knowing whether an invoice will actually pay that day.
The calendar uses the project's specified holiday rules; it does not model
every exceptional bank closure or changing payment-processing practice.

Segment and city vocabularies are learned only from training invoices and
reused for later partitions. Unseen categories receive a missing-category code.
The future trainer must explicitly identify these columns as categorical.

No feature scaling, class balancing, or negative-row sampling is performed in
the current preparation. Missing history is preserved. Numerical predictors
are stored as 32-bit floating-point values for memory efficiency; exact money
reconciliation should use integer minor units or decimal arithmetic elsewhere.

## 8. Chronological splits and observation cutoffs

We keep the chronological order rather than randomly mixing old and new
invoices. Every invoice belongs to exactly one partition, and all its daily
observations remain in that partition.

| Partition | Invoice issue window | Observe outcomes through | Current invoice count |
| --- | --- | --- | ---: |
| Training | January 2022–December 2024 | December 31, 2024 | 29,609 |
| Validation for tuning | January–September 2025 | September 30, 2025 | 8,704 |
| Validation for calibration | October–December 2025 | December 31, 2025 | 3,057 |
| Test | January–September 2026 | October 3, 2026 | 8,949 |

Training learns patterns, tuning supports choosing settings, calibration
supports correcting probabilities, and test data measures the finished system.
These later model activities are not performed by data preparation itself.

An invoice issued in December 2024 belongs to training even if paid in January
2025. Its January payment must remain hidden from training labels. Splitting
only by issue date without limiting outcome visibility would leak future data.

The same customer can appear in several partitions. This represents predicting
future invoices for an ongoing supplier. It does not independently establish
performance on new suppliers or entirely unseen customers.

History can include earlier payments from a later partition when those payments
were already observed before a subsequent invoice was issued. That represents
normal ledger updates over time, not refitting the model on test outcomes.

## 9. From one invoice to daily observations

The prepared data has one row for each day an invoice remains unpaid, beginning
the day after issue. A row's label is 1 if payment occurs on that day, otherwise
0. The payment day is included; there are no rows after payment.

For an invoice issued January 1 and paid January 5:

| Observation date | Age in days | Payment label |
| --- | ---: | ---: |
| January 2 | 1 | 0 |
| January 3 | 2 | 0 |
| January 4 | 3 | 0 |
| January 5 | 4 | 1 |

Observations stop at the earliest of observed payment, the partition's cutoff,
or 365 days after the due date. This cap limits the long unpaid tail; it is not
a declaration of permanent default.

If an invoice is still unpaid when observation stops, we know only that it did
not pay during the observed period. We do not assign an invented payment date.
This is called **right-censoring**. A daily zero means “no payment today,” not
“this invoice will never pay.”

Written-off invoices remain payment non-events through the same observation
limit in the current synthetic full-settlement setup. Their rows do not stop at
the write-off date. This treatment would need review for real data containing
recoveries or different accounting policies.

The current prepared dataset contains **1,807,153 daily rows and 36 predictors**.
Repeated days from one invoice are related observations, not independent new
invoices. We should not describe the data as 1.8 million independent customers
or transactions.

## 10. Checks, analysis, and reproducibility

The generator uses seed 2026 and pinned preparation dependencies. Repeated runs
with the same code and environment produce the same CSV. We checked two runs
and confirmed identical CSV contents. Different code or dependency versions
can change results, so the seed alone is not a complete reproducibility record.

Generation saves metadata with the snapshot, currency, seed, assumptions,
dependency versions, and hashes of the CSV and generator. Preparation checks
the CSV hash against that metadata and records the source hash in its split
manifest. Hashes detect changed files; they do not prove economic realism.

Implemented preparation checks include unique invoice IDs, present client IDs,
positive amounts, supported terms, consistent due dates, payments after issue,
no observed events beyond the snapshot, valid split cutoffs, and complete,
nonoverlapping invoice membership. These are important checks, but not a
complete validator for every possible external accounting dataset.

The separate integrity review also checked payment-target consistency,
write-off dates, status consistency, missing targets on unpaid records, array
dimensions, observation cutoffs, and at most one payment event per invoice.
Eight automated tests cover history visibility and ordering, censoring,
payment-day boundaries, the tail cap, calendar behavior, unknown categories,
and exclusion of outcome fields from predictors.

Current snapshot counts are:

| Status | Invoices |
| --- | ---: |
| Paid | 48,995 |
| Open, not yet due | 923 |
| Open, overdue | 268 |
| Written off | 133 |

Among paid invoices, approximately 56.6% paid late, and median delay was two
days after the due date. These describe the generated sample, not measured
Georgian market statistics. Payment-delay summaries based only on paid invoices
exclude the unpaid tail and must be labeled accordingly.

When analyzing future versions, inspect distributions and changes by year,
segment, city, terms, invoice amount, customer history, and invoice age. Check
missingness, unusually large amounts, long delays, customer entry/churn, and
how much follow-up is available. Counts alone do not establish data quality.

For future evaluation, include only forecast horizons with complete follow-up,
even when an invoice happened to pay early. Otherwise, we would favor examples
whose outcomes are easier to observe. Receipt comparisons must use the same
invoice cohort in predicted and actual receipts, excluding future invoices
that were not known at the forecast date.

## 11. Files and how to regenerate them

Repository paths:

| File or directory | Purpose |
| --- | --- |
| `ml/generate_invoices_ge.py` | Generates the corrected synthetic ledger. |
| `ml/data/invoices_georgia.csv` | Local generated invoice dataset. |
| `ml/data/dataset_metadata.json` | Generation assumptions, versions, and hashes. |
| `ml/prepare_data.py` | Builds features, splits, daily observations, and labels. |
| `ml/artifacts/prepared/` | Prepared numerical arrays, reference CSVs, and manifest. |
| `ml/tests/test_pipeline.py` | Automated checks for important preparation rules. |
| `ml/requirements.txt` | Pinned generation/preparation dependencies. |

The prepared arrays contain predictors (`X`), daily labels (`y`), candidate
dates, and invoice references. The manifest records exact split membership,
cutoffs, category mappings, and feature order. Training and prediction must
preserve that feature order.

**Reference invoice CSVs retain outcome fields, including payments beyond an
individual partition's cutoff.** The prepared arrays hide those outcomes
correctly. Any future evaluation code reading the reference CSVs must enforce
the same observation cutoff; those files must not be treated as fully
observable historical snapshots.

Generated data and artifacts are ignored by Git. Teammates can recreate them
from the tracked code. From the repository root:

```sh
python3 -m venv ml/.venv
source ml/.venv/bin/activate
python -m pip install -r ml/requirements.txt
python -m ml.generate_invoices_ge
python -m unittest discover -s ml/tests -v
python -m ml.prepare_data
```

Generation and preparation write local outputs; they do not train a model,
make paid API calls, commit code, or push to GitHub. Rerunning overwrites the
corresponding generated files. Review data changes and regenerate prepared
artifacts before comparing experiments from a new dataset version.

## 12. What this dataset can and cannot demonstrate

It can help demonstrate a reproducible data pipeline, handling of incomplete
payment observations, prevention of future-information leakage, and later
forecasting experiments under the simulation's assumptions.

It cannot establish real-world prediction accuracy, representativeness of
Georgian businesses, or performance on other suppliers. The simulation has no
shared economic shocks, actual payment batches, partial settlements, or
empirically estimated behavioral parameters. Known calendar patterns may make
the simulated task easier than real payment forecasting.

The CSV also has no actual starting bank balance, salaries, tax obligations,
or other scheduled outflows. Invoice receipts alone cannot validate a cash-gap
forecast or its uncertainty coverage. Those require additional dated inputs.

There are no trained-model performance results at this stage. We should also
avoid claims that dataset checks guarantee competition acceptance; the team
must check the competition's data and disclosure requirements separately.

## 13. Wording we can reuse

### README or presentation paragraph

Rightsize currently uses a reproducible synthetic invoice dataset representing
one Georgian supplier and 500 business customers, with 50,319 invoices across
nearly five years. The generator includes varied payment terms, customer
behavior, calendar effects, delayed payments, and observable write-offs.
Preparation reconstructs history available when each invoice was issued and
keeps invoices in chronological training, validation, and test partitions.
Payments beyond each partition's observation cutoff are hidden, while unpaid
invoices are treated as incomplete observations rather than assigned invented
payment dates. This supports prototype development under documented simulation
assumptions; real-world validation remains future work.

### Short slide bullets

- 50,319 synthetic invoices, 500 customers, one supplier, GEL amounts.
- Varied payment behavior, terms, calendar effects, and dated write-offs.
- History reconstructed using only information available before issue.
- Chronological partitions with explicit payment-observation cutoffs.
- Reproducible generation and automated preparation checks.
- Prototype evidence from simulated data; real-world validation still needed.

Update these counts and descriptions if generation or preparation rules change.
