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


def today() -> date:
    """The app's "today": the snapshot date of the books, never the system
    clock. Every business date in the app is measured from this."""
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'snapshot_date'"
        ).fetchone()
    return date.fromisoformat(row["value"])
