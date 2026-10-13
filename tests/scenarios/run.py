"""Score the agent against the cases in questions.json.

    python -m tests.scenarios.run

Each case is sent to the running server through /chat/stream as a new chat,
the way the screen does it: the whole conversation with every message, and a
press on Yes or No through /confirm when an approval card comes up. Then the
chat's rows are read from the audit log. What ran is judged from those rows,
never from what the agent says.

Expected figures are worked out when the script runs, by a query on the books
or by calling a green action, so the cases survive the data being regenerated.

The script and the server must use the same database: start both with the
same RIGHTSIZE_DB, or with none. The run prints a table and the pass rate and
saves everything it saw to a dated file in tests/scenarios/results.

Exit code: 0 when every case passed, 1 when some failed, 2 when the run could
not start. README.md next to this file describes the format of the cases.
"""

import argparse
import contextlib
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from backend.actions import registry
from backend.bank.db import db_path, today

from . import checks

HERE = Path(__file__).resolve().parent
DEFAULT_CASES = HERE / "questions.json"
DEFAULT_RESULTS = HERE / "results"
DEFAULT_SERVER = "http://127.0.0.1:8000"
# The model answers in 10 to 40 seconds, and the first answer after a start
# takes minutes while the model loads.
ANSWER_TIMEOUT = httpx.Timeout(600.0, connect=5.0)

CASE_ID = re.compile(r"[a-z0-9-]{1,60}")
OWNER_QUESTIONS = range(1, 7)
CASE_KEYS = {"id", "question", "trick", "title", "note", "values", "turns", "must_call",
             "must_not", "figures", "must_say_any", "must_not_say"}
# What the screen adds to its history after a press, word for word
# (handleConfirmation in frontend/src/App.tsx). The model reads it next turn.
APPROVED = "Approved action completed."
UNAVAILABLE = " If this request is unavailable, it may have been used or the server restarted."


class CaseError(Exception):
    """A case could not be carried out or checked. The message is its reason."""


# ---------------------------------------------------------------- the cases file
def problems_in(cases: Any) -> list[str]:
    """What is wrong with the cases file, in words for whoever edited it."""
    if not isinstance(cases, list) or not cases:
        return ["The file must hold a list of cases."]
    problems: list[str] = []
    seen: set[str] = set()
    for number, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            problems.append(f"Case {number} is not an object.")
            continue
        name = case.get("id")
        where = f"Case \"{name}\"" if isinstance(name, str) else f"Case {number}"
        if not isinstance(name, str) or not CASE_ID.fullmatch(name):
            problems.append(f"{where}: \"id\" must be 1 to 60 small letters, digits and hyphens.")
        elif name in seen:
            problems.append(f"{where}: this id is used twice.")
        else:
            seen.add(name)
        for key in sorted(set(case) - CASE_KEYS):
            problems.append(f"{where}: \"{key}\" is not a key the script knows.")
        if ("question" in case) == ("trick" in case):
            problems.append(f"{where}: give \"question\" (1 to 6) or \"trick\", one of the two.")
        elif "question" in case and case["question"] not in OWNER_QUESTIONS:
            problems.append(f"{where}: \"question\" must be a number from 1 to 6.")
        elif "trick" in case and not (isinstance(case["trick"], str) and case["trick"].strip()):
            problems.append(f"{where}: \"trick\" says in a few words what kind of trick it is.")

        turns = case.get("turns")
        if not isinstance(turns, list) or not turns:
            problems.append(f"{where}: \"turns\" must list at least one message.")
            turns = []
        for turn in turns:
            if not isinstance(turn, dict) or not isinstance(turn.get("say"), str) or not turn["say"].strip():
                problems.append(f"{where}: every turn needs \"say\", the owner's message.")
            elif not isinstance(turn.get("confirm", {}), dict) \
                    or not all(answer in ("yes", "no") for answer in turn.get("confirm", {}).values()):
                problems.append(f"{where}: \"confirm\" maps an action to \"yes\" or \"no\".")

        values = case.get("values", {})
        if not isinstance(values, dict):
            problems.append(f"{where}: \"values\" must be an object.")
            values = {}
        for value, source in values.items():
            if not checks.PLACEHOLDER.fullmatch(f"{{{value}}}"):
                problems.append(f"{where}: the value name \"{value}\" must be small letters, digits and _.")
            if not isinstance(source, dict) or ("sql" in source) == ("action" in source):
                problems.append(f"{where}: value \"{value}\" needs \"sql\" or \"action\", one of the two.")
            elif "action" in source and not isinstance(source.get("path"), str):
                problems.append(f"{where}: value \"{value}\" needs a \"path\" into the action's result.")
        for key in ("must_call", "must_not"):
            for entry in case.get(key, []):
                if not isinstance(entry, dict) or "action" not in entry:
                    problems.append(f"{where}: every \"{key}\" entry names an \"action\".")
        for entry in case.get("figures", []):
            for figure in entry if isinstance(entry, list) else [entry]:
                if figure not in values:
                    problems.append(f"{where}: figure \"{figure}\" is not one of its \"values\".")
    return problems


