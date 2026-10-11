# Rightsize

Rightsize is a copilot inside a bank's business app. It warns a small-business owner weeks before cash runs short, shows how big the gap really is, and lays out the cheapest ways to close it, including not borrowing.

It is our entry for the 2026 Shenzhen FinTechathon, International AI Track, Topic E (SME Finance Copilot). The deadline is 20 Oct 2026, 23:59 Beijing time.

The full picture is in the team brief: [docs/Idea_md.md](docs/Idea_md.md), also as a [PDF](docs/Idea.pdf).

## Where things are on 4 Oct 2026

`main` and `dev` hold only the documents. The code is on two branches that are not merged yet.

| Branch | What is there | Folders |
| --- | --- | --- |
| `feat/basic-chat` | Chat screen, server and the local AI model | `frontend/`, `backend/` |
| `feat/payment-forecast` | Generated invoice history and the payment forecast | `ml/`, `docs/data-generation-and-handling.md` |

The chat cannot see the company's data yet. Connecting the two is the next step.

## Run the chat

On `feat/basic-chat`, from the repository root. You need Python 3, Node.js and [Ollama](https://ollama.com).

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

On `feat/payment-forecast`, from the repository root:

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
