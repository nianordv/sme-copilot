"""Submission flows with hand-made invoices and deterministic payment curves."""

from unittest.mock import patch
from fastapi.testclient import TestClient

from backend import main
from backend.actions import registry
from backend.actions.forecast import forecast_cash
from backend.bank import ledger
from backend.bank.db import connect
from backend.insights import risk_summary, evaluate_alerts
from backend.tests import test_cash_forecast as cash_tests
from backend.tests.helpers import sign_in_as_owner


class SubmissionFlows(cash_tests.CurveBooks):
    def setUp(self):
        super().setUp()
        self.client = TestClient(main.app, base_url='http://localhost')
        sign_in_as_owner(self, self.client)

    def approve(self, action, arguments):
        response = self.client.post(
            '/bookkeeping',
            json={'action': action, 'arguments': arguments, 'session_id': 'submission'},
        )
        self.assertEqual(response.status_code, 200, response.text)
        pending = response.json()
        self.assertEqual(pending['status'], 'needs_confirmation', pending)
        result = self.client.post(
            '/confirm',
            json={
                'confirmation_id': pending['confirmation_id'],
                'approved': True,
                'session_id': 'submission',
            },
        ).json()
        self.assertEqual(result['status'], 'ok', result)
        return result['result']

    def test_payment_selector_shows_remaining_invoice_balance(self):
        self.approve('set_opening_balance', {'amount': 1000, 'opening_date': '2026-10-03'})
        self.approve('record_cash_transaction', {'reference': 'partial', 'event_date': '2026-10-03',
                     'direction': 'in', 'category': 'invoice', 'amount': 40, 'invoice_id': 'I1'})
        response = self.client.get('/payment-record-options', params={
            'kind': 'invoice', 'payment_date': '2026-10-03', 'query': 'I1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.json()['total'], 1)
        self.assertEqual(response.json()['invoices'][0]['amount'], 60)
        self.assertEqual(response.json()['bills'], [])
        empty = self.client.get('/payment-record-options', params={
            'kind': 'invoice', 'payment_date': '2026-08-01'}).json()
        self.assertEqual(empty['total'], 0)

    def test_payment_selector_is_protected_and_validates_kind(self):
        anonymous = TestClient(main.app, base_url='http://localhost')
        self.assertEqual(anonymous.get('/payment-record-options', params={
            'kind': 'invoice', 'payment_date': '2026-10-03'}).status_code, 401)
        self.assertEqual(self.client.get('/payment-record-options', params={
            'kind': 'unknown', 'payment_date': '2026-10-03'}).status_code, 422)

    def test_bill_selector_keeps_loan_repayments_separate(self):
        self.approve('set_opening_balance', {'amount': 1000, 'opening_date': '2026-10-03'})
        loan = self.approve('record_existing_loan', {'reference': 'loan-select', 'principal': 1200,
            'annual_rate': 12, 'fees': 0, 'disbursed_on': '2026-10-03',
            'first_payment_date': '2026-11-03', 'months': 12, 'repayment': 'interest_only',
            'included_in_opening': False})
        params = {'kind': 'loan', 'payment_date': '2026-10-03'}
        rows = self.client.get('/payment-record-options', params=params).json()['bills']
        self.assertEqual(len(rows), 12)
        self.assertTrue(all(row['obligation_id'].startswith(f'loan:{loan["loan_id"]}:') for row in rows))
        params['kind'] = 'expense'
        bills = self.client.get('/payment-record-options', params=params).json()['bills']
        self.assertTrue(all(row['category'] != 'loan' for row in bills))

    def test_opening_payment_loan_and_forecast_use_same_cash(self):
        self.approve('set_opening_balance', {'amount': 1000, 'opening_date': '2026-10-03'})
        self.approve(
            'record_cash_transaction',
            {
                'reference': 'receipt',
                'event_date': '2026-10-03',
                'direction': 'in',
                'amount': 40,
                'category': 'invoice',
                'invoice_id': 'I1',
            },
        )
        loan = self.approve(
            'record_existing_loan',
            {
                'reference': 'contract',
                'principal': 1200,
                'annual_rate': 12,
                'fees': 12,
                'disbursed_on': '2026-10-03',
                'first_payment_date': '2026-11-03',
                'months': 12,
                'repayment': 'interest_only',
                'included_in_opening': False,
            },
        )
        forecast = forecast_cash(days=30)
        self.assertEqual(forecast['opening_balance'], 2228)
        self.assertEqual(forecast['outstanding_invoices'], 3)
        self.assertEqual(forecast['expected_open_invoice_receipts'], 260)
        self.assertEqual(forecast['likely']['weekly_balances'][-1]['balance'], 2338)
        # Day 31 adds the first loan interest bill. Curve refresh is mocked to
        # test cash arithmetic independently of the already evaluated ML model.
        with patch(
            'backend.actions.forecast.current_curves',
            return_value=(self.curves, {'model_used': 'test', 'fallback': False}),
        ):
            self.approve('set_reporting_date', {'report_date': '2026-11-03'})
            before = forecast_cash(days=7)
            self.assertEqual(before['overdue_obligations'], 162)
            self.approve(
                'record_cash_transaction',
                {
                    'reference': 'interest',
                    'event_date': '2026-11-03',
                    'direction': 'out',
                    'amount': 12,
                    'category': 'loan',
                    'obligation_id': f'loan:{loan["loan_id"]}:1',
                },
            )
            after = forecast_cash(days=7)
        self.assertEqual(before['opening_balance'] - after['opening_balance'], 12)
        self.assertEqual(before['overdue_obligations'] - after['overdue_obligations'], 12)
        self.assertEqual(
            before['likely']['weekly_balances'][-1]['balance'],
            after['likely']['weekly_balances'][-1]['balance'],
        )
        self.assertTrue(registry.verify_log()['intact'])

    def test_full_receipt_removes_invoice_without_breaking_base_forecast(self):
        ledger.initialize(1000, '2026-10-03')
        ledger.record_cash('full', '2026-10-03', 'in', 100, 'invoice', invoice_id='I1')
        result = forecast_cash(days=14)
        self.assertEqual(result['outstanding_invoices'], 2)
        self.assertEqual(result['opening_balance'], 1100)
        self.assertEqual(result['expected_open_invoice_receipts'], 200)
        self.assertEqual(result['likely']['weekly_balances'][-1]['balance'], 1220)

    def test_new_endpoints_require_login_and_same_origin(self):
        stranger = TestClient(main.app, base_url='http://localhost')
        for path in ('/workspace', '/dashboard?days=60'):
            self.assertEqual(stranger.get(path).status_code, 401)
        self.assertEqual(stranger.post('/bookkeeping', json={}).status_code, 401)
        self.assertEqual(
            stranger.post('/funding-comparison', json={'amount': 100, 'days': 10}).status_code, 401
        )
        self.assertEqual(
            self.client.post(
                '/bookkeeping', json={}, headers={'Origin': 'https://evil.example'}
            ).status_code,
            403,
        )

    def test_concentration_and_alerts_are_explicit_and_read_only(self):
        with connect() as conn:
            before = conn.total_changes
            risk = risk_summary(conn)
            self.assertEqual(risk['top_customers'][0]['share_percent'], 50)
            self.assertTrue(risk['top_customers'][0]['concentration_warning'])
            self.assertEqual(conn.total_changes, before)
        self.approve('set_opening_balance', {'amount': 10, 'opening_date': '2026-10-03'})
        # Existing Yellow alert action still requires approval and no notification
        # service is implied by this refresh-time evaluation.
        pending = registry.execute('set_alert', {'balance_below': 20}, 'submission')
        self.assertEqual(
            registry.confirm(pending['confirmation_id'], True, 'submission')['status'], 'ok'
        )
        with connect() as conn:
            rows = evaluate_alerts(conn, forecast_cash(days=14))
        self.assertTrue(rows[0]['triggered'])
