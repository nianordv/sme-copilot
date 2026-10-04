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
