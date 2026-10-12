"""Hand-worked cash cases and integration with books, curves and permissions."""

import csv
import hashlib
import json
from datetime import date
from unittest.mock import patch

import numpy as np

from backend.actions import registry
from backend.actions.forecast import forecast_cash, register_forecast_action
from backend.forecast.cash import calculate
from backend.forecast.inputs import future_sales
from backend.bank.db import connect
from backend.tests.helpers import BooksTestCase, invoice


class CashForecastTests(BooksTestCase):
    rows = [
        invoice('I1', 'C1', 'Cafe', 'Tbilisi', '2026-09-01', '2026-10-01', 100),
        invoice('I2', 'C2', 'Cafe', 'Tbilisi', '2026-09-01', '2026-10-01', 200),
        invoice('I3', 'C3', 'Cafe', 'Tbilisi', '2026-09-01', '2026-10-01', 300),
    ]

    def setUp(self):
        super().setUp()
        self.curves = np.zeros((3, 60))
        self.curves[0, :] = 1
        self.curves[1, 1:] = 1
        # Third invoice never pays within the horizon.
        self.csv_path = self.folder / 'open_invoice_curves.csv'
        with self.csv_path.open('w', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['invoice_id', 'client_id', 'amount', *[f'day_{d}' for d in range(1, 61)]])
            for index, row in enumerate(self.rows):
                writer.writerow([row[0], row[1], row[9], *self.curves[index]])
        self.forecast_metadata = {'snapshot_date': '2026-10-03', 'source_csv_sha256': hashlib.sha256(self.csv.read_bytes()).hexdigest(),
                                 'currency': 'GEL', 'horizon_days': 60, 'invoice_count': 3,
                                 'forecast_csv_sha256': hashlib.sha256(self.csv_path.read_bytes()).hexdigest(),
                                 'model_used': 'test', 'fallback': False}
        self.save_metadata()
        location = patch('backend.actions.forecast.FORECAST', self.folder)
        location.start()
        self.addCleanup(location.stop)

    def save_metadata(self):
        (self.folder / 'metadata.json').write_text(json.dumps(self.forecast_metadata))

    def test_three_invoice_cash_arithmetic(self):
        expenses = [{'date': date(2026, 10, 5), 'amount': 400, 'label': 'Payroll', 'category': 'salaries'}]
        result = calculate(date(2026, 10, 3), 3, 50, [100, 200, 300], self.curves, expenses, [0, 0, 0])
        self.assertEqual(result['likely']['lowest_balance'], -50)
        self.assertEqual(result['likely']['first_day_below_zero'], '2026-10-05')
        self.assertEqual(result['likely']['days_below_zero'], 2)
        self.assertEqual(result['cautious'], result['likely'])
        self.assertEqual(result['worst']['shortfall'], 350)
        self.assertEqual(result['likely']['obligation_balances'][0]['balance'], -50)

    def test_assumed_paid_is_added_once_even_in_stress_case(self):
        result = calculate(date(2026, 10, 3), 3, 50, [100, 200, 300], self.curves, [], [0, 0, 0], [0])
        self.assertEqual(result['likely']['weekly_balances'][-1]['balance'], 350)
        self.assertEqual(result['worst']['weekly_balances'][-1]['balance'], 150)

    def test_same_call_is_reproducible_and_quantiles_are_ordered(self):
        curves = np.tile(np.linspace(0.01, 0.8, 60), (3, 1))
        arguments = (date(2026, 10, 3), 30, 50, [100, 200, 300], curves, [], [0] * 30)
        first = calculate(*arguments)
        self.assertEqual(first, calculate(*arguments))
        self.assertGreaterEqual(first['likely']['weekly_balances'][-1]['balance'], first['cautious']['weekly_balances'][-1]['balance'])

    def test_database_and_forecast_action(self):
        result = forecast_cash(days=14)
        self.assertEqual(result['opening_balance'], 1000)
        self.assertEqual(result['scheduled_and_extra_outflows'], 80)
        self.assertEqual(result['likely']['weekly_balances'][-1]['balance'], 1220)
        self.assertEqual(len(result['likely']['obligation_balances']), 2)
        self.assertLess(len(json.dumps(result)), 8000)
        self.assertEqual(result, forecast_cash(days=14))

    def test_what_if_outflows_and_invoice_assumptions_do_not_change_books(self):
        result = forecast_cash(days=14, extra_outflows=[{'date': '2026-10-04', 'amount': 1500, 'label': 'Purchase'}],
                               assume_paid={'invoices': ['I3']})
        self.assertEqual(result['assumed_paid_today']['amount'], 300)
        self.assertEqual(result['likely']['shortfall'], 100)
        self.assertEqual(self.query('SELECT COUNT(*) FROM invoices WHERE paid_date IS NOT NULL')[0][0], 0)

    def test_unknown_invoice_and_bad_nested_arguments_are_rejected(self):
        for arguments in ({'days': 61}, {'days': True}, {'assume_paid': {'invoices': ['missing']}},
                          {'extra_outflows': [{'date': '2026-10-03', 'amount': 2, 'label': 'Old'}]},
                          {'extra_outflows': [{'date': '2026-10-05', 'amount': -2, 'label': 'Bad'}]},
                          {'assume_paid': {'unexpected': True}}):
            with self.assertRaises(ValueError):
                forecast_cash(**arguments)

    def test_stale_or_changed_forecast_is_rejected(self):
        self.forecast_metadata['snapshot_date'] = '2026-10-02'
        self.save_metadata()
        with self.assertRaises(ValueError):
            forecast_cash()
        self.forecast_metadata['snapshot_date'] = '2026-10-03'
        self.save_metadata()
        self.csv_path.write_text('changed')
        with self.assertRaises(ValueError):
            forecast_cash()

    def test_registry_logs_green_forecast_and_rejects_bad_nested_input(self):
        with patch.dict(registry._actions, {}, clear=True):
            register_forecast_action()
            self.assertEqual(registry.execute('forecast_cash', {'days': 14}, 'forecast-test')['status'], 'ok')
            self.assertEqual(registry.execute('forecast_cash', {'extra_outflows': [{'amount': -1}]}, 'forecast-test')['status'], 'error')
        self.assertTrue(registry.audit_log('forecast-test'))

    def test_ambiguous_customer_is_not_assumed_paid(self):
        self.query("UPDATE customers SET name='Cafe One' WHERE customer_id='C1'")
        self.query("UPDATE customers SET name='Cafe Two' WHERE customer_id='C2'")
        result = forecast_cash(assume_paid={'customers': ['Cafe']})
        self.assertFalse(result['found'])
        self.assertEqual(result['candidates_total'], 2)

    def test_customer_assumption_includes_all_its_open_invoices(self):
        result = forecast_cash(days=14, assume_paid={'customers': ['C3'], 'invoices': ['I3']})
        self.assertEqual(result['assumed_paid_today'], {'invoice_count': 1, 'amount': 300})
        self.assertEqual(result['likely']['weekly_balances'][-1]['balance'], 1520)

    def test_empty_invoice_cohort_and_no_outflows(self):
        result = calculate(date(2026, 10, 3), 1, 50, [], np.empty((0, 60)), [], [0])
        self.assertEqual(result['likely']['lowest_balance'], 50)
        self.assertEqual(result['worst']['shortfall'], 0)

    def test_future_sales_preserves_unpaid_probability_mass(self):
        self.query("UPDATE invoices SET issue_date='2026-07-01', paid_date='2026-07-03' WHERE invoice_id='I1'")
        self.query("UPDATE invoices SET issue_date='2026-07-01' WHERE invoice_id='I2'")
        with connect() as conn:
            receipts, assumption = future_sales(conn, date(2026, 10, 3), 3)
        self.assertEqual(receipts[:2], [0, 0])
        self.assertAlmostEqual(assumption['historical_paid_within_60d_share'], 1 / 3, places=4)
        self.assertGreater(receipts[2], 0)
