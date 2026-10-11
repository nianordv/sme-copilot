"""Shared set-up for tests that need a small database of hand-made books."""

import csv
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.actions import registry
from backend.bank.seed import LEDGER_COLUMNS, seed

SNAPSHOT = "2026-10-03"
# The ledger columns plus two of the model's columns, which must not be copied.
HEADER = [*LEDGER_COLUMNS, "paid_weekday", "days_late"]
COMPANY = {
    "placeholder": True,
    "company_name": "Test Wholesale",
    "currency": "GEL",
    "opening_balance": 1000.0,
    "monthly_payments": [
        {"category": "salaries", "label": "Salaries", "days_of_month": [10], "amount": 50.0},
        {"category": "taxes", "label": "Taxes", "days_of_month": [15], "amount": 30.0},
    ],
    "one_off_payments": [
        {"category": "other", "label": "New van", "date": "2026-10-20", "amount": 70.0},
    ],
}


def invoice(invoice_id, customer_id, segment, city, issue, due, amount, paid="", writeoff="", terms=30):
    """One CSV row. The status follows from the dates, as in the generator."""
    if paid:
        status = "paid"
    elif writeoff:
        status = "written_off"
    else:
        status = "open_overdue" if due < SNAPSHOT else "open_not_due"
    return [invoice_id, customer_id, segment, city, issue, due, paid, writeoff,
            status, f"{amount:.2f}", str(terms), "", ""]


def use_temporary_database(test):
    """Points RIGHTSIZE_DB at an empty temporary database for one test. The
    registry adds its own tables to it; there are no books in it."""
    database = Path(tempfile.mkdtemp()) / "records.db"
    sqlite3.connect(database).close()
    pointed = patch.dict(os.environ, {"RIGHTSIZE_DB": str(database)})
    pointed.start()
    test.addCleanup(pointed.stop)
    return database


class BooksTestCase(unittest.TestCase):
    """Points RIGHTSIZE_DB at a temporary database for the length of a test."""

    rows: list = []
    names: dict = {}  # customer_id -> display name, to replace the generated one
    seed_on_setup = True

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.csv = self.folder / "invoices.csv"
        self.metadata = self.folder / "metadata.json"
        self.company = self.folder / "company.json"
        self.database = self.folder / "books.db"
        self.company.write_text(json.dumps(COMPANY))
        self.write_books(self.rows)
        self.previous = os.environ.get("RIGHTSIZE_DB")
        os.environ["RIGHTSIZE_DB"] = str(self.database)
        if self.seed_on_setup:
            self.run_seed()
            for customer_id, name in self.names.items():
                self.query("UPDATE customers SET name = ? WHERE customer_id = ?", (name, customer_id))

    def tearDown(self):
        if self.previous is None:
            del os.environ["RIGHTSIZE_DB"]
        else:
            os.environ["RIGHTSIZE_DB"] = self.previous

    def write_books(self, rows):
        with self.csv.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(HEADER)
            writer.writerows(rows)
        digest = hashlib.sha256(self.csv.read_bytes()).hexdigest()
        self.metadata.write_text(json.dumps({
            "snapshot_date": SNAPSHOT, "currency": "GEL", "csv_sha256": digest,
        }))

    def run_seed(self):
        return seed(self.csv, self.metadata, self.company)

    def query(self, sql, parameters=()):
        conn = sqlite3.connect(self.database)
        try:
            rows = conn.execute(sql, parameters).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()


NO_ARGUMENTS = {"type": "object", "properties": {}, "additionalProperties": False}
ANY_BOOKS = [invoice("INV-01", "CL0001", "Cafe", "Tbilisi", "2026-08-01", "2026-08-31", 100)]


class RegistryTestCase(BooksTestCase):
    """An empty registry over a temporary database. The real actions are put
    back afterwards."""

    rows = ANY_BOOKS

    def setUp(self):
        super().setUp()
        emptied = patch.dict(registry._actions, {}, clear=True)
        emptied.start()
        self.addCleanup(emptied.stop)
        self.calls = []  # (action name, arguments) for every handler that ran

    def add(self, name, level, parameters=NO_ARGUMENTS, handler=None, summarize=None):
        def record(**arguments):
            self.calls.append((name, arguments))
            return {"done": name}

        registry.register(registry.Action(
            name, f"A {level} test action.", parameters, level, handler or record, summarize))

    def decisions(self, session_id="s1"):
        return [(row["action"], row["decision"]) for row in registry.audit_log(session_id)]
