"""Where the demo company's books live, and what day it is in them."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "rightsize.db"


def db_path() -> Path:
    # Read on every call so tests can point RIGHTSIZE_DB at a temporary file.
    return Path(os.environ.get("RIGHTSIZE_DB", DEFAULT_DB))


@contextmanager
def connect(create: bool = False) -> Iterator[sqlite3.Connection]:
    path = db_path()
    if not create and not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist. Build it with: python -m backend.bank.seed"
        )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    """For code that only reads. A table nothing was saved to yet does not
    exist, and a reader must not be the one to make it."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def reporting_date(conn: sqlite3.Connection) -> date:
    if has_table(conn, 'ledger_settings'):
        row = conn.execute('SELECT report_date FROM ledger_settings WHERE id=1').fetchone()
        if row:
            return date.fromisoformat(row['report_date'])
    row = conn.execute("SELECT value FROM meta WHERE key='snapshot_date'").fetchone()
    return date.fromisoformat(row['value'])


def today() -> date:
    """The explicit reporting date; authentication still uses the real clock."""
    with connect() as conn:
        return reporting_date(conn)
