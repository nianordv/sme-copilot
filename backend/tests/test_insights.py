"""Risk and discount checks: figures come from the books, not the model."""

import json
import os
from unittest.mock import patch

from backend.tests.helpers import BooksTestCase, invoice
from backend.bank.db import connect
from backend.bank import ledger
from backend.insights import risk_summary
from backend.actions.borrowing import compare_options


class InsightTests(BooksTestCase):
    rows = [invoice('I1', 'C1', 'Cafe', 'Tbilisi', '2026-09-01', '2026-10-01', 10000)]

    def test_discount_applies_to_whole_invoice_and_is_contingent(self):
        path = self.folder / 'products.json'
        path.write_text(
            json.dumps(
                {
                    'placeholder': True,
                    'overdraft': {
                        'name': 'OD',
                        'yearly_rate_percent': 16,
                        'fee_percent': 0,
                        'limit': 100000,
                    },
                    'term_loan': {
                        'name': 'Loan',
                        'yearly_rate_percent': 15,
                        'fee_percent': 1,
                        'term_months': 12,
                        'min_amount': 5000,
                        'max_amount': 500000,
                    },
                    'early_discount': {'percent': 2},
                }
            )
        )
        with patch.dict(os.environ, {'RIGHTSIZE_PRODUCTS': str(path)}):
            result = compare_options(8000, 21)
        discount = next(r for r in result['options'] if r['option'] == 'early_payment_discount')
        self.assertEqual(discount['cost'], 200)
        self.assertEqual(discount['potential_net_receipts'], 9800)
        self.assertTrue(discount['covers_amount'])
        self.assertFalse(discount['certain'])
        self.assertAlmostEqual(discount['yearly_rate_percent'], 35.47, places=2)
        self.assertTrue(
            next(r for r in result['options'] if r['option'] == 'overdraft')['subject_to_approval']
        )
        self.assertIn(
            'consequences',
            next(r for r in result['options'] if r['option'] == 'wait')['cost_basis'],
        )

    def test_concentration_updates_after_partial_receipt(self):
        ledger.initialize(1000, '2026-10-03')
        ledger.record_cash('r1', '2026-10-03', 'in', 1000, 'invoice', invoice_id='I1')
        with connect() as conn:
            result = risk_summary(conn)
        self.assertEqual(result['outstanding_amount'], 9000)
        self.assertEqual(result['top_customers'][0]['share_percent'], 100)
        self.assertEqual(result['concentration_threshold_percent'], 25)

    def test_empty_receivables_have_no_concentration_division_error(self):
        ledger.initialize(1000, '2026-10-03')
        ledger.record_cash('full', '2026-10-03', 'in', 10000, 'invoice', invoice_id='I1')
        with connect() as conn:
            result = risk_summary(conn)
        self.assertEqual(result['top_customers'], [])
        self.assertEqual(result['outstanding_amount'], 0)
