import csv
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from backend.bank import db
from backend.bank.seed import (
    DEFAULT_CSV, DEFAULT_METADATA, LEDGER_COLUMNS, display_names,
    scheduled_payments, seed,
)

SNAPSHOT = "2026-10-03"
# The ledger columns plus two of the model's columns, which must not be copied.
HEADER = [*LEDGER_COLUMNS, "paid_weekday", "days_late"]
ROWS = [
    ["INV-1", "CL0001", "Cafe", "Tbilisi", "2026-08-01", "2026-08-31", "2026-09-02", "", "paid", "100.10", "30", "2", "2"],
    ["INV-2", "CL0001", "Cafe", "Tbilisi", "2026-09-01", "2026-09-16", "", "", "open_overdue", "200.20", "15", "", ""],
    ["INV-3", "CL0002", "Hotel", "Batumi", "2026-09-20", "2026-10-20", "", "", "open_not_due", "300.30", "30", "", ""],
    ["INV-4", "CL0002", "Hotel", "Batumi", "2026-03-01", "2026-03-31", "", "2026-06-29", "written_off", "400.40", "30", "", ""],
]
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


def owed_straight_from_csv(csv_path):
    """The amount customers owe, computed without going through the seed."""
    with open(csv_path, newline="") as handle:
        return round(sum(
            float(row["amount"]) for row in csv.DictReader(handle)
            if row["status"] in ("open_not_due", "open_overdue")
        ), 2)


