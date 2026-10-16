# BillBoard

Bill is a local cash assistant for a small business. It estimates when unpaid invoices may be collected, projects cash over 30 or 60 days, compares ways to cover a gap, and explains the results in chat. All financial calculations run in Python. Bill cannot access a bank account, move money or take a loan.

This is a competition demo using synthetic Georgian invoice histories and marked placeholder company/product terms. The original team brief is in [docs/Idea_md.md](docs/Idea_md.md); its older name was Rightsize.

## Start the demo

Run from the repository root. Keep the backend, frontend and Ollama running in separate terminals. You need Python, Node.js and Ollama. On a new computer, first create the backend environment with `python3 -m venv backend/.venv`.

```sh
source backend/.venv/bin/activate
python -m pip install -r backend/requirements.txt
ollama pull qwen2.5:7b
```

Open Ollama, or run `ollama serve` if it is not already running. An “address already in use” error means that port is occupied; do not start a second copy.

Start the backend in zsh. Choose a private demo PIN of at least eight characters. Input is hidden; the PIN is not written into your shell history.

```sh
read -s 'RIGHTSIZE_OWNER_PIN?Choose your demo PIN: '
export RIGHTSIZE_OWNER_PIN
python -m uvicorn backend.main:app --reload
```

In another terminal:

```sh
cd frontend
npm install
npm run dev
```

Open the frontend address, normally `http://localhost:5173`, and sign in. The backend normally listens on port 8000. Stop an older server in its own terminal with Ctrl+C if that port is occupied.

## Local data and model files

Generated data, the SQLite database and trained artifacts are ignored by Git. On a new computer prepare them before running the demo. Do not regenerate an established demo's source CSV or overwrite owner records just to restart the application.

```sh
python3 -m venv ml/.venv
source ml/.venv/bin/activate
python -m pip install -r ml/requirements.txt
python -m ml.generate_invoices_ge
python -m ml.prepare_data
python -m ml.baseline
```

Training and model selection are described in [ml/README.md](ml/README.md). The selected demo run is `ml/artifacts/lightgbm/leaves_16_min_100` (500 trees, 16 leaves, minimum 100 rows per leaf). Use those reviewed artifacts when available. `python -m ml.score_open_invoices` writes the initial 60-day curves and falls back explicitly to the empirical baseline if the selected model is missing. Model/source hashes must match.

With the backend environment active, seed the database once:

```sh
python -m backend.bank.seed
```

This imports 50,319 invoices and 500 customers from the synthetic CSV. Company outflows come from `backend/data/company.json`; funding terms come from `backend/data/products.json`. Both contain marked placeholders. Once owner cash is initialized, the seed refuses to overwrite the books. For experiments, seed a different file using `--db /tmp/your-demo.db` and start the server with `RIGHTSIZE_DB` pointing there.

## Using the dashboard

The six pages are Overview, Cash forecast, Invoices & customers, Transactions & loans, Funding options, and Activity.

1. Enter opening cash and its date in Overview or Transactions & loans. Review the proposed update in chat, then choose Yes. Before this setup, the figures use the clearly identified seeded demo cash.
2. Record actual receipts and payments as they happen. Use a unique reference. Link a customer receipt to its invoice ID, and a bill or loan payment to its obligation ID. The dashboard lists these IDs. Partial receipts reduce the remaining receivable. Exact duplicate references are idempotent; a different transaction cannot reuse a reference.
3. If an imported payment was already included in opening cash, mark it as such. It reconciles the invoice or bill without adding or subtracting cash again. Otherwise record only movements after opening cash, including later movements on that same date.
4. Record an existing loan only after receiving it elsewhere. Enter principal, annual nominal rate, upfront fees, dates, remaining monthly periods and repayment structure. New proceeds add principal minus upfront fees to recorded cash. A loan already included in opening cash adds no proceeds again. Its future repayments enter the forecast. Older loans require remaining principal and a future schedule; the form does not infer historical repayments.
5. Advance the reporting date explicitly. Cash remains opening cash plus recorded money in minus recorded money out. A scheduled bill is not automatically treated as paid. Unpaid past-due bills remain obligations and are assumed payable tomorrow in the forecast.
6. Correct mistakes with reversing entries. Original entries remain. Cancelling an incorrect loan record corrects these books; it does not cancel a real loan.

Every bookkeeping update is a Yellow action, including updates submitted through forms. It changes the books only after the owner approves the exact stored request in chat. A typed “yes” cannot approve it.

## Forecasts and comparisons

The trained LightGBM model estimates daily payment probability conditional on an invoice remaining unpaid. These hazards become payment-by-date curves. The dashboard shows 7-, 14-, 30- and 60-day probabilities for remaining invoices, and customer shares of outstanding debt.

