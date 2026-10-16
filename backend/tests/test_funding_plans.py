"""Small collection examples with known discount and borrowing costs."""

import unittest

from backend.funding_plans import collection_plans


PRODUCTS = {
    'early_discount': {'percent': 2},
    'overdraft': {'yearly_rate_percent': 16, 'fee_percent': 0, 'limit': 100000},
    'term_loan': {'yearly_rate_percent': 15, 'fee_percent': 1,
                  'min_amount': 5000, 'max_amount': 500000, 'term_months': 12},
}


class CollectionPlanTests(unittest.TestCase):
    invoice = {'invoice_id': 'I1', 'name': 'Cafe A', 'amount': 6000}

    def plans(self, amount=10000, products=PRODUCTS):
        return collection_plans([self.invoice], amount, 30, products, products['term_loan'])

    def test_discount_and_smaller_overdraft(self):
        plan = next(p for p in self.plans() if p['borrowing']['product'] == 'overdraft')
        self.assertEqual(plan['customers'], ['Cafe A'])
        self.assertEqual(plan['invoice_ids'], ['I1'])
        self.assertEqual(plan['discount_cost'], 120)
        self.assertEqual(plan['collection_net'], 5880)
        self.assertEqual(plan['remaining_gap'], 4120)
        self.assertEqual(plan['borrowing']['principal'], 4120)
        self.assertEqual(plan['borrowing']['interest'], 54.18)
        self.assertEqual(plan['total_cost'], 174.18)

    def test_minimum_loan_and_fee_are_not_hidden(self):
        plan = next(p for p in self.plans() if p['borrowing']['product'] == 'term_loan')
        self.assertEqual(plan['remaining_gap'], 4120)
        self.assertEqual(plan['borrowing']['principal'], 5000)
        self.assertEqual(plan['borrowing']['fee'], 50)
        self.assertGreaterEqual(5000 - 50, plan['remaining_gap'])
        self.assertEqual(plan['total_cost'], 120 + plan['borrowing']['cost'])

    def test_fee_is_funded_when_residual_exceeds_minimum(self):
        plan = next(p for p in self.plans(20000) if p['borrowing']['product'] == 'term_loan')
        loan = plan['borrowing']
        self.assertGreaterEqual(round(loan['principal'] - loan['fee'], 2), plan['remaining_gap'])

    def test_no_loan_when_discounted_receipt_covers_gap(self):
        plans = self.plans(4000)
        self.assertEqual(len(plans), 1)
        self.assertIsNone(plans[0]['borrowing'])
        self.assertEqual(plans[0]['total_cost'], 120)

    def test_empty_books_and_stable_plan_ids(self):
        self.assertEqual(collection_plans([], 10000, 30, PRODUCTS, PRODUCTS['term_loan']), [])
        self.assertEqual(self.plans(), self.plans())
        changed = {**PRODUCTS, 'early_discount': {'percent': 3}}
        self.assertNotEqual(self.plans()[0]['plan_id'], self.plans(products=changed)[0]['plan_id'])
