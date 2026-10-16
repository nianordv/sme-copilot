"""The single execution boundary between model requests and actions.

Action: name, description, parameters (JSON Schema), level, handler(**arguments),
and optionally summarize(**arguments), the sentence the owner is asked to approve,
and prepare(**arguments), which pins a yellow request down before the owner is
asked: a customer's name becomes an ID, a standard text is written in. What
prepare returns is what is stored, shown, logged and run. It raises ActionError
to stop a request that cannot go ahead.
register(action) adds an action; list_actions() returns serialisable descriptions.
execute(name, arguments, session_id) and
confirm(confirmation_id, approved, session_id) return one of:
  {"status": "ok", "result": dict}
  {"status": "needs_confirmation", "confirmation_id": str, "summary": str}
  {"status": "refused", "reason": str}
  {"status": "error", "message": str}
unlock(session_id) clears a lock; audit_log() and verify_log() only read.
owner_records() lets the sign-in code add its own rows to the log.

Levels: Green runs. Yellow is stored in the database with its exact arguments
and runs only when confirm() approves it: an ID works once, in one session, for
10 minutes, and what runs is what was stored. Red never runs. Arguments are
checked against the action's schema first; "30" sent for a number becomes 30.
waiting_requests() lists what is stored and still waiting, for the dashboard.

Only trusted server code may call confirm() and unlock(). No action exposes
them and both refuse to run from inside an action, so text produced by the
model cannot approve anything.

Text from the model is data, never layout. A summarize function puts a short
value through one_line() (in quotation marks, on one line) and a longer one
through quoted() ("> " before every line), so neither can pass for words of
ours. In every sentence the owner is shown and in every text column of a log
row, a character that cannot be seen or that moves other text is written as
its escape (\\n, \\u202e); logged arguments still read back as the same
characters. On a card, look-alike quotation marks in a name and long runs of
blanks are written out as well. This is a best effort: the list of characters
that mislead cannot be complete, and only the screen can box the model's text.
A reply repeats an argument name the model made up only when it
looks like a name.

Log: every call and every confirmation adds one row to the audit_log table:
time, session, action, arguments, level, decision and a short result. Decisions
are ran, waiting, approved, declined, refused, expired, error and unlocked, and
signed_in and signed_out on the rows the sign-in code adds.
Rows are only added: nothing here edits or deletes one, and the database
rejects an UPDATE, a DELETE or a replacing INSERT. Each row carries the SHA-256
of its content and of the row before it, so verify_log() finds a row that was
changed, removed or slipped in, even after the database's own protection is
removed. Whoever can rewrite the whole file can rebuild that chain or cut rows
off its end, so the last fingerprint belongs in the evidence we submit.

No log, no action: a decision and its row are saved in one transaction. A
yellow action runs only after its "approved" row is saved, and a result is
returned only after its "ran" row is saved. Every logged field has a size
limit, and arguments too long to log in full are turned down before the owner
is asked, so what the owner approves is in the log word for word.

Lock: after 3 refused calls in one session, every yellow and red call in that
session is refused until unlock(session_id), which stands in for the owner
re-verifying in the bank app. The lock is worked out from the log.

Who is calling is not decided here. backend/auth.py signs the one demo owner
in, and the endpoints let nobody else reach execute(), confirm() or unlock().
A session ID is still a scope marker, not proof of identity: the lock and the
one-session rule bind a chat, and the signed-in owner may use every chat.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import functools
import hashlib
import inspect
import json
import logging
from pathlib import Path
import re
import sqlite3
from threading import RLock
import traceback
from typing import Any, Callable, Literal
import unicodedata
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from backend.bank.db import connect

logger = logging.getLogger(__name__)

CONFIRM_MINUTES = 10
LOCK_AFTER_REFUSALS = 3
# Waiting requests are kept few: a session's sixth replaces its oldest, and
# past the total no more are stored until some are answered or expire.
WAITING_PER_SESSION = 5
WAITING_IN_TOTAL = 1000

# Size limits, in characters. Log rows are never removed, so nothing unlimited goes in.
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")  # The same rule as the endpoints.
NAME_LIMIT = 100         # action names and confirmation IDs, as logged
TEXT_LIMIT = 2000        # one text argument, unless its schema sets maxLength
ARGUMENTS_LIMIT = 4000   # the arguments of one call, as JSON
RESULT_LIMIT = 300       # the short result, as logged

# A call with no usable session ID is logged under this. The brackets keep it
# from ever being a valid ID itself.
INVALID_SESSION = "(invalid)"
# What the owner does outside any chat (signing in, a wrong PIN, signing out)
# is logged under this, which the brackets keep from being a chat's ID as well.
OWNER_SESSION = "(owner)"
LOGGED_COLUMNS = ("time", "session_id", "action", "arguments", "level", "decision",
                  "result", "confirmation_id")

# Unicode categories of what cannot be seen or moves other text: controls
# (line breaks among them), direction overrides, zero-width marks, line
# separators, and code points that have no character.
UNSEEN = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# Characters that draw nothing although Unicode files them as letters, marks or
# symbols: a joiner, Khmer and Mongolian marks, Hangul fillers, the blank
# braille cell. A list like this is a best effort and cannot be complete.
BLANK = frozenset(map(chr, (
    0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D, 0x180F,
    0x2800, 0x3164, 0xFFA0)))
# Marks that could pass for the quotation mark that closes a name.
QUOTE_LIKE = frozenset(map(chr, (
    0x02BA, 0x02DD, 0x02EE, 0x201C, 0x201D, 0x201E, 0x201F, 0x2033, 0x2036,
    0x275D, 0x275E, 0x2E42, 0x3003, 0x301D, 0x301E, 0x301F, 0xFF02)))
# Shown to the owner, blanks beyond this many in a row are written out: a long
# run pushes the words after it onto a line of their own.
BLANKS_IN_A_ROW = 3
# An argument name the model made up is repeated in a reply only if it looks like one.
NAME_LIKE = re.compile(r"[A-Za-z0-9_]{1,40}")

OWNER_ONLY = "The assistant is not allowed to do this. Only the owner can, in the bank app."
LOCKED = (f"This session is locked after {LOCK_AFTER_REFUSALS} refused requests. "
          "Only look-ups work until the owner re-verifies in the bank app.")
EXPIRED = (f"This request expired after {CONFIRM_MINUTES} minutes. Nothing was done. "
           "Ask again if it is still wanted.")
NOT_WAITING = "Confirmation unavailable for this session."
TOO_MANY = "Too many requests are waiting for approval. Answer them or try again in a few minutes."
FAILED = "Action execution failed."
UNAVAILABLE = "The action log is unavailable, so the request was not carried out."
RAN_UNRECORDED = "The action ran, but its result could not be recorded. Check before asking again."
CANNOT_BE_SHOWN = "This request cannot be shown here. Answer it in the chat."

TABLES = """
CREATE TABLE IF NOT EXISTS audit_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL,
    arguments TEXT NOT NULL,
    level TEXT NOT NULL,
    decision TEXT NOT NULL,
    result TEXT NOT NULL,
    confirmation_id TEXT,
    row_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_by_session ON audit_log (session_id, log_id);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
