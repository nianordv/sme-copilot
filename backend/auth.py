"""Signing in the one demo owner.

The PIN comes from the environment variable RIGHTSIZE_OWNER_PIN, set when the
server starts. There is no default: without it nobody can sign in. The PIN is
never stored and never logged.

POST /login (pin) answers the right PIN with a cookie that holds a long random
token. The database keeps only the token's SHA-256, for 8 hours. POST /logout
removes it, GET /me says whether the caller is signed in, and POST /unlock
(session_id, pin) asks for the PIN again before it clears a chat's lock. It is
the only caller of registry.unlock().

require_owner guards every endpoint except /health, /login and /me, the ones
added later included: an endpoint is open only if its path is listed here.
same_origin turns down a request that changes something and was sent by a page
on another website. invalid_request says what is wrong with a request without
repeating what was sent, so a mistyped PIN is never sent back or logged.

After 5 wrong PINs within 5 minutes every attempt is refused for 5 minutes,
the right PIN included. The wrong PINs are counted from the audit log, as a
chat's lock is, so a restart does not reset the count. The limit is one for
everybody and it never grows, so it only slows guessing down: the PIN must
have at least 6 characters, and whoever can reach the server can keep the
owner from signing in by typing wrong PINs. A sign-in, a wrong PIN
and a sign-out each add a row to that log, in the same transaction as what
they record. Attempts made while the limit holds add none: the log is never
shortened, so nothing a stranger can repeat without end goes into it.

This stands in for a bank's own login. One PIN is not two-step verification,
and with one owner "signed in" means "may use every chat".
"""

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
from typing import Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field

from backend.actions import registry
from backend.bank.db import connect

PIN_VARIABLE = "RIGHTSIZE_OWNER_PIN"
# The limit below lets a program try about 1,400 PINs a day. Four digits would
# last it a week at most, and six digits a year on average.
PIN_MIN_LENGTH = 6
PIN_MAX_LENGTH = 64
COOKIE = "rightsize_owner"
LOGIN_HOURS = 8
WRONG_PINS = 5
WITHIN_MINUTES = 5
BLOCK_MINUTES = 5

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
OPEN_PATHS = frozenset({"/health", "/login", "/me"})
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

NOT_SIGNED_IN = "Not signed in."
WRONG_PIN = "Wrong PIN."
NOW_BLOCKED = (f"Wrong PIN. That was {WRONG_PINS} wrong PINs within {WITHIN_MINUTES} minutes, "
               f"so every attempt is refused for {BLOCK_MINUTES} minutes.")
OTHER_WEBSITE = "This request came from another website, so it was not carried out."
NO_RECORDS = ("The sign-in records are unavailable. Build the company's books with: "
              "python -m backend.bank.seed")

LOGINS = """
CREATE TABLE IF NOT EXISTS logins (
    token_hash TEXT PRIMARY KEY,
    signed_in_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
)
"""

router = APIRouter()


class PinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pin: str = Field(min_length=1, max_length=PIN_MAX_LENGTH)


class UnlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    pin: str = Field(min_length=1, max_length=PIN_MAX_LENGTH)


# ----------------------------------------------------------------- the endpoints
@router.post("/login")
def login(request: PinRequest, response: Response) -> dict:
    token = secrets.token_urlsafe(32)
    now = _now()
    expires_at = _stamp(now + timedelta(hours=LOGIN_HOURS))

    def sign_in(conn, log) -> None:
        conn.execute("DELETE FROM logins WHERE expires_at <= ?", (_stamp(now),))
        conn.execute("INSERT INTO logins (token_hash, signed_in_at, expires_at) VALUES (?, ?, ?)",
                     (_digest(token), _stamp(now), expires_at))
        log("sign_in", "signed_in", f"signed in for {LOGIN_HOURS} hours")

    _check_pin(request.pin, "sign_in", {}, wrong_status=401, when_right=sign_in)
    # Scripts on the page cannot read this cookie, and the browser does not
    # send it with a request that another website started.
    response.set_cookie(COOKIE, token, max_age=LOGIN_HOURS * 3600, path="/",
                        httponly=True, samesite="strict")
    return {"signed_in": True, "expires_at": expires_at}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(COOKIE, "")
    try:
        with registry.owner_records() as (conn, log):
            conn.execute(LOGINS)
            removed = conn.execute("DELETE FROM logins WHERE token_hash = ?", (_digest(token),))
            if removed.rowcount:
                log("sign_out", "signed_out", "signed out")
    except (OSError, sqlite3.Error) as problem:
        raise HTTPException(status_code=503, detail=NO_RECORDS) from problem
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
    return {"signed_in": False}


@router.get("/me")
def me(request: Request) -> dict:
    expires_at = _signed_in_until(request)
    if expires_at is None:
        return {"signed_in": False}
    return {"signed_in": True, "expires_at": expires_at}


@router.post("/unlock")
def unlock(request: UnlockRequest) -> dict:
    # 403 and not 401 for a wrong PIN here: the caller is signed in, and the
    # screen takes a 401 to mean "show the sign-in page".
    _check_pin(request.pin, "unlock", {"session_id": request.session_id}, wrong_status=403)
    return registry.unlock(request.session_id)


# ------------------------------------------------------- what guards the endpoints
def require_owner(request: Request) -> None:
    """Set on the whole app, so an endpoint needs the owner unless its path is
    in OPEN_PATHS. The path looked at is the whole one the request asked for:
    the matched route's own path leaves out a prefix it was added under, and
    /admin/health would pass for /health."""
    if request.scope.get("path") in OPEN_PATHS:
        return
    if _signed_in_until(request) is None:
        raise HTTPException(status_code=401, detail=NOT_SIGNED_IN)


