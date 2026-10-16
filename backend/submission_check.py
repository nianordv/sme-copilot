"""Exercise the real bookkeeping/API/forecast on freshly seeded temporary books.

Run: backend/.venv/bin/python -m backend.submission_check
No real credentials, bank access, training or production database writes.
"""

import json
import os
from pathlib import Path
import secrets
import tempfile
from datetime import date, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend.bank.seed import seed, DEFAULT_CSV, DEFAULT_METADATA, DEFAULT_COMPANY
from backend.actions import registry
from backend.bank.db import connect
from backend import main


def run():
    with tempfile.TemporaryDirectory() as folder:
        database = Path(folder) / 'submission.db'
        seed(DEFAULT_CSV, DEFAULT_METADATA, DEFAULT_COMPANY, database)
        pin = secrets.token_urlsafe(16)
        with patch.dict(os.environ, {'RIGHTSIZE_DB': str(database), 'RIGHTSIZE_OWNER_PIN': pin}):
            client = TestClient(main.app, base_url='http://localhost')
            assert client.get('/workspace').status_code == 401
            assert client.post('/login', json={'pin': pin}).status_code == 200

            def approve(name, arguments):
                waiting = client.post(
                    '/bookkeeping',
                    json={'action': name, 'arguments': arguments, 'session_id': 'submission-check'},
                ).json()
                assert waiting['status'] == 'needs_confirmation', waiting
                result = client.post(
                    '/confirm',
                    json={
                        'confirmation_id': waiting['confirmation_id'],
                        'approved': True,
                        'session_id': 'submission-check',
                    },
                ).json()
                assert result['status'] == 'ok', result
                return result['result']

            with connect() as conn:
                base = date.fromisoformat(
                    conn.execute("SELECT value FROM meta WHERE key='snapshot_date'").fetchone()[0]
                )
                inv = conn.execute(
                    'SELECT invoice_id,amount FROM invoices WHERE paid_date IS NULL AND writeoff_date IS NULL ORDER BY amount DESC LIMIT 1'
                ).fetchone()
            approve('set_opening_balance', {'amount': 1000, 'opening_date': str(base)})
            received = min(100, inv['amount'])
            receipt = {
                'reference': 'receipt-check',
                'event_date': str(base),
                'direction': 'in',
                'amount': received,
                'category': 'invoice',
                'invoice_id': inv['invoice_id'],
            }
            approve('record_cash_transaction', receipt)
            approve('record_cash_transaction', receipt)  # Idempotent duplicate.
            first = base + timedelta(days=31)
            approve(
                'record_existing_loan',
                {
                    'reference': 'loan-check',
                    'principal': 12000,
                    'annual_rate': 12,
                    'fees': 120,
                    'disbursed_on': str(base),
                    'first_payment_date': str(first),
                    'months': 3,
                    'repayment': 'interest_only',
                    'included_in_opening': False,
                },
            )
            page = client.get('/dashboard').json()
            assert page['forecast']['available'], page['forecast']
            assert page['cash']['balance']['balance'] == round(1000 + received + 11880, 2)
            with connect() as conn:
                total = conn.execute('SELECT COUNT(*) FROM cash_entries').fetchone()[0]
            assert total == 3, total
            approve('set_reporting_date', {'report_date': str(base + timedelta(days=12))})
            future = client.get('/dashboard?days=60').json()
            assert future['forecast']['available'], future['forecast']
            assert future['forecast']['result']['as_of'] == str(base + timedelta(days=12))
            assert (
                future['forecast']['result']['opening_balance']
                == page['cash']['balance']['balance']
            )
            records = client.get('/workspace').json()
            assert records['payments']['available'], records['payments']
            assert records['ledger']['loans'][0]['outstanding_principal'] == 12000
            options = client.post('/funding-comparison', json={'amount': 8000, 'days': 21}).json()
            assert any(r['option'] == 'early_payment_discount' for r in options['options'])
            assert registry.verify_log()['intact']
            assert client.post('/logout').status_code == 200
            assert client.get('/dashboard').status_code == 401
            return {
                'result': 'passed',
                'checks': [
                    'authentication',
                    'opening cash',
                    'partial receipt',
                    'duplicate protection',
                    'existing loan and fees',
                    '30/60-day forecasts',
                    'fresh model scoring at a later date',
                    'invoice probabilities',
                    'discount comparison',
                    'audit integrity',
                    'logout',
                ],
                'reporting_date': future['forecast']['result']['as_of'],
                'predictor': future['forecast']['result']['predictor'],
                'recorded_cash': page['cash']['balance']['balance'],
            }


if __name__ == '__main__':
    print(json.dumps(run(), indent=2))