-- INSERT OR REPLACE removes the old row without firing the DELETE trigger.
CREATE TRIGGER IF NOT EXISTS audit_log_no_replace BEFORE INSERT ON audit_log
WHEN EXISTS (SELECT 1 FROM audit_log WHERE log_id = NEW.log_id)
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
-- One row per request still waiting for the owner. Answered and expired
-- requests are removed; the log keeps what became of them.
CREATE TABLE IF NOT EXISTS confirmations (
    confirmation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL,
    arguments TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


class ActionError(Exception):
    """Raise from a prepare step or handler when a request cannot go ahead for a reason
    the model should pass on, such as a customer who is not found. The message
    is returned as an "error" and shown on the screen as a step, so write it in
    our own words and do not repeat text the model sent."""


@dataclass(frozen=True)
class Action:
    name: str
    description: str
    parameters: dict
    level: Literal["green", "yellow", "red"]
    handler: Callable[..., dict]
    # Yellow only, optional: turns the arguments into the sentence the owner
    # approves. It must show everything that will happen, because the owner
    # reads this sentence and not the arguments. The model wrote the values,
    # so each goes in through one_line() or quoted().
    summarize: Callable[..., str] | None = None
    # Yellow only, optional: takes the checked arguments and returns the final
    # ones. May raise ActionError to stop the request before the owner is asked.
    # It must only read: it runs before the owner has agreed to anything.
    prepare: Callable[..., dict] | None = None


_actions: dict[str, Action] = {}
_lock = RLock()
# The name of the action whose handler is running right now, if any.
_inside_action: ContextVar[str | None] = ContextVar("inside_action", default=None)


def register(action: Action) -> None:
    if not isinstance(action.name, str) or not action.name.strip():
        raise ValueError("An action needs a nonempty name.")
    if len(action.name) > NAME_LIMIT:
        raise ValueError(f"An action name has at most {NAME_LIMIT} characters.")
    if action.level not in {"green", "yellow", "red"}:
        raise ValueError("Unknown permission level.")
    if not callable(action.handler):
        raise ValueError("The handler must be callable.")
    if not isinstance(action.parameters, dict):
        raise ValueError("Parameters must be a JSON Schema dictionary.")
    if action.prepare is not None and action.level != "yellow":
        raise ValueError("Only a yellow action has a prepare step.")
    _arguments_model(action)  # Fails here, at start-up, if the schema cannot be checked.
    with _lock:
        if action.name in _actions:
            raise ValueError(f"Action already registered: {action.name}")
        # Detach the stored schema from the caller's mutable dictionary.
        _actions[action.name] = replace(action, parameters=deepcopy(action.parameters))


def list_actions() -> list[dict]:
    with _lock:
        return [{"name": a.name, "description": a.description,
                 "parameters": deepcopy(a.parameters), "level": a.level}
                for a in _actions.values()]


def execute(name, arguments, session_id) -> dict:
    try:
        with _records() as conn:
            if not _valid_session(session_id):
                return _bad_session(conn, name, session_id)
            return _execute(conn, name, arguments, session_id)
    except Exception as problem:
        _report("execute", problem)
        return _error(UNAVAILABLE)


def confirm(confirmation_id, approved, session_id) -> dict:
    try:
        with _records() as conn:
            if not _valid_session(session_id):
                return _bad_session(conn, "confirm", session_id)
            return _confirm(conn, confirmation_id, approved, session_id)
    except Exception as problem:
        _report("confirm", problem)
        return _error(UNAVAILABLE)


def cancel_chat_requests(session_id) -> dict:
    """Cancel pending approvals when the signed-in owner deletes a chat."""
    try:
        with _records() as conn:
            if not _valid_session(session_id):
                return _bad_session(conn, "delete_chat", session_id)
            if _inside_action.get() is not None:
                return _refused("An action cannot delete a chat.")
            with _transaction(conn):
                rows = conn.execute("SELECT * FROM confirmations WHERE session_id = ?",
                                    (session_id,)).fetchall()
                for row in rows:
                    conn.execute("DELETE FROM confirmations WHERE confirmation_id = ?",
                                 (row["confirmation_id"],))
                    _log(conn, session_id, row["action"], json.loads(row["arguments"]),
                         "yellow", "declined", "Chat deleted; pending approval cancelled.",
                         row["confirmation_id"])
                _log(conn, session_id, "delete_chat", {}, "owner", "ran",
                     "Chat history deletion requested; financial records preserved.")
            return {"status": "ok", "result": {"cancelled": len(rows)}}
    except Exception as problem:
        _report("delete chat", problem)
        return _error(UNAVAILABLE)


def unlock(session_id) -> dict:
    """Clear a session's lock. This stands in for the owner re-verifying in the
    bank app, so only trusted server code may call it."""
    try:
        with _records() as conn:
            if not _valid_session(session_id):
                return _bad_session(conn, "unlock", session_id)
            with _transaction(conn):
                caller = _inside_action.get()
                if caller is not None:
                    _log(conn, session_id, "unlock", {}, "owner", "refused",
                         f"called from inside {caller}")
                    return _refused("An action cannot unlock a session.")
                was_locked = _is_locked(conn, session_id)
                _log(conn, session_id, "unlock", {}, "owner", "unlocked",
                     "was locked" if was_locked else "was not locked")
            return {"status": "ok", "result": {"was_locked": was_locked}}
    except Exception as problem:
        _report("unlock", problem)
        return _error(UNAVAILABLE)


@contextmanager
def owner_records() -> Iterator[tuple[sqlite3.Connection, Callable[..., None]]]:
    """For the sign-in code (backend/auth.py) and nothing else: one transaction
    over the records, and a function that adds a row for something the owner
    did outside any chat, such as signing in or typing a wrong PIN. The row is
    written the way unlock() writes its own, with level "owner", under
    OWNER_SESSION. What the caller changes and the row that records it are
    saved together or not at all."""
    if _inside_action.get() is not None:
        raise RuntimeError("An action cannot write the owner's rows.")
    with _records() as conn, _transaction(conn):
        def log(action, decision, result, arguments=None) -> None:
            _log(conn, OWNER_SESSION, action, arguments or {}, "owner", decision, result)

        yield conn, log


def audit_log(session_id=None) -> list[dict]:
    """The log rows, oldest first, for one session or for all of them."""
    sql = ("SELECT log_id, time, session_id, action, arguments, level, decision, "
           "result, confirmation_id FROM audit_log")
    parameters: tuple = ()
    if session_id is not None:
        sql += " WHERE session_id = ?"
        parameters = (_text(session_id),)
    with _records() as conn:
        rows = conn.execute(sql + " ORDER BY log_id", parameters).fetchall()
    return [{**dict(row), "arguments": json.loads(row["arguments"])} for row in rows]


def verify_log() -> dict:
    """Work out every row's fingerprint again and compare it with the one
    stored. Returns whether the log is intact, the first row that is not, and
    the last fingerprint, which stands for the whole log up to that point."""
    with _records() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY log_id").fetchall()
    previous = ""
    for row in rows:
        if row["row_hash"] != _fingerprint(previous, [row[column] for column in LOGGED_COLUMNS]):
            return {"intact": False, "rows": len(rows), "first_bad_row": row["log_id"]}
        previous = row["row_hash"]
    return {"intact": True, "rows": len(rows), "last_fingerprint": previous or None}


def one_line(value) -> str:
    """For a summarize function: a short value from the model, such as a name,
    in quotation marks on one line. A line break in it is written as \\n and a
    quotation mark as \\", so it can neither start a line nor end the quotation.
    Curly and other look-alike quotation marks are written as codes."""
    text = json.dumps(_text(value), ensure_ascii=False)
    return _shown("".join(_code(char) if char in QUOTE_LIKE else char for char in text))


def quoted(value) -> str:
    """For a summarize function: a longer text from the model, such as a
    message, with "> " before every line, so that no line of it can pass for
    words of ours."""
    lines = _text(value).splitlines() or [""]
    return "\n".join(f"> {_shown(line)}".rstrip() for line in lines)


def shown(value) -> str:
    """For a page that shows the owner a text the model wrote, such as a saved
    reminder: the text with its line breaks kept and every character that
    cannot be seen written as its code, as on an approval card. Unlike
    one_line() and quoted() it adds no marks of its own, so the page must draw
    the text in a box of its own."""
    return _shown(_text(value), keep="\n")


def waiting_requests(limit=20) -> dict:
    """The requests still waiting for the owner's Yes or No, for the dashboard:
    the one that expires soonest first, each with the sentence its approval
    card shows. This only reads. A request past its time is left out, and
    left for confirm() to expire. No confirmation ID is given out: the card in
    the chat holds it, and the owner answers there."""
    now = _stamp(_now())
    with _records() as conn:
        rows = conn.execute(
            "SELECT session_id, action, arguments, expires_at FROM confirmations "
            "WHERE expires_at >= ? ORDER BY expires_at, rowid LIMIT ?", (now, limit)).fetchall()
        waiting = conn.execute(
            "SELECT COUNT(*) FROM confirmations WHERE expires_at >= ?", (now,)).fetchone()[0]
    items = []
    for row in rows:
        with _lock:
            action = _actions.get(row["action"])
        summary = None
        if action is not None and action.level == "yellow":
            try:
                summary = _shown(_text(_summary(action, json.loads(row["arguments"]))), keep="\n")
            except Exception as problem:
                _report(row["action"], problem)
        items.append({"session_id": row["session_id"], "action": row["action"],
                      "summary": summary or CANNOT_BE_SHOWN, "expires_at": row["expires_at"]})
    return {"items": items, "not_listed": waiting - len(items)}


# --------------------------------------------------------------------- the rules
def _execute(conn, name, arguments, session_id) -> dict:
    with _lock:
        action = _actions.get(name) if isinstance(name, str) else None
    if action is None:
        with _transaction(conn):
            _log(conn, session_id, name, arguments, "unknown", "error", "unknown action")
        return _error("Unknown action.")

    if action.level == "red":
        # Refused before the arguments are even looked at.
        with _transaction(conn):
            _log(conn, session_id, name, arguments, "red", "refused", OWNER_ONLY)
            locked = _is_locked(conn, session_id)
        return _refused(f"{OWNER_ONLY} {LOCKED}" if locked else OWNER_ONLY)

    if action.level == "yellow":
        # A locked session is turned away before its arguments are looked at, too.
        with _transaction(conn):
            if _is_locked(conn, session_id):
                _log(conn, session_id, name, arguments, "yellow", "refused", "session locked")
                return _refused(LOCKED)

    try:
        checked = _check_arguments(action, arguments)
    except ValueError as problem:
        reply = _error(str(problem))
        with _transaction(conn):
            _log(conn, session_id, name, arguments, action.level, "error", reply["message"])
        return reply

    if action.level == "green":
        return _run(conn, action, checked, session_id)
    if action.prepare is not None:
        try:
            checked = _prepared(action, checked)
        except ActionError as stop:
            reply = _error(_shorten(_escaped(_text(stop)), RESULT_LIMIT))
        except Exception as problem:
            _report(action.name, problem)
            reply = _error(FAILED)
        else:
            reply = None
        if reply is not None:
            with _transaction(conn):
                _log(conn, session_id, name, checked, "yellow", "error", reply["message"])
            return reply
    return _ask(conn, action, checked, session_id)


def _prepared(action, arguments) -> dict:
    """The final arguments of a yellow request, held to the same rules as the
    ones the model sent: an object the log can keep in full and the handler
    can take."""
    marker = _inside_action.set(action.name)
    try:
        final = action.prepare(**deepcopy(arguments))
    finally:
        _inside_action.reset(marker)
    if not isinstance(final, dict) or not all(isinstance(key, str) for key in final):
        raise TypeError("A prepare step must return the arguments as a dictionary.")
    text = json.dumps(final, ensure_ascii=False, allow_nan=False)
    text.encode("utf-8")
    inspect.signature(action.handler).bind(**final)
    if len(_escaped(text)) > ARGUMENTS_LIMIT:
        raise ActionError("This request is too long to show the owner in full.")
    # Rebuilt from its JSON, so the owner is shown exactly what will be stored and run.
    return json.loads(text)


def _ask(conn, action, arguments, session_id) -> dict:
    """Store a yellow call beside its "waiting" row. Nothing runs here."""
    try:
        # Whatever the sentence was built from, nothing in it is hidden from the owner.
        summary = _shown(_text(_summary(action, arguments)), keep="\n")
    except Exception as problem:
        _report(action.name, problem)
        with _transaction(conn):
            _log(conn, session_id, action.name, arguments, "yellow", "error", FAILED)
        return _error(FAILED)

    confirmation_id = str(uuid4())
    now = _now()
    with _transaction(conn):
        if not _make_room(conn, session_id, now):
            _log(conn, session_id, action.name, arguments, "yellow", "error", TOO_MANY)
            return _error(TOO_MANY)
        conn.execute(
            "INSERT INTO confirmations (confirmation_id, session_id, action, arguments, "
            "expires_at) VALUES (?, ?, ?, ?, ?)",
            (confirmation_id, session_id, action.name, _json(arguments),
             _stamp(now + timedelta(minutes=CONFIRM_MINUTES))))
        _log(conn, session_id, action.name, arguments, "yellow", "waiting", summary,
             confirmation_id)
    return {"status": "needs_confirmation", "confirmation_id": confirmation_id,
            "summary": summary}


def _confirm(conn, confirmation_id, approved, session_id) -> dict:
    def log(action, arguments, level, decision, result):
        _log(conn, session_id, action, arguments, level, decision, result,
             _text(confirmation_id))

    # One transaction from reading the request to deciding it: a second press,
    # or a refusal that locks the session, cannot land in the middle.
    with _transaction(conn):
        caller = _inside_action.get()
        if caller is not None:
            log("confirm", {}, "owner", "refused", f"called from inside {caller}")
            return _refused("An action cannot approve a request. Only the owner can.")
        if not isinstance(approved, bool):
            log("confirm", {}, "owner", "error", "approval was not true or false")
            return _error("Approval must be true or false.")
        if not isinstance(confirmation_id, str):
            log("confirm", {}, "owner", "error", "confirmation ID was not text")
            return _error("Invalid confirmation request.")

        pending = conn.execute(
            "SELECT * FROM confirmations WHERE confirmation_id = ?",
            (_text(confirmation_id),)).fetchone()
        # A wrong ID and another session's ID get the same answer as a used
        # one, so the reply says nothing about which IDs exist.
        if pending is None or pending["session_id"] != session_id:
            log("confirm", {}, "owner", "error", "no such request in this session")
            return _error(NOT_WAITING)

        name, stored = pending["action"], json.loads(pending["arguments"])
        now = _now()
        if pending["expires_at"] < _stamp(now):
            _expire(conn, pending, now)
            return _refused(EXPIRED)
        if approved and _is_locked(conn, session_id):
            # Left waiting, so the owner can approve it after unlocking.
            log(name, stored, "yellow", "refused", "session locked")
            return _refused(LOCKED)

        # The ID is used up, and the decision logged, before anything runs.
        conn.execute("DELETE FROM confirmations WHERE confirmation_id = ?",
                     (pending["confirmation_id"],))
        if not approved:
            log(name, stored, "yellow", "declined", "the owner said no")
            return _refused("The owner declined this action.")
        with _lock:
            action = _actions.get(name)
        if action is None or action.level != "yellow":
            log(name, stored, "yellow", "error", "action is no longer available")
            return _error("This action is no longer available.")
        log(name, stored, "yellow", "approved", "the owner said yes")

    return _run(conn, action, stored, session_id, pending["confirmation_id"])


def _run(conn, action, arguments, session_id, confirmation_id=None) -> dict:
    marker = _inside_action.set(action.name)
    try:
        # A copy: whatever the handler does to it, the log keeps what was asked.
        result = action.handler(**deepcopy(arguments))
        if not isinstance(result, dict):
            raise TypeError("An action must return a dictionary.")
        json.dumps(result, allow_nan=False)
        reply = {"status": "ok", "result": result}
    except ActionError as stop:
        # Deliberate validation messages written by our action code are safe
        # to explain. Unexpected exceptions still use the generic error below.
        reply = _error(_shorten(_escaped(_text(stop)), RESULT_LIMIT))
    except Exception as problem:
        # Avoid exposing internal exception details to the model/client.
        _report(action.name, problem)
        reply = _error(FAILED)
    finally:
        _inside_action.reset(marker)

    ran = reply["status"] == "ok"
    try:
        with _transaction(conn):
            _log(conn, session_id, action.name, arguments, action.level,
                 "ran" if ran else "error",
                 _json(reply["result"]) if ran else reply["message"], confirmation_id)
    except Exception as problem:
        _report(action.name, problem)
        if ran and action.level == "yellow":
            # Its "approved" row is in the log. Say plainly that it happened.
            return _error(RAN_UNRECORDED)
        # A look-up changes nothing, and its result is not handed back unlogged.
        return _error(UNAVAILABLE)
    return reply


def _is_locked(conn, session_id) -> bool:
    refusals = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE session_id = :s AND decision = 'refused' "
        "AND log_id > COALESCE((SELECT MAX(log_id) FROM audit_log "
        "WHERE session_id = :s AND decision = 'unlocked'), 0)",
        {"s": session_id}).fetchone()[0]
    return refusals >= LOCK_AFTER_REFUSALS


