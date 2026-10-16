import unittest

from backend.funding_answers import collection_answer, preview_answer


class FundingAnswerTests(unittest.TestCase):
    plan = {'customers': ['Cafe A'], 'invoice_amount': 6000, 'discount_percent': 2,
            'discount_cost': 120, 'collection_net': 5880, 'remaining_gap': 4120,
            'borrowing': {'product': 'overdraft', 'principal': 4120, 'annual_rate': 16,
                          'cost': 54.18, 'interest': 54.18, 'fee': 0, 'days': 30},
            'total_cost': 174.18}

    def test_principal_is_distinct_from_borrowing_cost(self):
        text = collection_answer(self.plan)
        self.assertIn('Borrowing amount: GEL 4,120.00', text)
        self.assertIn('Borrowing cost: GEL 54.18', text)
        self.assertIn('Total discount and borrowing cost: GEL 174.18', text)

    def test_preview_uses_calculated_shortfalls_not_the_stated_gap(self):
        result = {'plan': self.plan, 'baseline': {'cautious': {'shortfall': 0}},
                  'with_plan': {'cautious': {'shortfall': 22, 'lowest_balance': -22, 'lowest_date': '2026-11-02'}},
                  'assumption': 'Conditional on customers paying today.'}
        text = preview_answer(result)
        self.assertIn('Original cautious shortfall: GEL 0.00', text)
        self.assertIn('Cautious shortfall with this plan: GEL 22.00', text)
        self.assertIn(result['assumption'], text)
