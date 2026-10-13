# Rightsize

Rightsize is a copilot inside a bank's business app. It warns a small-business owner weeks before cash runs short, shows how big the gap really is, and lays out the cheapest ways to close it, including not borrowing.

It is our entry for the 2026 Shenzhen FinTechathon, International AI Track, Topic E (SME Finance Copilot). The deadline is 20 Oct 2026, 23:59 Beijing time.

The full picture is in the team brief: [docs/Idea_md.md](docs/Idea_md.md), also as a [PDF](docs/Idea.pdf).

## Where things are on 13 Oct 2026

`main` holds only the documents. `dev` holds the chat screen, the server and the payment forecast. The books and the actions are on branches that are not merged into `dev` yet. Each one in this table contains the ones above it:

| Branch | What it adds |
| --- | --- |
| `feat/company-books` | The company's books as a database |
| `feat/lookups` | The look-up actions |
| `feat/permission-checks` | The permission levels, the log and the lock |
| `feat/more-actions` | Reminders, alerts, plans, option costs, the credit-request draft and the owner-only actions |
| `feat/test-script` | The script that scores the agent, and its first 13 cases |

`feat/cash-forecast` adds the cash forecast action. It is built on `feat/permission-checks`, beside `feat/more-actions`, so the two still have to be merged with each other. Until they are, the cases that need the forecast fail. Owner questions 1, 4, 5 and 6 each have one.

## Run the chat