def _make_room(conn, session_id, now) -> bool:
    """Keep the stored requests few. False if there is no room for one more."""
    def waiting():
        return conn.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0]

    if waiting() >= WAITING_IN_TOTAL:
        overdue = conn.execute(
            "SELECT * FROM confirmations WHERE expires_at < ?", (_stamp(now),)).fetchall()
        for row in overdue:
            _expire(conn, row, now)
        if waiting() >= WAITING_IN_TOTAL:
            return False
    # A session keeps its newest requests; the oldest gives way to a new one.
    oldest = conn.execute(
        "SELECT * FROM confirmations WHERE session_id = ? "
        "ORDER BY expires_at DESC, rowid DESC LIMIT -1 OFFSET ?",
        (session_id, WAITING_PER_SESSION - 1)).fetchall()
    for row in oldest:
        _expire(conn, row, now)
    return True


def _expire(conn, row, now) -> None:
    """Remove a waiting request for good and log why it will never run."""
    reason = (f"not answered within {CONFIRM_MINUTES} minutes"
              if row["expires_at"] < _stamp(now) else "replaced by a newer request")
    conn.execute("DELETE FROM confirmations WHERE confirmation_id = ?",
                 (row["confirmation_id"],))
    _log(conn, row["session_id"], row["action"], json.loads(row["arguments"]), "yellow",
         "expired", reason, row["confirmation_id"])


