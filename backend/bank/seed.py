"""Builds the demo company's books as a SQLite database.

    python -m backend.bank.seed

Reads the generated invoice CSV and backend/data/company.json, and writes
backend/data/rightsize.db. Safe to re-run: the books tables are rebuilt from
scratch in one transaction, and every other table in the file (the audit log,
for one) is left alone.

Tables:
    meta                key/value: snapshot_date, currency, csv_sha256
    company             one row: name, currency, opening_balance, balance_date
    customers           customer_id, name, segment, city
    invoices            invoice_id, customer_id, issue_date, due_date,
                        paid_date, writeoff_date, status, amount, payment_terms
    scheduled_payments  due_date, category, label, amount, for the 60 days
                        after the snapshot

Dates are ISO text, amounts are GEL. Model features and outcome columns in the
CSV (paid_weekday, days_late and the rest) are left out on purpose: work
lateness out from the dates.
"""

import argparse
import calendar
import csv
import hashlib
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from backend.bank.db import db_path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CSV = ROOT / "ml" / "data" / "invoices_georgia.csv"
DEFAULT_METADATA = ROOT / "ml" / "data" / "dataset_metadata.json"
DEFAULT_COMPANY = ROOT / "backend" / "data" / "company.json"

SCHEDULE_DAYS = 60
STATUSES = {"paid", "open_not_due", "open_overdue", "written_off"}
OPEN_STATUSES = ("open_not_due", "open_overdue")
LEDGER_COLUMNS = (
    "invoice_id", "client_id", "client_segment", "client_city", "issue_date",
    "due_date", "paid_date", "writeoff_date", "status", "amount",
    "payment_terms",
)
BOOKS_TABLES = ("invoices", "customers", "scheduled_payments", "company", "meta")

SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE company (
    name TEXT NOT NULL,
    currency TEXT NOT NULL,
    opening_balance REAL NOT NULL,
    balance_date TEXT NOT NULL
);
CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    segment TEXT NOT NULL,
    city TEXT NOT NULL
);
CREATE TABLE invoices (
    invoice_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id),
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    paid_date TEXT,
    writeoff_date TEXT,
    status TEXT NOT NULL,
    amount REAL NOT NULL,
    payment_terms INTEGER NOT NULL
);
CREATE INDEX invoices_by_customer ON invoices (customer_id);
CREATE INDEX invoices_by_status ON invoices (status, due_date);
CREATE TABLE scheduled_payments (
    payment_id INTEGER PRIMARY KEY,
    due_date TEXT NOT NULL,
    category TEXT NOT NULL,
    label TEXT NOT NULL,
    amount REAL NOT NULL
);
CREATE INDEX scheduled_payments_by_date ON scheduled_payments (due_date);
"""

# Customers have only IDs in the data, so each gets an invented name the owner
# can type in chat. Any resemblance to a real business is a coincidence.
NAME_STEMS = (
    "Aragvi", "Mtkvari", "Rioni", "Enguri", "Alazani", "Iori", "Kazbegi",
    "Ushba", "Shkhara", "Tetnuldi", "Mtatsminda", "Narikala", "Metekhi",
    "Sololaki", "Vake", "Saburtalo", "Avlabari", "Ortachala", "Didube",
    "Vera", "Kakheti", "Kartli", "Imereti", "Guria", "Samegrelo", "Svaneti",
    "Racha", "Adjara", "Tusheti", "Khevsureti", "Saperavi", "Rkatsiteli",
    "Tsinandali", "Mukuzani", "Mtsvane", "Kisi", "Chinuri", "Tavkveri",
    "Tamada", "Supra", "Marani", "Kvevri", "Churchkhela", "Sulguni",
    "Tkemali", "Ajika", "Shoti", "Lobiani", "Mtsvadi", "Pkhali", "Satsivi",
    "Chakapuli", "Kubdari", "Bakuriani", "Gudauri", "Sighnaghi", "Mtskheta",
    "Vardzia", "Ananuri", "Gelati", "Bagrati", "Gergeti", "Kolkheti",
    "Medea", "Amirani", "Dila", "Mze", "Zgva", "Mta", "Vazi", "Ezo",
    "Sakhli", "Keria", "Bazari", "Dzveli", "Akhali", "Okro", "Lurji",
    "Tetri", "Shavi",
)
NAME_KINDS = {
    "Restaurant": ("Restaurant", "Kitchen", "Tavern"),
    "Cafe": ("Cafe", "Coffee House", "Bakery"),
    "Hotel": ("Hotel", "Guesthouse", "Inn"),
    "Grocery store": ("Market", "Grocery", "Mini-Market"),
    "Bar/Pub": ("Bar", "Pub", "Wine Bar"),
    "Catering": ("Catering", "Banquets", "Events"),
}
OTHER_KINDS = ("Trading",)


def display_names(segments: dict[str, str]) -> dict[str, str]:
    """A unique readable name per customer ID, the same on every run.

    The ID's hash picks a starting name; if another customer already has it,
    the next free one is used. Customers are taken in ID order, so the result
    depends only on the set of customers, never on row order."""
    names: dict[str, str] = {}
    taken: set[str] = set()
    for customer_id in sorted(segments):
        kinds = NAME_KINDS.get(segments[customer_id], OTHER_KINDS)
        choices = len(NAME_STEMS) * len(kinds)
        start = int(hashlib.sha256(customer_id.encode()).hexdigest(), 16)
        name = None
        for step in range(choices):
            pick = (start + step) % choices
            stem = NAME_STEMS[pick % len(NAME_STEMS)]
            kind = kinds[pick // len(NAME_STEMS)]
            if f"{stem} {kind}" not in taken:
                name = f"{stem} {kind}"
                break
        if name is None:  # More customers of one kind than names for them.
            name = f"{kinds[0]} {customer_id}"
        names[customer_id] = name
        taken.add(name)
    return names


def read_snapshot(metadata_path: Path, csv_path: Path) -> tuple[date, str, str]:
    """Snapshot date, currency and CSV hash, after checking that the metadata
    describes this CSV and not an older one."""
    metadata = json.loads(metadata_path.read_text())
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    if metadata["csv_sha256"] != digest:
        raise ValueError(
            f"{csv_path.name} does not match {metadata_path.name}. "
            "Regenerate both with: python -m ml.generate_invoices_ge"
        )
    return (
        date.fromisoformat(metadata["snapshot_date"]),
        metadata.get("currency", "GEL"),
        digest,
    )


def read_ledger(csv_path: Path) -> tuple[list[tuple], dict[str, tuple[str, str]]]:
    """Invoice rows (ledger columns only) and each customer's segment and city."""
    invoices: list[tuple] = []
    customers: dict[str, tuple[str, str]] = {}
    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(LEDGER_COLUMNS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"CSV is missing columns: {sorted(missing)}")
        for row in reader:
            invoice_id = row["invoice_id"]
            if row["status"] not in STATUSES:
                raise ValueError(f"{invoice_id}: unknown status {row['status']!r}")
            amount = float(row["amount"])
            if amount <= 0:
                raise ValueError(f"{invoice_id}: amount must be positive")
            for column in ("issue_date", "due_date", "paid_date", "writeoff_date"):
                if row[column]:
                    date.fromisoformat(row[column])

            customer_id = row["client_id"]
            profile = (row["client_segment"], row["client_city"])
            if customers.setdefault(customer_id, profile) != profile:
                raise ValueError(f"{customer_id}: segment or city changes between invoices")

            invoices.append((
                invoice_id, customer_id, row["issue_date"], row["due_date"],
                row["paid_date"] or None, row["writeoff_date"] or None,
                row["status"], amount, int(row["payment_terms"]),
            ))
    return invoices, customers


def scheduled_payments(company: dict[str, Any], snapshot: date) -> list[tuple]:
    """Every payment due in the SCHEDULE_DAYS days after the snapshot, as
    (due_date, category, label, amount), in date order."""
    first = snapshot + timedelta(days=1)
    last = snapshot + timedelta(days=SCHEDULE_DAYS)
    rows: list[tuple] = []

    months = {(first.year, first.month)}
    cursor = first
    while cursor <= last:
        months.add((cursor.year, cursor.month))
        cursor += timedelta(days=1)

    for rule in company.get("monthly_payments", []):
        for year, month in sorted(months):
            month_length = calendar.monthrange(year, month)[1]
            for day in rule["days_of_month"]:
                if not 1 <= day <= 31:
                    raise ValueError(f"{rule['label']}: day of month {day}")
                # The 31st in a 30-day month means its last day.
                due = date(year, month, min(day, month_length))
                if first <= due <= last:
                    rows.append((
                        due.isoformat(), rule["category"], rule["label"],
                        _amount(rule),
                    ))

    for payment in company.get("one_off_payments", []):
        due = date.fromisoformat(payment["date"])
        if first <= due <= last:
            rows.append((
                due.isoformat(), payment["category"], payment["label"],
                _amount(payment),
            ))

    return sorted(set(rows))