def csv_row_count(csv_path):
    with open(csv_path, newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.csv = self.folder / "invoices.csv"
        self.metadata = self.folder / "metadata.json"
        self.company = self.folder / "company.json"
        self.database = self.folder / "books.db"
        self.company.write_text(json.dumps(COMPANY))
        self.write_books(ROWS)
        self.previous = os.environ.get("RIGHTSIZE_DB")
        os.environ["RIGHTSIZE_DB"] = str(self.database)

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

    def query(self, sql):
        conn = sqlite3.connect(self.database)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_row_counts_and_owed_match_the_csv(self):
        result = self.run_seed()
        self.assertEqual(result["invoices"], csv_row_count(self.csv))
        self.assertEqual(result["customers"], 2)
        self.assertEqual(result["owed"], owed_straight_from_csv(self.csv))
        self.assertEqual(result["owed"], 500.50)
        self.assertEqual(result["overdue"], 200.20)

    def test_only_ledger_columns_are_copied(self):
        self.run_seed()
        columns = [row[1] for row in self.query("PRAGMA table_info(invoices)")]
        self.assertEqual(columns, [
            "invoice_id", "customer_id", "issue_date", "due_date", "paid_date",
            "writeoff_date", "status", "amount", "payment_terms",
        ])

    def test_blank_dates_become_null(self):
        self.run_seed()
        self.assertEqual(
            self.query("SELECT paid_date, writeoff_date FROM invoices WHERE invoice_id = 'INV-2'"),
            [(None, None)],
        )

    def test_today_is_the_snapshot_not_the_clock(self):
        self.run_seed()
        self.assertEqual(db.today(), date(2026, 10, 3))
        self.assertNotEqual(db.today(), date.today())

    def test_balance_and_schedule_come_from_the_company_file(self):
        self.run_seed()
        self.assertEqual(self.query("SELECT opening_balance, balance_date FROM company"), [(1000.0, SNAPSHOT)])
        self.assertEqual(
            self.query("SELECT due_date, label, amount FROM scheduled_payments ORDER BY due_date"),
            [
                ("2026-10-10", "Salaries", 50.0), ("2026-10-15", "Taxes", 30.0),
                ("2026-10-20", "New van", 70.0),
                ("2026-11-10", "Salaries", 50.0), ("2026-11-15", "Taxes", 30.0),
            ],
        )

    def test_rerun_replaces_the_books_and_keeps_other_tables(self):
        self.run_seed()
        conn = sqlite3.connect(self.database)
        conn.execute("CREATE TABLE audit_log (note TEXT)")
        conn.execute("INSERT INTO audit_log VALUES ('kept')")
        conn.commit()
        conn.close()

        self.write_books(ROWS[:3])  # Regenerated data: different rows, same columns.
        self.assertEqual(self.run_seed()["invoices"], 3)
        self.assertEqual(self.query("SELECT note FROM audit_log"), [("kept",)])

    def test_failed_seed_leaves_the_old_books_in_place(self):
        self.run_seed()
        broken = [list(row) for row in ROWS]
        broken[1][8] = "half_paid"
        self.write_books(broken)
        with self.assertRaises(ValueError):
            self.run_seed()
        self.assertEqual(self.query("SELECT COUNT(*) FROM invoices"), [(4,)])

    def test_csv_that_does_not_match_its_metadata_is_refused(self):
        with self.csv.open("a", newline="") as handle:
            csv.writer(handle).writerow(["INV-9", *ROWS[0][1:]])
        with self.assertRaises(ValueError):
            self.run_seed()
        self.assertFalse(self.database.exists())

    def test_missing_database_says_how_to_build_it(self):
        with self.assertRaises(FileNotFoundError) as caught:
            db.today()
        self.assertIn("backend.bank.seed", str(caught.exception))


class NameTests(unittest.TestCase):
    def segments(self):
        kinds = ["Restaurant", "Cafe", "Hotel", "Grocery store", "Bar/Pub", "Catering"]
        return {f"CL{n:04d}": kinds[n % len(kinds)] for n in range(1, 501)}

    def test_names_are_unique(self):
        names = display_names(self.segments())
        self.assertEqual(len(set(names.values())), 500)

    def test_names_do_not_depend_on_row_order(self):
        segments = self.segments()
        backwards = dict(reversed(list(segments.items())))
        self.assertEqual(display_names(segments), display_names(backwards))

    def test_unknown_segment_and_too_many_customers_still_get_names(self):
        names = display_names({f"CL{n:04d}": "Kiosk" for n in range(200)})
        self.assertEqual(len(set(names.values())), 200)


class ScheduleTests(unittest.TestCase):
    def test_only_the_60_days_after_the_snapshot(self):
        rows = scheduled_payments(COMPANY, date(2026, 10, 3))
        dates = [row[0] for row in rows]
        self.assertEqual(dates, sorted(dates))
        self.assertGreater(min(dates), SNAPSHOT)
        self.assertLessEqual(max(dates), "2026-12-02")

    def test_payment_due_on_the_snapshot_day_is_not_upcoming(self):
        rule = {"category": "stock", "label": "Stock", "days_of_month": [3], "amount": 5.0}
        rows = scheduled_payments({"monthly_payments": [rule]}, date(2026, 10, 3))
        self.assertEqual([row[0] for row in rows], ["2026-11-03"])

    def test_day_31_means_the_last_day_of_a_shorter_month(self):
        rule = {"category": "stock", "label": "Stock", "days_of_month": [31], "amount": 5.0}
        rows = scheduled_payments({"monthly_payments": [rule]}, date(2026, 10, 3))
        self.assertEqual([row[0] for row in rows], ["2026-10-31", "2026-11-30"])


@unittest.skipUnless(
    DEFAULT_CSV.exists() and DEFAULT_METADATA.exists(),
    "generate the data first: python -m ml.generate_invoices_ge",
)
class RealBooksTests(unittest.TestCase):
    def test_generated_books_match_the_csv(self):
        database = Path(tempfile.mkdtemp()) / "books.db"
        result = seed(database=database)
        self.assertEqual(result["invoices"], csv_row_count(DEFAULT_CSV))
        self.assertEqual(result["owed"], owed_straight_from_csv(DEFAULT_CSV))


if __name__ == "__main__":
    unittest.main()