def _valid_session(session_id) -> bool:
    # Plain letters, digits, _ and - only: nothing that can fail to encode,
    # hide a difference, or be read two ways by the database.
    return isinstance(session_id, str) and SESSION_ID.fullmatch(session_id) is not None


def _bad_session(conn, action, given) -> dict:
    # Not cut down to fit: two long IDs could then become the same session.
    # The start of what was given is kept, for whoever reads the log.
    seen = {"session_id_given": _text(given)[:NAME_LIMIT], "length": len(_text(given))}
    with _transaction(conn):
        _log(conn, INVALID_SESSION, action, seen, "unknown", "error", "invalid session ID")
    return _error("A valid session ID is required.")


def _summary(action, arguments) -> str:
    if action.summarize is not None:
        return action.summarize(**deepcopy(arguments))
    # No sentence written for this action: show the owner the exact arguments.
    return (f"Approve {action.name.replace('_', ' ')}?\n\n"
            + json.dumps(arguments, ensure_ascii=False, allow_nan=False))


def _refused(reason) -> dict:
    return {"status": "refused", "reason": reason}


def _error(message) -> dict:
    return {"status": "error", "message": message}


# ------------------------------------------------------------------- the records
@contextmanager
def _records() -> Iterator[sqlite3.Connection]:
    with connect() as conn:
        conn.executescript(TABLES)
        yield conn


