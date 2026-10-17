"""Owner-reported cash, not a bank connection. Money is stored in whole cents.

The opening balance is the starting anchor. Record only subsequent movements,
including later events on the same date. Predictions never
write here. Corrections append opposite entries; original records remain.
"""

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import re

from backend.bank.db import connect, has_table, reporting_date
from backend.actions.registry import ActionError

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_settings (
 id INTEGER PRIMARY KEY CHECK(id=1), opening_cents INTEGER NOT NULL,
 opening_date TEXT NOT NULL, report_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cash_entries (
 entry_id INTEGER PRIMARY KEY, reference TEXT NOT NULL UNIQUE,
 event_date TEXT NOT NULL, direction TEXT NOT NULL CHECK(direction IN ('in','out')),
 amount_cents INTEGER NOT NULL CHECK(amount_cents>0), category TEXT NOT NULL,
 note TEXT NOT NULL, invoice_id TEXT, obligation_id TEXT, loan_id INTEGER,
 reversal_of INTEGER UNIQUE REFERENCES cash_entries(entry_id), included_in_opening INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recorded_loans (
 loan_id INTEGER PRIMARY KEY, reference TEXT NOT NULL UNIQUE, principal_cents INTEGER NOT NULL,
 annual_rate TEXT NOT NULL, fees_cents INTEGER NOT NULL, disbursed_on TEXT NOT NULL,
 first_payment_date TEXT NOT NULL, months INTEGER NOT NULL, repayment TEXT NOT NULL,
 included_in_opening INTEGER NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loan_instalments (
 obligation_id TEXT PRIMARY KEY, loan_id INTEGER NOT NULL REFERENCES recorded_loans(loan_id),
 due_date TEXT NOT NULL, principal_cents INTEGER NOT NULL, interest_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS loan_cancellations (
 reference TEXT PRIMARY KEY, loan_id INTEGER NOT NULL UNIQUE, reason TEXT NOT NULL, event_date TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS cash_no_update BEFORE UPDATE ON cash_entries
 BEGIN SELECT RAISE(ABORT, 'Cash records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS cash_no_delete BEFORE DELETE ON cash_entries
 BEGIN SELECT RAISE(ABORT, 'Cash records are append-only'); END;
"""


def cents(value, positive=False):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) > Decimal('1000000000'):
            raise ValueError()
        result = int((number * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except Exception as exc:
        raise ActionError('Enter a finite monetary amount, at most GEL 1 billion.') from exc
    if positive and result <= 0:
        raise ActionError('The amount must be at least GEL 0.01.')
    return result


def money(value):
    return round(value / 100, 2)


DATE_TEXT = re.compile(r'\d{4}-\d{2}-\d{2}')


def calendar_day(text):
    """A real day written YYYY-MM-DD, the only form stored. SQL here compares
    dates as text: Python also reads 20261003 as 3 October, but stored like
    that a receipt would never count and its reversal would."""
    try:
        if isinstance(text, str) and DATE_TEXT.fullmatch(text):
            return date.fromisoformat(text)
    except ValueError:
        pass
    raise ActionError('Write dates as YYYY-MM-DD, a real day such as 2026-10-03.')


def schema(conn):
    # execute individually: executescript would commit an existing transaction.
    import sqlite3

    statement = ''
    for line in SCHEMA.splitlines():
        statement += line + '\n'
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ''
    if 'included_in_opening' not in {
        row[1] for row in conn.execute('PRAGMA table_info(cash_entries)')
    }:
        conn.execute(
            'ALTER TABLE cash_entries ADD COLUMN included_in_opening INTEGER NOT NULL DEFAULT 0'
        )


def settings(conn):
    return (
        conn.execute('SELECT * FROM ledger_settings WHERE id=1').fetchone()
        if has_table(conn, 'ledger_settings')
        else None
    )


def require_setup(conn):
    result = settings(conn)
    if result is None:
        raise ActionError('Set the opening cash balance and date before recording transactions.')
    return result


def canonical(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False)


def initialize(amount, opening_date):
    day = calendar_day(opening_date)
    if day > date.max - timedelta(days=60):
        raise ActionError('Choose a reporting date with room for the next 60 forecast days.')
    amount_cents = cents(amount)
    with connect() as conn:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            schema(conn)
            existing = settings(conn)
            if existing:
                if (
                    existing['opening_cents'] != amount_cents
                    or existing['opening_date'] != day.isoformat()
                ):
                    raise ActionError(
                        'The opening balance is already set. Use an approved correction entry; do not overwrite history.'
                    )
                return {'message': 'The opening balance was already set. Nothing was added.'}
            base = date.fromisoformat(
                conn.execute("SELECT value FROM meta WHERE key='snapshot_date'").fetchone()[0]
            )
            if day < base:
                raise ActionError('The opening date cannot precede the imported books snapshot.')
            conn.execute(
                'INSERT INTO ledger_settings VALUES (1,?,?,?)',
                (amount_cents, day.isoformat(), day.isoformat()),
            )
    return {
        'message': 'Opening cash saved. Future cash changes require recorded receipts or payments.',
        'balance': money(amount_cents),
        'as_of': day.isoformat(),
    }


def balance(conn):
    config = settings(conn)
    if config is None:
        old = conn.execute('SELECT opening_balance, balance_date FROM company').fetchone()
        return {
            'balance': old['opening_balance'],
            'as_of': old['balance_date'],
            'initialized': False,
            'source': 'Seeded demo balance; owner setup not completed.',
        }
    rows = conn.execute(
        'SELECT direction, amount_cents FROM cash_entries WHERE event_date<=? AND included_in_opening=0',
        (config['report_date'],),
    ).fetchall()
    total = config['opening_cents'] + sum(
        row['amount_cents'] * (1 if row['direction'] == 'in' else -1) for row in rows
    )
    return {
        'balance': money(total),
        'as_of': config['report_date'],
        'initialized': True,
        'opening_balance': money(config['opening_cents']),
        'opening_date': config['opening_date'],
        'source': 'Owner-reported records. Not verified against a bank account.',
    }


def payments_by_invoice(conn, as_of):
    if not has_table(conn, 'cash_entries'):
        return {}
    rows = conn.execute(
        "SELECT invoice_id, SUM(CASE direction WHEN 'in' THEN amount_cents ELSE -amount_cents END) n FROM cash_entries WHERE invoice_id IS NOT NULL AND event_date<=? GROUP BY invoice_id",
        (str(as_of),),
    )
    return {r['invoice_id']: r['n'] for r in rows}


def paid_obligations(conn, as_of):
    if not has_table(conn, 'cash_entries'):
        return {}
    rows = conn.execute(
        "SELECT obligation_id, SUM(CASE direction WHEN 'out' THEN amount_cents ELSE -amount_cents END) n FROM cash_entries WHERE obligation_id IS NOT NULL AND event_date<=? GROUP BY obligation_id",
        (str(as_of),),
    )
    return {r['obligation_id']: r['n'] for r in rows}


def obligations(conn):
    """Known bills plus recurring rules beyond the original imported window."""
    result = [
        dict(r) | {'obligation_id': f"scheduled:{r['payment_id']}"}
        for r in conn.execute('SELECT * FROM scheduled_payments')
    ]
    meta = dict(conn.execute('SELECT key,value FROM meta').fetchall())
    base = date.fromisoformat(meta['snapshot_date'])
    end = reporting_date(conn) + timedelta(days=60)
    if 'monthly_payments_json' in meta:
        rules = json.loads(meta['monthly_payments_json'])
    else:
        # Older demo databases lack stored rules. Only extend a pattern that
        # appears in at least two different months; a one-off is never repeated.
        groups = {}
        for row in result:
            d = date.fromisoformat(row['due_date'])
            key = (row['category'], row['label'], row['amount'], d.day)
            groups.setdefault(key, set()).add((d.year, d.month))
        rules = [
            {'category': k[0], 'label': k[1], 'amount': k[2], 'days_of_month': [k[3]]}
            for k, v in groups.items()
            if len(v) > 1
        ]
    day = base + timedelta(days=61)
    while day <= end:
        for rule in rules:
            # A payment on the 31st falls on month end in shorter months,
            # just as it does in the imported schedule.
            due_days = {min(n, monthrange(day.year, day.month)[1]) for n in rule['days_of_month']}
            if day.day in due_days:
                key = canonical([rule['category'], rule['label'], rule['amount'], day.isoformat()])
                result.append(
                    {
                        'obligation_id': 'recurring:'
                        + hashlib.sha256(key.encode()).hexdigest()[:20],
                        'due_date': day.isoformat(),
                        'category': rule['category'],
                        'label': rule['label'],
                        'amount': rule['amount'],
                    }
                )
        day += timedelta(days=1)
    if has_table(conn, 'recorded_loans'):
        for row in conn.execute(
            'SELECT i.*,l.reference FROM loan_instalments i JOIN recorded_loans l USING(loan_id) WHERE l.cancelled=0'
        ):
            result.append(
                {
                    'obligation_id': row['obligation_id'],
                    'due_date': row['due_date'],
                    'category': 'loan',
                    'label': f"Loan repayment: {row['reference']}",
                    'amount': money(row['principal_cents'] + row['interest_cents']),
                }
            )
    paid = paid_obligations(conn, reporting_date(conn))
    for row in result:
        row['amount'] = money(max(0, cents(row['amount']) - paid.get(row['obligation_id'], 0)))
    return sorted(
        [r for r in result if r['amount'] > 0], key=lambda r: (r['due_date'], r['obligation_id'])
    )


def _entry(
    conn,
    *,
    reference,
    event_date,
    direction,
    amount_cents,
    category,
    note='',
    invoice_id=None,
    obligation_id=None,
    loan_id=None,
    reversal_of=None,
    included_in_opening=False,
):
    data = dict(
        reference=reference,
        event_date=event_date,
        direction=direction,
        amount_cents=amount_cents,
        category=category,
        note=note,
        invoice_id=invoice_id,
        obligation_id=obligation_id,
        loan_id=loan_id,
        reversal_of=reversal_of,
        included_in_opening=int(included_in_opening),
    )
    payload = canonical(data)
    existing = conn.execute(
        'SELECT entry_id,payload FROM cash_entries WHERE reference=?', (reference,)
    ).fetchone()
    if existing:
        if existing['payload'] != payload:
            raise ActionError(
                'That reference already belongs to a different transaction. Use its original record or a new reference.'
            )
        return existing['entry_id'], False
    keys = ','.join(data)
    cursor = conn.execute(
        f"INSERT INTO cash_entries ({keys},payload) VALUES ({','.join('?' for _ in data)},?)",
        (*data.values(), payload),
    )
    return cursor.lastrowid, True


def record_cash(
    reference,
    event_date,
    direction,
    amount,
    category,
    note='',
    invoice_id=None,
    obligation_id=None,
    included_in_opening=False,
):
    amount_cents = cents(amount, positive=True)
    if category == 'invoice' and not invoice_id:
        raise ActionError(
            'An invoice receipt needs its invoice ID so receivables and cash update together.'
        )
    if category == 'loan' and (
        direction != 'out' or not obligation_id or not obligation_id.startswith('loan:')
    ):
        raise ActionError(
            'Use record_existing_loan for proceeds. For a repayment, choose the matching loan obligation ID.'
        )
    day = calendar_day(event_date)
    if direction not in ('in', 'out'):
        raise ActionError('A cash entry must be money in or money out.')
    with connect() as conn:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            config = require_setup(conn)
            opening = date.fromisoformat(config['opening_date'])
            base = date.fromisoformat(
                conn.execute("SELECT value FROM meta WHERE key='snapshot_date'").fetchone()[0]
            )
            allowed = (
                base <= day <= opening
                if included_in_opening
                else opening <= day <= date.fromisoformat(config['report_date'])
            )
            if not allowed:
                raise ActionError(
                    'Record actual cash only between the opening date and the reporting date. Advance the reporting date for later events. Earlier receipts/payments can be reconciled only when marked already included in the opening balance.'
                )
            existing = conn.execute(
                'SELECT * FROM cash_entries WHERE reference=?', (reference,)
            ).fetchone()
            if invoice_id and not existing:
                inv = conn.execute(
                    'SELECT * FROM invoices WHERE invoice_id=?', (invoice_id,)
                ).fetchone()
                if (
                    inv is None
                    or inv['paid_date'] is not None
                    or inv['writeoff_date'] is not None
                    or inv['issue_date'] > event_date
                ):
                    raise ActionError(
                        'Choose an open invoice issued on or before the payment date.'
                    )
                remaining = cents(inv['amount']) - payments_by_invoice(
                    conn, config['report_date']
                ).get(invoice_id, 0)
                if direction != 'in' or amount_cents > remaining:
                    raise ActionError(
                        'An invoice receipt must be money in and cannot exceed the remaining invoice amount.'
                    )
            if obligation_id and not existing:
                row = next(
                    (r for r in obligations(conn) if r['obligation_id'] == obligation_id), None
                )
                if direction != 'out' or row is None or amount_cents > cents(row['amount']):
                    raise ActionError(
                        'Select an unpaid obligation. A settlement cannot exceed its remaining amount.'
                    )
            if invoice_id and obligation_id:
                raise ActionError(
                    'A transaction cannot settle both a receivable and an obligation.'
                )
            entry_id, added = _entry(
                conn,
                reference=reference,
                event_date=event_date,
                direction=direction,
                amount_cents=amount_cents,
                category=category,
                note=note,
                invoice_id=invoice_id,
                obligation_id=obligation_id,
                included_in_opening=included_in_opening,
            )
            current = balance(conn)
    return {
        'entry_id': entry_id,
        'added': added,
        'recorded_balance': current['balance'],
        'message': (
            'Cash record saved; no money was moved.'
            if added
            else 'That exact cash record already exists. Nothing was added.'
        ),
    }


def reverse_entry(entry_id, reference, reason):
    with connect() as conn:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            config = require_setup(conn)
            old = conn.execute(
                'SELECT * FROM cash_entries WHERE entry_id=?', (entry_id,)
            ).fetchone()
            if old is None or old['reversal_of'] is not None or old['loan_id'] is not None:
                raise ActionError(
                    'Choose an original cash record. Use cancel_recorded_loan for loan proceeds or fees.'
                )
            prior = conn.execute(
                'SELECT reference FROM cash_entries WHERE reversal_of=?', (entry_id,)
            ).fetchone()
            if prior:
                raise ActionError(
                    'This cash record was already reversed; nothing else can be subtracted.'
                )
            new_id, _ = _entry(
                conn,
                reference=reference,
                event_date=config['report_date'],
                direction='out' if old['direction'] == 'in' else 'in',
                amount_cents=old['amount_cents'],
                category='correction',
                note=reason,
                invoice_id=old['invoice_id'],
                obligation_id=old['obligation_id'],
                reversal_of=entry_id,
                included_in_opening=bool(old['included_in_opening']),
            )
    return {
        'entry_id': new_id,
        'message': 'Correction recorded as an opposite entry. The original remains in the history.',
    }


def advance_date(report_date):
    day = calendar_day(report_date)
    if day > date.max - timedelta(days=60):
        raise ActionError('Choose a reporting date with room for the next 60 forecast days.')
    with connect() as conn:
        with conn:
            config = require_setup(conn)
            if day < date.fromisoformat(config['report_date']):
                raise ActionError('The reporting date cannot move backwards.')
            conn.execute('UPDATE ledger_settings SET report_date=? WHERE id=1', (day.isoformat(),))
    return {
        'as_of': day.isoformat(),
        'message': 'Reporting date updated. Scheduled bills are not treated as paid until you record payment.',
    }


def month_date(first, offset):
    index = first.year * 12 + first.month - 1 + offset
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(first.day, monthrange(year, month)[1]))


def repayment_schedule(principal, annual_rate, months, first_payment_date, repayment):
    principal = cents(principal, positive=True)
    rate = Decimal(str(annual_rate))
    if not rate.is_finite() or not 0 <= rate <= 100:
        raise ActionError('Annual interest must be between 0 and 100 percent.')
    if (
        type(months) is not int
        or not 1 <= months <= 360
        or repayment not in ('amortizing', 'interest_only', 'bullet')
    ):
        raise ActionError('Use 1–360 months and a supported repayment structure.')
    first = calendar_day(first_payment_date)
    monthly = rate / Decimal(1200)
    level = (
        Decimal(principal) / months
        if not monthly
        else Decimal(principal) * monthly / (1 - (1 + monthly) ** (-months))
    )
    level = int(level.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    remaining = principal
    result = []
    accumulated = 0
    for i in range(months):
        interest = int((Decimal(remaining) * monthly).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        accumulated += interest
        if repayment == 'amortizing':
            capital = remaining if i == months - 1 else min(remaining, max(0, level - interest))
        elif repayment == 'interest_only':
            capital = remaining if i == months - 1 else 0
        else:
            capital = remaining if i == months - 1 else 0
            interest = accumulated if i == months - 1 else 0
        result.append(
            {
                'due_date': month_date(first, i).isoformat(),
                'principal_cents': capital,
                'interest_cents': interest,
            }
        )
        remaining -= capital
    return result


def record_loan(
    reference,
    principal,
    annual_rate,
    fees,
    disbursed_on,
    first_payment_date,
    months,
    repayment,
    included_in_opening,
):
    principal_cents, fee_cents = cents(principal, True), cents(fees)
    if fee_cents < 0 or fee_cents >= principal_cents:
        raise ActionError('Upfront fees must be nonnegative and smaller than principal.')
    start, first = calendar_day(disbursed_on), calendar_day(first_payment_date)
    if first <= start:
        raise ActionError('The first repayment must be after disbursement.')
    schedule = repayment_schedule(principal, annual_rate, months, first_payment_date, repayment)
    data = dict(
        reference=reference,
        principal_cents=principal_cents,
        annual_rate=str(Decimal(str(annual_rate))),
        fees_cents=fee_cents,
        disbursed_on=disbursed_on,
        first_payment_date=first_payment_date,
        months=months,
        repayment=repayment,
        included_in_opening=int(included_in_opening),
    )
    with connect() as conn:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            config = require_setup(conn)
            existing = conn.execute(
                'SELECT loan_id,payload FROM recorded_loans WHERE reference=?', (reference,)
            ).fetchone()
            if existing:
                if existing['payload'] != canonical(data):
                    raise ActionError('A different loan already uses that reference.')
                return {
                    'loan_id': existing['loan_id'],
                    'message': 'This loan was already recorded. No proceeds or repayments were added again.',
                }
            opening = date.fromisoformat(config['opening_date'])
            report = date.fromisoformat(config['report_date'])
            if start > report or (not included_in_opening and start < opening):
                raise ActionError(
                    'Record a received loan by the reporting date. Loans received before the opening balance must be marked included in that balance.'
                )
            if included_in_opening and start > opening:
                raise ActionError(
                    'A loan received after the opening balance cannot already be included in that balance.'
                )
            if first <= opening:
                raise ActionError(
                    'For older loans use the remaining principal and a new schedule starting after the opening balance; this form does not infer past repayments.'
                )
            keys = ','.join(data)
            loan_id = conn.execute(
                f"INSERT INTO recorded_loans ({keys},payload) VALUES ({','.join('?' for _ in data)},?)",
                (*data.values(), canonical(data)),
            ).lastrowid
            for i, row in enumerate(schedule):
                conn.execute(
                    'INSERT INTO loan_instalments VALUES (?,?,?,?,?)',
                    (
                        f'loan:{loan_id}:{i+1}',
                        loan_id,
                        row['due_date'],
                        row['principal_cents'],
                        row['interest_cents'],
                    ),
                )
            if not included_in_opening:
                _entry(
                    conn,
                    reference='loan-receipt:' + reference,
                    event_date=disbursed_on,
                    direction='in',
                    amount_cents=principal_cents,
                    category='loan_proceeds',
                    loan_id=loan_id,
                )
                if fee_cents:
                    _entry(
                        conn,
                        reference='loan-fee:' + reference,
                        event_date=disbursed_on,
                        direction='out',
                        amount_cents=fee_cents,
                        category='loan_fee',
                        loan_id=loan_id,
                    )
            current = balance(conn)
    return {
        'loan_id': loan_id,
        'recorded_balance': current['balance'],
        'message': 'Existing loan recorded, including its repayment schedule. No credit was requested and no money was moved.',
    }


def cancel_loan(loan_id, reference, reason):
    with connect() as conn:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            config = require_setup(conn)
            loan = conn.execute(
                'SELECT * FROM recorded_loans WHERE loan_id=?', (loan_id,)
            ).fetchone()
            if loan is None or loan['cancelled']:
                raise ActionError(
                    'Choose an active recorded loan. A cancelled record cannot be cancelled twice.'
                )
            settled = paid_obligations(conn, config['report_date'])
            if any(n > 0 for key, n in settled.items() if key.startswith(f'loan:{loan_id}:')):
                raise ActionError(
                    'Reverse recorded repayments before cancelling an incorrect loan record.'
                )
            conn.execute(
                'INSERT INTO loan_cancellations VALUES (?,?,?,?)',
                (reference, loan_id, reason, config['report_date']),
            )
            for old in conn.execute(
                'SELECT * FROM cash_entries WHERE loan_id=?', (loan_id,)
            ).fetchall():
                _entry(
                    conn,
                    reference=f'cancel:{reference}:{old["entry_id"]}',
                    event_date=config['report_date'],
                    direction='out' if old['direction'] == 'in' else 'in',
                    amount_cents=old['amount_cents'],
                    category='loan_correction',
                    note=reason,
                    loan_id=loan_id,
                    reversal_of=old['entry_id'],
                )
            conn.execute('UPDATE recorded_loans SET cancelled=1 WHERE loan_id=?', (loan_id,))
    return {
        'message': 'Incorrect loan record cancelled with reversing entries. Original records remain. This did not cancel a real bank loan.'
    }


def read_ledger(conn):
    current = balance(conn)
    entries = (
        [
            dict(r)
            for r in conn.execute('SELECT * FROM cash_entries ORDER BY entry_id DESC LIMIT 100')
        ]
        if settings(conn)
        else []
    )
    for entry in entries:
        entry['amount'] = money(entry.pop('amount_cents'))
        entry.pop('payload')
    loans = []
    if settings(conn):
        settled = paid_obligations(conn, current['as_of'])
        for row in conn.execute('SELECT * FROM recorded_loans ORDER BY loan_id DESC'):
            loan = dict(row)
            schedule = [
                dict(r)
                for r in conn.execute(
                    'SELECT * FROM loan_instalments WHERE loan_id=? ORDER BY due_date',
                    (loan['loan_id'],),
                )
            ]
            outstanding = loan['principal_cents']
            for item in schedule:
                paid = settled.get(item['obligation_id'], 0)
                outstanding -= max(0, min(item['principal_cents'], paid - item['interest_cents']))
            loan.update(
                principal=money(loan.pop('principal_cents')),
                fees=money(loan.pop('fees_cents')),
                outstanding_principal=money(outstanding),
                schedule=[
                    {
                        **r,
                        'principal': money(r['principal_cents']),
                        'interest': money(r['interest_cents']),
                        'total': money(r['principal_cents'] + r['interest_cents']),
                    }
                    for r in schedule
                ],
            )
            loan.pop('payload')
            loans.append(loan)
    bills = obligations(conn)
    report = current['as_of']
    return {
        'cash': current,
        'entries': entries,
        'loans': loans,
        'obligations': [
            r
            for r in bills
            if r['due_date'] <= (date.fromisoformat(report) + timedelta(days=60)).isoformat()
        ],
    }
