"""Hand-worked owner records, duplicate protection and remaining receivables."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from backend.bank import ledger
from backend.bank.db import connect, today
from backend.actions import registry
from backend.actions.bookkeeping import register_bookkeeping_actions
from backend.actions.lookups import get_balance, open_invoices
from backend.tests.helpers import BooksTestCase, invoice


class LedgerTests(BooksTestCase):
    rows = [invoice('I1', 'C1', 'Cafe', 'Tbilisi', '2026-09-01', '2026-10-01', 100)]

    def setup_cash(self):
        return ledger.initialize(1000, '2026-10-03')

    def cash(self):
        return get_balance()['balance']

    def test_opening_once_and_no_scheduled_cash_deduction(self):
        self.setup_cash()
        ledger.initialize(1000, '2026-10-03')
        with self.assertRaises(registry.ActionError):
            ledger.initialize(2000, '2026-10-03')
        ledger.advance_date('2026-10-20')
        self.assertEqual(self.cash(), 1000)
        self.assertEqual(today().isoformat(), '2026-10-20')
        with self.assertRaises(registry.ActionError):
            ledger.advance_date('2026-10-19')

    def test_partial_receipt_duplicate_overpayment_and_reversal(self):
        self.setup_cash()
        args = dict(
            reference='receipt1',
            event_date='2026-10-03',
            direction='in',
            amount=40,
            category='invoice',
            invoice_id='I1',
        )
        result = ledger.record_cash(**args)
        self.assertTrue(result['added'])
        self.assertFalse(ledger.record_cash(**args)['added'])
        self.assertEqual(self.cash(), 1040)
        with connect() as conn:
            self.assertEqual(open_invoices(conn, today())[0]['amount'], 60)
        with self.assertRaises(registry.ActionError):
            ledger.record_cash(**{**args, 'reference': 'too much', 'amount': 61})
        with self.assertRaises(registry.ActionError):
            ledger.record_cash(**{**args, 'amount': 41})
        ledger.reverse_entry(result['entry_id'], 'undo1', 'Incorrect receipt')
        self.assertEqual(self.cash(), 1000)
        with connect() as conn:
            self.assertEqual(open_invoices(conn, today())[0]['amount'], 100)
        with self.assertRaises(registry.ActionError):
            ledger.reverse_entry(result['entry_id'], 'undo2', 'Again')

    def test_a_date_written_another_way_is_refused_not_stored(self):
        # Python reads 20261003 as 3 October, but these tables compare dates as
        # text: stored like that, a receipt never counted and its reversal did.
        others = ('20261003', '2026-W40-6', '2026-10-3', '2026-02-30', 20261003)
        for other in others:
            with self.assertRaisesRegex(registry.ActionError, 'YYYY-MM-DD'):
                ledger.initialize(1000, other)
        self.setup_cash()
        for other in others:
            with self.assertRaisesRegex(registry.ActionError, 'YYYY-MM-DD'):
                ledger.record_cash(f'r-{other}', other, 'in', 40, 'invoice', invoice_id='I1')
            with self.assertRaisesRegex(registry.ActionError, 'YYYY-MM-DD'):
                ledger.record_loan(f'l-{other}', 500, 12, 0, other, '2026-11-03', 2, 'amortizing', False)
            with self.assertRaisesRegex(registry.ActionError, 'YYYY-MM-DD'):
                ledger.record_loan(f'm-{other}', 500, 12, 0, '2026-10-03', other, 2, 'amortizing', False)
            with self.assertRaisesRegex(registry.ActionError, 'YYYY-MM-DD'):
                ledger.advance_date(other)
        self.assertEqual(self.cash(), 1000)
        self.assertEqual(today().isoformat(), '2026-10-03')
        with connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM cash_entries').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM recorded_loans').fetchone()[0], 0)

    def test_bill_is_told_the_date_form_before_the_owner_is_asked(self):
        self.setup_cash()
        with patch.dict(registry._actions, {}, clear=True):
            register_bookkeeping_actions()
            reply = registry.execute('record_cash_transaction', {
                'reference': 'r1', 'event_date': '20261003', 'direction': 'in',
                'amount': 40, 'category': 'invoice', 'invoice_id': 'I1'}, 'owner')
        self.assertEqual(reply['status'], 'error')
        self.assertIn('event_date', reply['message'])
        self.assertIn('YYYY-MM-DD', reply['message'])
        self.assertEqual(registry.waiting_requests()['items'], [])

    def test_concurrent_duplicate_receipt_adds_once(self):
        self.setup_cash()

        def receive(_):
            return ledger.record_cash('same', '2026-10-03', 'in', 20, 'invoice', invoice_id='I1')

        with ThreadPoolExecutor(2) as pool:
            result = list(pool.map(receive, range(2)))
        self.assertEqual(sum(r['added'] for r in result), 1)
        self.assertEqual(self.cash(), 1020)

    def test_loan_proceeds_fees_schedule_and_actual_repayment(self):
        self.setup_cash()
        args = dict(
            reference='contract1',
            principal=1200,
            annual_rate=12,
            fees=12,
            disbursed_on='2026-10-03',
            first_payment_date='2026-11-03',
            months=12,
            repayment='interest_only',
            included_in_opening=False,
        )
        loan = ledger.record_loan(**args)
        ledger.record_loan(**args)
        self.assertEqual(self.cash(), 2188)
        with connect() as conn:
            view = ledger.read_ledger(conn)
            self.assertEqual(view['loans'][0]['schedule'][0]['total'], 12)
            self.assertEqual(view['loans'][0]['schedule'][-1]['total'], 1212)
        ledger.advance_date('2026-11-03')
        ledger.record_cash(
            'repay1', '2026-11-03', 'out', 12, 'loan', obligation_id=f'loan:{loan["loan_id"]}:1'
        )
        self.assertEqual(self.cash(), 2176)
        with connect() as conn:
            self.assertFalse(
                any(
                    r['obligation_id'] == f'loan:{loan["loan_id"]}:1'
                    for r in ledger.obligations(conn)
                )
            )
            self.assertEqual(ledger.read_ledger(conn)['loans'][0]['outstanding_principal'], 1200)
        with self.assertRaises(registry.ActionError):
            ledger.cancel_loan(loan['loan_id'], 'cancel', 'wrong')

    def test_old_loan_does_not_add_proceeds_again(self):
        self.setup_cash()
        ledger.record_loan('old', 1200, 0, 0, '2026-09-01', '2026-11-01', 12, 'amortizing', True)
        self.assertEqual(self.cash(), 1000)
        with connect() as conn:
            schedule = ledger.read_ledger(conn)['loans'][0]['schedule']
        self.assertEqual(sum(r['principal'] for r in schedule), 1200)
        self.assertEqual(schedule[0]['total'], 100)

    def test_cancel_wrong_loan_reverses_net_cash(self):
        self.setup_cash()
        loan = ledger.record_loan(
            'new', 1000, 10, 10, '2026-10-03', '2026-11-03', 12, 'bullet', False
        )
        self.assertEqual(self.cash(), 1990)
        ledger.cancel_loan(loan['loan_id'], 'cancel-new', 'Wrong amount')
        self.assertEqual(self.cash(), 1000)
        with connect() as conn:
            self.assertFalse(any(r['category'] == 'loan' for r in ledger.obligations(conn)))

    def test_month_end_and_last_payment_rounding(self):
        rows = ledger.repayment_schedule(1000, 15, 3, '2026-01-31', 'amortizing')
        self.assertEqual([r['due_date'] for r in rows], ['2026-01-31', '2026-02-28', '2026-03-31'])
        self.assertEqual(sum(r['principal_cents'] for r in rows), 100000)
        self.assertTrue(all(r['principal_cents'] >= 0 for r in rows))

    def test_reseed_cannot_destroy_owner_records(self):
        self.setup_cash()
        with self.assertRaisesRegex(ValueError, 'Owner cash records'):
            self.run_seed()
        self.assertEqual(self.cash(), 1000)

    def test_actual_payment_requires_setup_and_observed_date(self):
        with self.assertRaises(registry.ActionError):
            ledger.record_cash('x', '2026-10-03', 'in', 1, 'other')
        self.setup_cash()
        with self.assertRaises(registry.ActionError):
            ledger.record_cash('x', '2026-10-04', 'in', 1, 'other')
        with self.assertRaises(registry.ActionError):
            ledger.record_cash('x', '2026-10-02', 'in', 1, 'other')

    def test_yellow_setup_only_changes_cash_after_approval(self):
        with patch.dict(registry._actions, {}, clear=True):
            register_bookkeeping_actions()
            result = registry.execute(
                'set_opening_balance', {'amount': 500, 'opening_date': '2026-10-03'}, 'owner'
            )
            self.assertEqual(result['status'], 'needs_confirmation')
            self.assertEqual(self.cash(), 1000)
            self.assertEqual(
                registry.confirm(result['confirmation_id'], True, 'other')['status'], 'error'
            )
            self.assertEqual(
                registry.confirm(result['confirmation_id'], True, 'owner')['status'], 'ok'
            )
            self.assertEqual(self.cash(), 500)
            self.assertEqual(
                registry.confirm(result['confirmation_id'], True, 'owner')['status'], 'error'
            )

    def test_reconciled_earlier_receipt_does_not_increase_opening_cash(self):
        ledger.initialize(1000, '2026-10-15')
        item = ledger.record_cash(
            'already-cashed',
            '2026-10-10',
            'in',
            100,
            'invoice',
            invoice_id='I1',
            included_in_opening=True,
        )
        self.assertEqual(self.cash(), 1000)
        with connect() as conn:
            self.assertEqual(open_invoices(conn, today()), [])
        ledger.reverse_entry(item['entry_id'], 'undo-reconciliation', 'Wrong invoice')
        self.assertEqual(self.cash(), 1000)
        with connect() as conn:
            self.assertEqual(open_invoices(conn, today())[0]['amount'], 100)

    def test_partial_obligation_settlement_only_leaves_remaining_bill(self):
        self.setup_cash()
        ledger.advance_date('2026-10-10')
        ledger.record_cash(
            'payroll-part', '2026-10-10', 'out', 20, 'salary', obligation_id='scheduled:1'
        )
        self.assertEqual(self.cash(), 980)
        with connect() as conn:
            bill = next(r for r in ledger.obligations(conn) if r['obligation_id'] == 'scheduled:1')
            self.assertEqual(bill['amount'], 30)

    def test_new_horizon_keeps_recurring_bills_after_imported_window(self):
        self.setup_cash()
        ledger.advance_date('2026-12-03')
        with connect() as conn:
            future = [r for r in ledger.obligations(conn) if r['due_date'] == '2026-12-10']
        self.assertEqual(len(future), 1)
        self.assertEqual(future[0]['amount'], 50)

    def test_extended_month_end_bill_is_not_lost_in_february(self):
        import json

        self.setup_cash()
        with connect() as conn:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='monthly_payments_json'",
                (
                    json.dumps(
                        [{'category': 'rent', 'label': 'Rent', 'amount': 75, 'days_of_month': [31]}]
                    ),
                ),
            )
            conn.commit()
        ledger.advance_date('2027-02-01')
        with connect() as conn:
            bills = [r for r in ledger.obligations(conn) if r['due_date'] == '2027-02-28']
        self.assertEqual(len(bills), 1)
        self.assertEqual(bills[0]['amount'], 75)