@contextmanager
def _transaction(conn) -> Iterator[None]:
    """Everything inside is saved together or not at all. IMMEDIATE takes the
    write lock at the start, so what is read inside stays true until the end."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _log(conn, session_id, action, arguments, level, decision, result,
         confirmation_id=None) -> None:
    """Add one row. Call it inside _transaction, which also keeps the chain of
    fingerprints in order. Here, the one place rows are written, every field
    is cut to a fixed size and put on one line of visible characters, so that
    printed out, no row can look like two."""
    fields = [
        _now().isoformat(timespec="seconds"),
        session_id,
        _shorten(_escaped(_text(action)), NAME_LIMIT),
        _arguments_for_log(arguments),
        level,
        decision,
        _shorten(_escaped(_text(result)), RESULT_LIMIT),
        _shorten(_escaped(_text(confirmation_id)), NAME_LIMIT) if confirmation_id else None,
    ]
    last = conn.execute("SELECT row_hash FROM audit_log ORDER BY log_id DESC LIMIT 1").fetchone()
    conn.execute(
        f"INSERT INTO audit_log ({', '.join(LOGGED_COLUMNS)}, row_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*fields, _fingerprint(last["row_hash"] if last else "", fields)))


def _fingerprint(previous, fields) -> str:
    """SHA-256 of a row's content together with the fingerprint of the row
    before it. Changing any earlier row changes every fingerprint after it."""
    return hashlib.sha256(_json([previous, *fields]).encode()).hexdigest()


def _arguments_for_log(arguments) -> str:
    """The arguments as JSON on one line of visible characters, whatever they
    are. Checked arguments always fit. Anything larger was never accepted, so
    its size, the SHA-256 of its JSON and its start are kept. Anything that
    cannot be written out at all is recorded as such."""
    try:
        if not isinstance(arguments, dict):
            arguments = {"not_an_object": _text(arguments)[:RESULT_LIMIT]}
        exact = _json(arguments)
        text = _escaped(exact)
        if len(text) > ARGUMENTS_LIMIT:
            text = _json({"too_long": len(text),
                          "sha256": hashlib.sha256(exact.encode()).hexdigest(),
                          "starts": text[:RESULT_LIMIT]})
        return text
    except Exception as problem:
        # A refused call must reach the log even when its arguments cannot.
        return _json({"could_not_be_written": type(problem).__name__})


def _json(value) -> str:
    """JSON that can always be stored. Georgian and other scripts stay as
    written. Half of a character pair, which cannot be stored as it is, is
    written as its JSON escape and reads back as the same character."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _text(value) -> str:
    """Any value as text that can always be stored."""
    try:
        return str(value).encode("utf-8", "backslashreplace").decode("utf-8")
    except Exception as problem:
        return f"({type(problem).__name__})"