At a later reporting date, the server scores the frozen predictor again locally; it does not silently reuse the old date's probabilities or retrain. It uses `ml/.venv/bin/python`, or `BILLBOARD_ML_PYTHON` when explicitly configured. Actual receipts remove or reduce invoices; the model's issue-date customer-history feature definition remains frozen.

Cash forecasts draw 2,000 possible invoice-payment futures with a fixed seed. They include recorded cash, remaining invoice receipts, scheduled unpaid bills, existing loan repayments, and a separate historical future-sales assumption. Likely is the daily median; cautious is the daily 10th percentile; the no-payment stress case assumes no invoice receipts. The cautious line is not a 90% guarantee for the entire path. Graphs connect weekly checkpoints and each scenario's minimum, rather than displaying every daily balance.

Customer concentration at or above 25% is flagged as an explicit demo policy, not a learned probability of default. Saved alerts are evaluated when the dashboard opens or refreshes. There is no background notification service.

Funding comparison includes waiting, reminders, early-payment discounts, overdraft and a term loan. Waiting/reminder costs cover known direct fees only; unknown late-payment consequences are not priced. Discounts require customer agreement and use whole selected invoices; selection is not a global optimization. Bank credit remains subject to approval. Nominal annual rates and the simple annualized discount equivalent are not effective APRs. Term-loan cost covers its configured full term, not an invented 30-day product.

Remaining modeling limits: synthetic data does not establish real-world accuracy; independent invoice draws do not model shared economic shocks; future-sales uncertainty is not simulated; applying the same timing curve to a partial invoice's remaining balance is an assumption. This demo imports a fixed invoice dataset; ongoing new-invoice import is not implemented. Predictions do not mark payments as received.

## Security

- A single demo owner signs in using a server environment PIN. There is no default PIN or public signup. This stands in for a bank's authentication; it is not MFA or production identity management.
- The browser gets a random eight-hour session cookie, HttpOnly and SameSite=Strict. Only the token's SHA-256 is stored in the database. PINs and raw tokens are not written to logs.
- Every business endpoint requires sign-in. Host checks restrict the local app, and state-changing requests reject nonlocal browser origins. Local HTTP does not use Secure cookies; deployable public authentication would require HTTPS and stronger controls. Other ports on the same computer remain local.
- Five wrong PIN attempts within five minutes block attempts for five minutes. The shared limit persists across server restarts. Unlocking a restricted chat requires PIN verification through a separate endpoint.
- The model can only suggest registered actions. Green reads run; Yellow writes wait for an owner button press; Red money-moving actions never run. Arguments are validated. Pending approval belongs to one chat, expires in ten minutes, freezes exact arguments and is consumed once.
- Three refused calls lock non-Green actions in that chat. The model cannot approve requests or unlock itself.
- Calls, approvals, refusals and authentication events enter an append-only, hash-linked audit log. Cash corrections append opposite entries. Database triggers prevent normal updates/deletes; this is not protection against someone controlling the whole machine and rewriting the database.
- Reminders go to a local outbox, credit requests remain drafts, and no bank connector or external sending service exists. Ollama runs locally. Untrusted text is rendered as data and cannot authorize an operation.

The underlying action design is explained in [docs/action-registry-security.md](docs/action-registry-security.md).

## Check the submission

A real API/bookkeeping/model smoke check uses freshly seeded temporary books, never the owner's database. It requires the generated CSV, matching saved baseline/model artifacts and the ML environment, but no Ollama or user PIN:

```sh
backend/.venv/bin/python -m backend.submission_check
```

Automated checks:

```sh
backend/.venv/bin/python -m unittest discover -s backend/tests
backend/.venv/bin/python -m unittest discover -s tests/scenarios -p 'test_*.py'
ml/.venv/bin/python -m unittest discover -s ml/tests
cd frontend
npm run lint
npm run build
npm test
```

For live agent scenarios, start a separate server with disposable books and your demo PIN, and use the same `RIGHTSIZE_DB` and `RIGHTSIZE_OWNER_PIN` for server and runner. Run the general cases before the bookkeeping cases, which intentionally change the books:

```sh
python -m tests.scenarios.run --server http://127.0.0.1:8001
python -m tests.scenarios.run --server http://127.0.0.1:8001 --cases tests/scenarios/b6_questions.json
python -m tests.scenarios.run --server http://127.0.0.1:8001 --cases tests/scenarios/bookkeeping_questions.json
```

The last file expects freshly seeded books with no opening-cash setup. Runs save transcripts and audit evidence under `tests/scenarios/results/`. These are specific acceptance cases, not a claim that every possible conversation is correct. See [tests/scenarios/README.md](tests/scenarios/README.md) for the format.