From the repository root. You need Python 3, Node.js and [Ollama](https://ollama.com). Bill can do nothing without the company's books, so [build them](#build-the-companys-books) before you ask the first question.

1. Download the model once. It is about 5 GB.

   ```sh
   ollama pull qwen2.5:7b
   ```

2. Start the server. It listens on port 8000.

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r backend/requirements.txt
   python -m uvicorn backend.main:app --reload
   ```

3. In a second terminal, start the screen and open the address it prints.

   ```sh
   cd frontend
   npm install
   npm run dev
   ```

`http://127.0.0.1:8000/health` answers `{"status":"ok"}` when the server is up. If Ollama is not running, the screen shows "Lost connection to Ollama."

## Generate the data and prepare the forecast

From the repository root:

```sh
python3 -m venv ml/.venv
source ml/.venv/bin/activate
python -m pip install -r ml/requirements.txt
python -m ml.generate_invoices_ge
python -m unittest discover -s ml/tests -v
python -m ml.prepare_data
```

This writes 50,319 generated invoices to `ml/data/` and the prepared training files to `ml/artifacts/`. Git ignores both folders, so each of us generates them locally. Training with `python -m ml.train` also needs `lightgbm`, which is not in `ml/requirements.txt` yet.

## Build the company's books

The server reads the demo company's books from a SQLite database, `backend/data/rightsize.db`. Build it after generating the data, with the server's environment (`.venv`) active:

```sh
python -m backend.bank.seed
```

It copies the ledger columns of the invoice CSV into the database, gives each of the 500 customers a readable name, and adds the company's own money: the bank balance and the payments due in the next 60 days. Those amounts come from one file, `backend/data/company.json`. They are placeholders until the finance owners overwrite that file.

Run the command again whenever the CSV or `company.json` changes. It rebuilds the books and leaves everything else in the database alone. Git ignores the database file.

"Today" in the app is the last day of the books, 3 Oct 2026, not the day you run it. `backend.bank.db.today()` returns it.

## What Bill can do

| Action | Level | What it does |
| --- | --- | --- |
| `get_receivables`, `get_customer`, `get_balance`, `get_upcoming_outflows` | Green | Look things up in the books. |
| `draft_reminder` | Green | Writes a payment reminder for one customer from a fixed template. Sends nothing. |
| `compare_options` | Green | Lists the ways to cover a shortfall of a given size for a given number of days (remind late customers, wait, overdraft, loan), each with its total cost in GEL and its yearly rate, cheapest first. |
| `send_reminder` | Yellow | Saves the reminder the owner approved to the `outbox` table. |
| `set_alert` | Yellow | Stores an alert for a date, or for the bank balance falling below an amount, in the `alerts` table. |
| `save_plan` | Yellow | Stores a plan the owner agreed to in the `plans` table. |
| `prepare_credit_request` | Yellow | Saves a filled-in request for an overdraft or a loan to the `credit_request_drafts` table. It stays a draft. |
| `transfer_money`, `take_loan`, `repay_loan`, `submit_credit_request` | Red | Always refused and logged. |

Nothing leaves the database. A "sent" reminder is a row in `outbox`, nothing watches the alerts or sends a notification, and no credit request reaches a bank. Rebuilding the books leaves these tables alone.

### Product terms and the cost of each option

The overdraft's and the loan's terms come from one file, `backend/data/products.json`. They are placeholders (overdraft 16% a year, loan 15% a year plus a 1% fee) until the finance owners overwrite that file. An edit shows up at the next question, with no restart and no rebuild.

The arithmetic is in `backend/bank/products.py`, and the AI model takes no part in it:

- Overdraft: the amount times the yearly rate, for the days used out of 365. GEL 8,000 for 21 days at 16% costs GEL 73.64.
- Loan: equal monthly payments over the whole term, plus the fee. GEL 30,000 over 12 months at 15% costs GEL 2,493.00 in interest plus a fee of GEL 300.00.

`compare_options` marks the cheapest option that covers the shortfall as recommended. When that option depends on customers paying, it also names a fallback: the cheapest option that is sure to bring the money. Waiting is never recommended.

The rate and fee on a draft request are the ones the owner saw on the approval card, even if the file changes before the owner presses Yes.

## Permissions and the log

Everything the AI model asks for goes through `backend/actions/registry.py`. It decides by the action's level, never by what is written in the chat.

| Level | What happens |
| --- | --- |
| Green | Runs straight away. |
| Yellow | Is stored and shown to the owner. It runs only when the owner presses Yes: once, within 10 minutes, in the same chat. |
| Red | Never runs. |

Every call and every Yes or No adds a row to the `audit_log` table in the same database. No action runs without the database, so build it first. Rows can only be added. After 3 refused calls in one chat, that chat can only look things up until `registry.unlock(session_id)` is called.

To check that no row was changed or removed:

```sh
python -c "from backend.actions.registry import verify_log; print(verify_log())"
```

`registry.audit_log(session_id)` returns the rows themselves. Nothing checks who is calling the server, so "one chat" is not "one person".

A Yellow action can have a `prepare` step. It runs before the owner is asked and pins the request down: a customer's name becomes the one ID it points to, a date becomes a real date, a standard text is written in. What it returns is what is stored, shown to the owner, logged and run. It raises `registry.ActionError` to turn down a request that cannot go ahead, such as a reminder for a customer with nothing overdue, so the owner is never shown a card for it. A `prepare` step must only read.

Text from the AI model is treated as data, never as layout. On the approval card a name appears on one line in quotation marks, and a message has `>` in front of every line, so neither can pass for the app's own words. A character that is invisible or reorders text is written out as a visible code, on the card and in the log. When you add a Yellow action, build its sentence with `registry.one_line()` for short values and `registry.quoted()` for longer text. Text the app takes from its own database needs neither.

On the card, look-alike quotation marks in a name and more than three blanks in a row are written out as codes too. All of this is a best effort: no list of misleading characters is complete, and the card is still plain text. The sound fix is on the screen: draw the model's text in its own box, so that nothing inside it can pass for the app's words.

## Score the agent

With the server running, one command asks the agent every question in `tests/scenarios/questions.json`, checks what it did and what it answered, and prints which owner questions pass today:

```sh
python -m tests.scenarios.run
```

It reads what ran from the log, works the expected figures out from the books, and saves everything it saw to a dated file. Every run adds rows to the database, so use a copy for practice runs. [tests/scenarios/README.md](tests/scenarios/README.md) has the commands and the format of a case.

To run the tests of the code, with no server and no AI model:

```sh
python -m unittest discover -s backend/tests -t .
python -m unittest discover -s tests -t .
```