def _escaped(text, keep="") -> str:
    """The text with every character that cannot be seen, or that moves other
    text, written as its JSON escape: a line break as \\n, a direction override
    as \\u202e. Letters of any script stay as written. JSON treated this way
    reads back as the same characters."""
    return "".join(
        char if char in keep or (unicodedata.category(char) not in UNSEEN and char not in BLANK)
        else _code(char)
        for char in text)


def _shown(text, keep="") -> str:
    """Text as the owner is shown it: escaped, and with any blank after the
    first few in a row written as a code. What an action receives and what
    the log keeps are not changed by this."""
    shown, run = [], 0
    for char in _escaped(text, keep):
        run = run + 1 if unicodedata.category(char) == "Zs" else 0
        shown.append(char if run <= BLANKS_IN_A_ROW else f"{chr(92)}u{ord(char):04x}")
    return "".join(shown)


def _code(char) -> str:
    return json.dumps(char)[1:-1]


def _shorten(text, limit) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _now() -> datetime:
    # The real clock, unlike business dates: the log records when a call
    # happened, and a request expires 10 real minutes after it was made.
    return datetime.now(timezone.utc)


def _stamp(moment) -> str:
    # Always six decimals, so these strings sort in time order.
    return moment.isoformat(timespec="microseconds")