def _amount(entry: dict[str, Any]) -> float:
    amount = round(float(entry["amount"]), 2)
    if amount <= 0:
        raise ValueError(f"{entry['label']}: amount must be positive")
    return amount


def seed(
    csv_path: Path = DEFAULT_CSV,
    metadata_path: Path = DEFAULT_METADATA,
    company_path: Path = DEFAULT_COMPANY,
    database: Path | None = None,
) -> dict[str, Any]:
    database = Path(database) if database else db_path()
    if not csv_path.exists() or not metadata_path.exists():
        raise FileNotFoundError(
            f"{csv_path} or {metadata_path.name} is missing. "
            "Generate them with: python -m ml.generate_invoices_ge"
        )

    # Read and check everything before touching the database.
    snapshot, currency, digest = read_snapshot(metadata_path, csv_path)
    invoices, customers = read_ledger(csv_path)
    company = json.loads(company_path.read_text())
    payments = scheduled_payments(company, snapshot)
    names = display_names({cid: segment for cid, (segment, _) in customers.items()})
    opening_balance = round(float(company["opening_balance"]), 2)

    database.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: we open and close the transaction ourselves, so
    # dropping and refilling the tables either all happens or none of it does.
    conn = sqlite3.connect(database, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in BOOKS_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("snapshot_date", snapshot.isoformat()),
            ("currency", currency),
            ("csv_sha256", digest),
            ("company_numbers_are_placeholders", str(int(bool(company.get("placeholder"))))),
        ])
        conn.execute(
            "INSERT INTO company VALUES (?, ?, ?, ?)",
            (company["company_name"], company.get("currency", currency),
             opening_balance, snapshot.isoformat()),
        )
        conn.executemany(
            "INSERT INTO customers VALUES (?, ?, ?, ?)",
            [(cid, names[cid], segment, city)
             for cid, (segment, city) in sorted(customers.items())],
        )
        conn.executemany("INSERT INTO invoices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", invoices)
        conn.executemany(
            "INSERT INTO scheduled_payments (due_date, category, label, amount) "
            "VALUES (?, ?, ?, ?)",
            payments,
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return summary(database)


def summary(database: Path) -> dict[str, Any]:
    conn = sqlite3.connect(database)
    try:
        one = lambda sql: conn.execute(sql).fetchone()[0]
        open_list = ", ".join(f"'{status}'" for status in OPEN_STATUSES)
        return {
            "database": str(database),
            "snapshot_date": one("SELECT value FROM meta WHERE key = 'snapshot_date'"),
            "invoices": one("SELECT COUNT(*) FROM invoices"),
            "customers": one("SELECT COUNT(*) FROM customers"),
            "owed": round(one(
                f"SELECT COALESCE(SUM(amount), 0) FROM invoices WHERE status IN ({open_list})"
            ), 2),
            "overdue": round(one(
                "SELECT COALESCE(SUM(amount), 0) FROM invoices WHERE status = 'open_overdue'"
            ), 2),
            "opening_balance": one("SELECT opening_balance FROM company"),
            "scheduled_payments": one("SELECT COUNT(*) FROM scheduled_payments"),
            "scheduled_total": round(one(
                "SELECT COALESCE(SUM(amount), 0) FROM scheduled_payments"
            ), 2),
            "placeholders": one(
                "SELECT value FROM meta WHERE key = 'company_numbers_are_placeholders'"
            ) == "1",
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--company", type=Path, default=DEFAULT_COMPANY)
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args()

    result = seed(args.csv, args.metadata, args.company, args.db)
    print(f"Books written to {result['database']}")
    print(f"  today in the books   {result['snapshot_date']}")
    print(f"  invoices             {result['invoices']:,}")
    print(f"  customers            {result['customers']:,}")
    print(f"  owed by customers    GEL {result['owed']:,.2f}")
    print(f"  of which overdue     GEL {result['overdue']:,.2f}")
    print(f"  bank balance         GEL {result['opening_balance']:,.2f}")
    print(f"  scheduled payments   {result['scheduled_payments']} in the next "
          f"{SCHEDULE_DAYS} days, GEL {result['scheduled_total']:,.2f}")
    if result["placeholders"]:
        print("  NOTE: balance and scheduled payments are placeholders "
              "from backend/data/company.json")


if __name__ == "__main__":
    main()
