# Rightsize: review of the invoice data and its generator

Oct 4, 2026 · Review only, no code was changed

**What was reviewed.** Branch `feat/payment-forecast` at commit `c65a75d`: the generator `ml/generate_invoices_ge.py`, the preparation step `ml/prepare_data.py`, and the data they produce. The data was regenerated from that commit and checked with separate analysis scripts. Every figure below comes from that run. The generated CSV has SHA-256 `c095be6a33b198b7c8047996aefcb8b1ba5ac5eaae25eaca05c6a7356cea636a`; if your file has a different hash, these figures may not match yours.

## Verdict

The data is clean, reproducible, and protected against the model seeing the future, so it is sound for training the payment model. Its weakness is that nothing in it can cause a cash shortage, so the model can score well while the product has no gap to show.

## Advantages

- **It reproduces exactly.** The run gave 50,319 invoices with the same status counts as the team's doc, and two runs gave byte-identical files. Versions are pinned, and the prep step refuses a CSV whose hash does not match.
- **The rows are consistent.** There are no duplicate IDs, every due date equals issue date plus terms, no payment falls on or before issue, and status agrees with the dates on every row.
- **No future information reaches the model.** Customer history uses only payments strictly before the issue date, and the hidden "good/average/slow" label is never exported. A never-paid invoice is revealed only 90 days after its due date. This code was read line by line and no leak was found; the team's 8 tests pass.
- **The test year is untouched.** No model script opens the 2026 or calibration partitions.
- **The overall shape is believable.** 57% of paid invoices are late, the median is 2 days late, 4% are more than 30 days late, and 0.26% are written off. The holiday list looks right, though it was not checked against an official source.
- **Limits are stated honestly** in `docs/data-generation-and-handling.md`.

## Disadvantages, by impact

### 1. Cash-in is too steady to create a gap

- Across 192 weekly dates, the share of open invoices collected in the next 30 days was 64.6%, give or take 2.6 points.
- A rule of thumb ("we collect the usual share") is off by 3.2% on average, about GEL 29,000, and by 7.4% in the worst tenth of cases.
- The biggest customer is 1.3% of sales, about GEL 14,000 a month.
- So no single late payer can matter: the banker story and owner question 5 ("Client A paid. Do I still need the overdraft?") cannot happen here.
- The likely/cautious/worst spread will be a few percent.

### 2. Customers never have a bad spell, and non-payment is a coin flip

- After an invoice more than 30 days late, the same customer's next invoice is back to normal (correlation between consecutive delays is -0.02).
- Write-off is drawn per invoice, independent of delays or disputes (`ml/generate_invoices_ge.py:255`). Of 92 customers with a write-off, 91 kept ordering and 94% of their later invoices were paid.
- The model will learn to ignore `last_delay`, `avg_of_last_3_delays` and `delay_trend`, which matter on real data.
- The worst case is understated, because bad news never clusters.

### 3. The forecast covers only invoices that already exist

Open invoices supply 99% of the next 7 days' receipts, 87% of the next 30, and 63% of the next 60. The other 37% of a 60-day cash forecast needs a sales assumption that nobody has built.

### 4. The calibration slice barely contains late invoices

Calibration means adjusting the model so that "70%" comes true about 70% of the time. That slice (`ml/prepare_data.py:23`) holds invoices issued October to December 2025, watched only to 31 December. As a result, 41% are still unpaid at the cutoff, only 32 invoices are seen 30 to 60 days overdue, and 4 beyond that. Overdue invoices are what question 2 depends on.

### 5. Smaller artefacts

| Finding | Evidence | Where |
| --- | --- | --- |
| Monday pile-up | 41% of invoices are issued on Monday, so 30-day invoices are mostly due on Wednesday and 60-day ones on Friday | `generate_invoices_ge.py:210` |
| Terms change per invoice | 92% of customers have all three terms; real terms are fixed per customer | `:213` |
| Customers drift apart forever | Spread of customers' yearly average delay grows from 7.0 days (2022) to 10.8 (2026), so the test year is wider than training | `:219` |
| Clamp spike | 711 invoices are exactly 15 days early, against 51 at 16 days | `:240` |
| Start-up ramp | 345 customers appear in the first quarter of 2022 with nothing owed | `:180` |
| Hard-coded city penalty | Zugdidi customers are 83% late; a judge may question a location rule | `:112` |
| CSV mixes ledger and outcomes | `paid_weekday` and similar columns sit beside the features; prep ignores them, a quick notebook might not | CSV columns |
| No tracked hash | Data and metadata are git-ignored, so teammates cannot confirm they hold the same file | `.gitignore` |
| Naming | Generator says customers' payroll is on the 15th; the brief says salaries on the 10th and taxes on the 15th | `:38` |

## Why these happen

- **Why 500 customers is "too safe".** Each customer's lateness is drawn independently, so their errors cancel out when summed. Real gaps come from a few big customers or from many being late together.
- **Why the scores will flatter.** Features such as `due_in_payroll_window` mirror rules written into the generator, so the model is rediscovering what was planted. The team's doc already says this.
- **Why calibration is starved.** Partitions are cut by issue date and stop watching at the window's end, so a short window never sees its invoices grow old.

## What to raise first

Points 1 and 2 are the ones to settle, and the recommendation is to fix both in the generator: add a few large customers and customer-level bad spells. Both are small changes and retraining is cheap. Without them, the gap has to come entirely from the outflow side (salaries, taxes, stock purchases), and question 5 stays hollow.

Any generator change renumbers the whole dataset and every figure in the brief. So agree all changes in one batch, before the finance teammates size salaries and taxes against the cash-in profile.