def _report(what, problem) -> None:
    """One line in the server log for whoever runs the server; the reply to the
    model stays vague. Text that may come from the model is written with %r, so
    a line break in it cannot start a second log line."""
    frames = traceback.extract_tb(problem.__traceback__)[-3:]
    where = " < ".join(f"{Path(frame.filename).name}:{frame.lineno}" for frame in reversed(frames))
    logger.error("%r failed at %s: %s: %r", what, where or "?", type(problem).__name__,
                 _shorten(_text(problem), RESULT_LIMIT))


# ------------------------------------------------------------ checking arguments
_JSON_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool,
               "array": list, "object": dict}
_LIMITS = {"minimum": "ge", "maximum": "le", "exclusiveMinimum": "gt", "exclusiveMaximum": "lt",
           "minLength": "min_length", "maxLength": "max_length"}


def _arguments_model(action) -> type[BaseModel]:
    """A pydantic model built from the action's JSON Schema, so the schema the
    model is shown and the check its arguments get cannot drift apart."""
    try:
        schema_text = json.dumps(action.parameters, allow_nan=False, sort_keys=True)
        return _build_model(action.name, schema_text)
    except Exception as problem:
        raise ValueError(f"The parameters of {action.name} cannot be checked: {problem}") from None


@functools.lru_cache(maxsize=None)
def _build_model(name, schema_text) -> type[BaseModel]:
    schema = json.loads(schema_text)
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}
    for key, spec in schema.get("properties", {}).items():
        declared = spec.get("type")
        kind: Any = _JSON_TYPES.get(declared, Any) if isinstance(declared, str) else Any
        limits = {ours: spec[theirs] for theirs, ours in _LIMITS.items() if theirs in spec}
        if "enum" in spec:
            kind = Literal[tuple(spec["enum"])]
        elif kind is str:
            # No text argument is unlimited, whether or not its schema says so.
            limits.setdefault("max_length", TEXT_LIMIT)
        if key in required:
            fields[key] = (kind, Field(..., **limits))
        else:
            fields[key] = (kind | None, Field(None, **limits))
    for key in required - fields.keys():
        fields[key] = (Any, Field(...))
    # As in JSON Schema: other arguments are allowed unless the schema rules them out.
    extra = "forbid" if schema.get("additionalProperties") is False else "allow"
    return create_model(f"{name}_arguments", __config__=ConfigDict(extra=extra), **fields)