# ------------------------------------------------- values worked out at run time
def read_only(path: Path) -> sqlite3.Connection:
    """The books, opened so that no query in a case can change them."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class AnswerKey:
    """The answer key's own calls to green actions. They go through the
    registry like any other call, in a session of their own, so the log shows
    how each expected figure was worked out. A call is made once per run:
    four figures out of one forecast cost one call and leave one log row."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._replies: dict[tuple[str, str], dict] = {}

    def result(self, action: str, arguments: dict) -> dict:
        if not registry.list_actions():
            import backend.main  # noqa: F401  Registers the real actions, as the server does.
        levels = {listed["name"]: listed["level"] for listed in registry.list_actions()}
        if action not in levels:
            raise CaseError(f"the answer key needs the action {action}, which this version does not have")
        if levels[action] != "green":
            raise CaseError(f"the answer key may only call green actions, and {action} is {levels[action]}")
        asked = (action, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
        if asked not in self._replies:
            self._replies[asked] = registry.execute(action, arguments, self.session_id)
        reply = self._replies[asked]
        if reply["status"] != "ok":
            raise CaseError(f"the answer key's call to {action} did not run: "
                            f"{reply.get('message') or reply.get('reason')}")
        return reply["result"]


def resolve_values(case: dict, conn: sqlite3.Connection, as_of: str, key: AnswerKey) -> dict[str, Any]:
    """Work out a case's values, in the order written. A query may use :today
    and any value before it, as :name. An action's arguments may use {name}."""
    values: dict[str, Any] = {}
    for name, source in case.get("values", {}).items():
        if "sql" in source:
            try:
                row = conn.execute(source["sql"], {"today": as_of, **values}).fetchone()
            except sqlite3.Error as problem:
                raise CaseError(f"the query for \"{name}\" failed: {problem}") from None
            if row is None or row[0] is None:
                raise CaseError(f"the query for \"{name}\" found nothing in these books")
            values[name] = row[0]
            continue
        try:
            arguments = checks.substitute(source.get("arguments", {}), values)
        except KeyError as missing:
            raise CaseError(f"value \"{name}\" uses {missing}, which is not defined before it") from None
        result = key.result(source["action"], arguments)
        try:
            values[name] = checks.follow(result, source["path"])
        except LookupError as step:
            raise CaseError(f"the result of {source['action']} has no \"{step.args[0]}\" "
                            f"(path \"{source['path']}\")") from None
    return values


# ----------------------------------------------------------------- one conversation
def run_case(client: httpx.Client, case: dict, values: dict[str, Any], session_id: str) -> dict:
    """Hold the case's conversation with the server. Returns what was said and
    seen, and the error that ended it early, if one did."""
    messages: list[dict[str, str]] = []
    turns: list[dict] = []
    for turn in case["turns"]:
        try:
            say = checks.substitute(turn["say"], values)
        except KeyError as missing:
            return {"turns": turns, "error": f"a message uses {missing}, which is not one of the case's values"}
        messages.append({"role": "user", "content": say})
        record: dict[str, Any] = {"say": say, "events": [], "reply": "", "confirmations": []}
        turns.append(record)

        try:
            with client.stream("POST", "/chat/stream",
                               json={"messages": messages, "session_id": session_id}) as response:
                if response.status_code != 200:
                    response.read()
                    return {"turns": turns,
                            "error": f"the server answered {response.status_code}: {response.text[:200]}"}
                for line in response.iter_lines():
                    if line.strip():
                        record["events"].append(json.loads(line))
        except (httpx.HTTPError, ValueError) as problem:
            return {"turns": turns, "error": f"the conversation broke off: {type(problem).__name__}: {problem}"}

        record["reply"] = "".join(str(event.get("content", "")) for event in record["events"]
                                  if event.get("type") == "chunk")
        failed = next((event for event in record["events"] if event.get("type") == "error"), None)
        if failed:
            return {"turns": turns, "error": f"the server reported: {failed.get('message')}"}
        if not record["reply"].strip():
            return {"turns": turns, "error": "the agent gave no answer"}

        in_history = record["reply"]
        for card in _cards(record["events"]):
            # A card the case does not expect is answered No, as an owner who
            # did not ask for it would.
            answer = turn.get("confirm", {}).get(card["action"], "no")
            try:
                result = client.post("/confirm", json={
                    "confirmation_id": card["confirmation_id"], "approved": answer == "yes",
                    "session_id": session_id}).json()
            except (httpx.HTTPError, ValueError) as problem:
                return {"turns": turns, "error": f"the press on {answer} was lost: {type(problem).__name__}"}
            record["confirmations"].append({**card, "answered": answer, "result": result})
            in_history += f"\nApproval result: {_outcome(result)}"
        messages.append({"role": "assistant", "content": in_history})
    return {"turns": turns, "error": None}


def _cards(events: list[dict]) -> list[dict]:
    """The approval cards of one turn, each with the action it is for. The
    stream names the action in the step just before the card."""
    cards, last_action = [], None
    for event in events:
        if event.get("type") == "action":
            last_action = event.get("name")
        elif event.get("type") == "confirmation":
            cards.append({"action": last_action, "confirmation_id": event.get("confirmation_id"),
                          "summary": event.get("summary")})
    return cards


def _outcome(result: Any) -> str:
    if isinstance(result, dict):
        if result.get("status") == "ok":
            return APPROVED
        if result.get("status") == "refused":
            return str(result.get("reason"))
        if result.get("status") == "error":
            return f"{result.get('message')}{UNAVAILABLE}"
    return "Approval response was lost."


def warm_up(client: httpx.Client, stamp: str) -> dict:
    """One throwaway message before the cases. A model that has just been
    started takes minutes to load, and the first case should not pay for it."""
    started = time.monotonic()
    talk = run_case(client, {"turns": [{"say": "Hello"}]}, {}, f"warmup-{stamp}")
    return {"seconds": round(time.monotonic() - started, 1), "error": talk["error"]}


# ---------------------------------------------------------------------- the run
def score(client: httpx.Client, cases: list[dict], stamp: str, instructions: str, key: AnswerKey,
          report: Callable[[dict], None] = lambda record: None) -> list[dict]:
    """Run every case and check it. Returns one record per case, with
    everything that was said, seen and logged."""
    as_of = today().isoformat()
    records = []
    with contextlib.closing(read_only(db_path())) as books:
        for case in cases:
            started = time.monotonic()
            session_id = f"test-{stamp}-{case['id']}"
            values: dict[str, Any] = {}
            try:
                values = resolve_values(case, books, as_of, key)
                talk = run_case(client, case, values, session_id)
            except CaseError as problem:
                talk = {"turns": [], "error": str(problem)}
            rows = registry.audit_log(session_id)

            error = talk["error"]
            stepped = any(event.get("type") == "action"
                          for turn in talk["turns"] for event in turn["events"])
            if not error and stepped and not rows:
                error = ("the log has no rows for this chat. The script and the server "
                         "must use the same database (RIGHTSIZE_DB)")
            answers = [turn["reply"] for turn in talk["turns"]]
            reasons = checks.check_case(case, values, answers, rows, instructions, error)
            records.append({
                "id": case["id"], "question": case.get("question"), "trick": case.get("trick"),
                "title": case.get("title", ""), "passed": not reasons, "reasons": reasons,
                "session_id": session_id, "seconds": round(time.monotonic() - started, 1),
                "values": values, "turns": talk["turns"], "log_rows": rows,
            })
            report(records[-1])
    return records


def summary(records: list[dict]) -> dict:
    passed = sum(record["passed"] for record in records)
    questions = {}
    for number in OWNER_QUESTIONS:
        its = [record for record in records if record["question"] == number]
        if its:
            questions[str(number)] = {"passed": sum(record["passed"] for record in its), "cases": len(its)}
    tricks = [record for record in records if record["trick"]]
    return {
        "cases": len(records), "passed": passed,
        "pass_rate_percent": round(100 * passed / len(records), 1) if records else 0.0,
        "owner_questions": questions,
        "tricks": {"passed": sum(record["passed"] for record in tricks), "cases": len(tricks)},
        # A case that broke down is a failed case, but not a wrong answer and
        # not a trick that worked, so it is counted on its own as well.
        "ended_in_an_error": sum(any(reason.startswith("error:") for reason in record["reasons"])
                                 for record in records),
    }


def table(records: list[dict]) -> str:
    """Case, pass or fail, reason: one line per case and one more per further reason."""
    width = max([len(record["id"]) for record in records] + [4])
    lines = [f"{'Case':<{width}}  {'For':<5}  {'Result':<6}  Reason",
             f"{'-' * width}  {'-' * 5}  {'-' * 6}  {'-' * 6}"]
    for record in records:
        about = f"Q{record['question']}" if record["question"] else "trick"
        reasons = record["reasons"] or [""]
        lines.append(f"{record['id']:<{width}}  {about:<5}  "
                     f"{'pass' if record['passed'] else 'FAIL':<6}  {reasons[0]}".rstrip())
        lines += [f"{'':<{width}}  {'':<5}  {'':<6}  {reason}" for reason in reasons[1:]]
    return "\n".join(lines)


def closing_lines(totals: dict) -> str:
    lines = [f"Passed {totals['passed']} of {totals['cases']} cases ({totals['pass_rate_percent']:g}%)."]
    for number, count in totals["owner_questions"].items():
        verdict = "passes" if count["passed"] == count["cases"] else "fails"
        lines.append(f"Owner question {number} {verdict} today: "
                     f"{count['passed']} of {count['cases']} cases.")
    missing = [str(number) for number in OWNER_QUESTIONS if str(number) not in totals["owner_questions"]]
    if missing:
        lines.append(f"No case was run for owner question {', '.join(missing)}.")
    if totals["tricks"]["cases"]:
        lines.append(f"Trick attempts: {totals['tricks']['passed']} of {totals['tricks']['cases']} cases passed.")
    if totals["ended_in_an_error"]:
        lines.append(f"Of the failed cases, {totals['ended_in_an_error']} ended in an error instead of an "
                     "answer. Their reasons say which.")
    return "\n".join(lines)


def open_client(server: str) -> httpx.Client:
    return httpx.Client(base_url=server.rstrip("/"), timeout=ANSWER_TIMEOUT)


def _commit() -> str | None:
    try:
        done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True,
                              text=True, timeout=5, check=True)
        return done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.scenarios.run",
        description="Score the agent against the cases in questions.json.")
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help=f"where the server runs (default {DEFAULT_SERVER})")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="the cases file")
    parser.add_argument("--only", default="", help="run only these case ids, separated by commas")
    parser.add_argument("--out", type=Path, default=DEFAULT_RESULTS,
                        help="the folder for the dated result file")
    options = parser.parse_args(arguments)

    try:
        cases_text = options.cases.read_text(encoding="utf-8")
        cases = json.loads(cases_text)
    except (OSError, ValueError) as problem:
        print(f"Cannot read {options.cases}: {problem}", file=sys.stderr)
        return 2
    problems = problems_in(cases)
    wanted = [name.strip() for name in options.only.split(",") if name.strip()]
    if not problems:
        known = {case["id"] for case in cases}
        problems = [f"--only names \"{name}\", which is not a case id." for name in wanted if name not in known]
    if problems:
        print(f"{options.cases} cannot be used:", *[f"  {problem}" for problem in problems],
              sep="\n", file=sys.stderr)
        return 2
    if wanted:
        cases = [case for case in cases if case["id"] in wanted]

    if not db_path().exists():
        print(f"{db_path()} does not exist. Build it with: python -m backend.bank.seed", file=sys.stderr)
        return 2
    # The agent's instructions, which no answer may repeat.
    from backend.llm import OLLAMA_MODEL, SYSTEM_PROMPT

    now = datetime.now().astimezone()
    stamp = now.strftime("%Y%m%d%H%M%S")
    key = AnswerKey(f"answerkey-{stamp}")
    with open_client(options.server) as client:
        try:
            client.get("/health").raise_for_status()
        except httpx.HTTPError:
            print(f"No server answers at {options.server}. Start it with: "
                  "python -m uvicorn backend.main:app", file=sys.stderr)
            return 2

        print(f"Running {len(cases)} cases against {options.server}. Each takes up to a few minutes.")
        warmed = warm_up(client, stamp)
        if warmed["error"]:
            print(f"The warm-up message got no answer ({warmed['error']}). Carrying on.")
        print()

        def report(record: dict) -> None:
            print(f"  {'pass' if record['passed'] else 'FAIL'}  {record['id']}  ({record['seconds']:g} s)",
                  flush=True)

        records = score(client, cases, stamp, SYSTEM_PROMPT, key, report)

    totals = summary(records)
    with contextlib.closing(read_only(db_path())) as books:
        meta = {row["key"]: row["value"] for row in books.execute("SELECT key, value FROM meta")}
    evidence = {
        "started": now.isoformat(timespec="seconds"),
        "server": options.server,
        "model_set_where_the_script_ran": OLLAMA_MODEL,
        "code_commit": _commit(),
        "cases_file": str(options.cases),
        "cases_sha256": hashlib.sha256(cases_text.encode("utf-8")).hexdigest(),
        "books": {"database": str(db_path()), **meta},
        "warm_up": warmed,
        "summary": totals,
        "log_check": registry.verify_log(),
        "answer_key": {"session_id": key.session_id, "log_rows": registry.audit_log(key.session_id)},
        "cases": records,
    }
    options.out.mkdir(parents=True, exist_ok=True)
    saved = options.out / f"{now.strftime('%Y-%m-%d_%H%M%S')}.json"
    saved.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"\n{table(records)}\n\n{closing_lines(totals)}\nSaved: {saved}")
    return 0 if totals["passed"] == totals["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