def same_origin(request: Request) -> None:
    """A browser names the page that sent a request in its Origin header. A
    request that changes something is carried out only when that page is a
    local one. A program that is not a browser sends no Origin, and needs the
    cookie like anyone else."""
    if request.method in READ_ONLY_METHODS:
        return
    for origin in request.headers.getlist("origin"):
        try:
            host = urlsplit(origin).hostname
        except ValueError:
            host = None
        if host not in LOCAL_HOSTS:
            raise HTTPException(status_code=403, detail=OTHER_WEBSITE)


def invalid_request(request: Request, problem: RequestValidationError) -> Response:
    """The answer to a request that does not have the right form: where the
    mistake is and what kind it is. FastAPI's own answer also copies back what
    was sent, a typed PIN included, and when that cannot be written as UTF-8 it
    fails instead, leaving what was sent in the server's log."""
    details = [{"type": str(error.get("type")), "msg": str(error.get("msg")),
                "loc": [part if isinstance(part, int) else str(part) for part in error.get("loc", ())]}
               for error in problem.errors()]
    # Plain ASCII, so a name the caller made up cannot fail to be written out either.
    return Response(json.dumps({"detail": details}), status_code=422, media_type="application/json")


def _signed_in_until(request: Request) -> str | None:
    """When the caller's login ends, or None for a caller who is not signed in."""
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        with connect() as conn:
            row = conn.execute("SELECT expires_at FROM logins WHERE token_hash = ?",
                               (_digest(token),)).fetchone()
    except (OSError, sqlite3.Error):
        # No database or no logins table yet: nobody has signed in.
        return None
    if row is None or row["expires_at"] <= _stamp(_now()):
        return None
    return row["expires_at"]


# ------------------------------------------------------------------------ the PIN
def _check_pin(given: str, action: str, arguments: dict, wrong_status: int,
               when_right: Callable[..., None] | None = None) -> None:
    """Returns only for the right PIN, after when_right(conn, log) has run in
    the same transaction. A wrong PIN is logged and counted. Nothing is raised
    inside the transaction, where it would undo the row just written."""
    pin = _owner_pin()
    refusal: HTTPException | None = None
    try:
        with registry.owner_records() as (conn, log):
            conn.execute(LOGINS)
            now = _now()
            earlier = _wrong_pin_times(conn, WRONG_PINS)
            wait = _seconds_blocked(earlier, now)
            if wait:
                minutes = math.ceil(wait / 60)
                refusal = HTTPException(
                    status_code=429, headers={"Retry-After": str(wait)},
                    detail=(f"Too many wrong PINs. Try again in {minutes} "
                            f"minute{'' if minutes == 1 else 's'}."))
            elif not _same(given, pin):
                # With this one, are the newest wrong PINs enough to start the block?
                starts_block = _seconds_blocked([now, *earlier[:WRONG_PINS - 1]], now) > 0
                log(action, "refused",
                    f"wrong PIN; every attempt is refused for {BLOCK_MINUTES} minutes"
                    if starts_block else "wrong PIN", arguments)
                refusal = HTTPException(status_code=429 if starts_block else wrong_status,
                                        detail=NOW_BLOCKED if starts_block else WRONG_PIN)
            elif when_right is not None:
                when_right(conn, log)
    except (OSError, sqlite3.Error) as problem:
        raise HTTPException(status_code=503, detail=NO_RECORDS) from problem
    if refusal is not None:
        raise refusal


def _owner_pin() -> str:
    # Read on every call, like RIGHTSIZE_DB, so tests can set it.
    pin = os.environ.get(PIN_VARIABLE, "")
    if not pin:
        raise HTTPException(status_code=503, detail=(
            f"Nobody can sign in: {PIN_VARIABLE} is not set on the server. "
            "Set it and start the server again."))
    if not PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH:
        raise HTTPException(status_code=503, detail=(
            f"Nobody can sign in: {PIN_VARIABLE} on the server must have {PIN_MIN_LENGTH} to "
            f"{PIN_MAX_LENGTH} characters. Set another one and start the server again."))
    return pin


def _same(given: str, pin: str) -> bool:
    # Compared as digests, so the time taken says nothing about the PIN, its length included.
    return hmac.compare_digest(hashlib.sha256(given.encode("utf-8", "backslashreplace")).digest(),
                               hashlib.sha256(pin.encode("utf-8", "backslashreplace")).digest())


def _wrong_pin_times(conn, count) -> list[datetime]:
    """When the newest wrong PINs were typed, newest first. Under the owner's
    session a "refused" row is a wrong PIN and nothing else."""
    rows = conn.execute(
        "SELECT time FROM audit_log WHERE session_id = ? AND decision = 'refused' "
        "ORDER BY log_id DESC LIMIT ?", (registry.OWNER_SESSION, count)).fetchall()
    return [datetime.fromisoformat(row["time"]) for row in rows]


def _seconds_blocked(wrong_pins: list[datetime], now: datetime) -> int:
    """How long attempts are still refused, given the newest wrong PINs, newest
    first. Refused attempts are not logged, so they do not lengthen the block."""
    if len(wrong_pins) < WRONG_PINS:
        return 0
    newest, oldest = wrong_pins[0], wrong_pins[WRONG_PINS - 1]
    if newest - oldest > timedelta(minutes=WITHIN_MINUTES):
        return 0
    left = newest + timedelta(minutes=BLOCK_MINUTES) - now
    return max(0, math.ceil(left.total_seconds()))


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "backslashreplace")).hexdigest()


def _now() -> datetime:
    # The real clock, as in the registry: a login ends 8 real hours after it began.
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    # Always six decimals, so these strings sort in time order.
    return moment.isoformat(timespec="microseconds")