def _check_arguments(action, arguments) -> dict:
    """The arguments, checked and tidied ("30" becomes 30, an empty optional
    argument is left out). Raises ValueError with the message to return."""
    if not isinstance(arguments, dict) or not all(isinstance(key, str) for key in arguments):
        raise ValueError("Arguments must be a JSON object.")
    try:
        json.dumps(arguments, allow_nan=False)
        checked = _arguments_model(action).model_validate(arguments).model_dump(exclude_none=True)
        # What the model sends goes to the prepare step first, where there is one.
        inspect.signature(action.prepare or action.handler).bind(**checked)
        text = json.dumps(checked, ensure_ascii=False, allow_nan=False)
        text.encode("utf-8")  # Half of a character pair cannot be stored.
    except ValidationError as invalid:
        problems = "; ".join(
            f"{_argument_name(action, error['loc'])}: {error['msg']}"
            for error in invalid.errors()[:3])
        message = f"Invalid action arguments: {_text(problems)}"
        raise ValueError(_shorten(message, RESULT_LIMIT)) from None
    except Exception:
        # Whatever went wrong, the call is turned down and still gets its log row.
        raise ValueError("Invalid action arguments.") from None
    # Measured as the log writes them, so whatever is accepted fits there in full.
    if len(_escaped(text)) > ARGUMENTS_LIMIT:
        raise ValueError("Invalid action arguments: too long.")
    return checked


def _argument_name(action, where) -> str:
    """How a reply names the argument at fault. The screen shows replies as
    steps, in our voice, so a name the action does not declare is repeated
    only when it looks like a name and never when it reads as a sentence."""
    if not where:
        return "arguments"
    name = str(where[0])
    declared = action.parameters.get("properties")
    if (isinstance(declared, dict) and name in declared) or NAME_LIKE.fullmatch(name):
        return name
    return "an unexpected argument"
