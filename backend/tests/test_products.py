"""The cost arithmetic, checked against examples worked by hand."""

import json
import unittest

from backend.bank.products import DEFAULT_PRODUCTS, loan_cost, overdraft_cost

OVERDRAFT = {"yearly_rate_percent": 16.0, "fee_percent": 0.0, "limit": 15000.0}
LOAN = {"yearly_rate_percent": 15.0, "fee_percent": 1.0, "term_months": 12}


class OverdraftCostTests(unittest.TestCase):
    def test_the_brief_example_is_about_74(self):
        # 8,000 x 0.16 x 21 / 365 = 73.6438...
        cost = overdraft_cost(8000, 21, OVERDRAFT)
        self.assertEqual(cost, {"interest": 73.64, "fee": 0.0, "total": 73.64})

    def test_one_day_and_one_year(self):
        # 8,000 x 0.16 / 365 = 3.5068... and a full year is 16% of 8,000.
        self.assertEqual(overdraft_cost(8000, 1, OVERDRAFT)["total"], 3.51)
        self.assertEqual(overdraft_cost(8000, 365, OVERDRAFT)["total"], 1280.00)

    def test_cost_grows_in_step_with_amount_and_days(self):
        self.assertEqual(overdraft_cost(10000, 73, OVERDRAFT)["total"], 320.00)   # 10,000 x 0.16 x 0.2
        self.assertEqual(overdraft_cost(20000, 73, OVERDRAFT)["total"], 640.00)

    def test_fee_is_added_when_there_is_one(self):
        # 0.5% of 8,000 is 40.
        cost = overdraft_cost(8000, 21, {**OVERDRAFT, "fee_percent": 0.5})
        self.assertEqual(cost, {"interest": 73.64, "fee": 40.00, "total": 113.64})


class LoanCostTests(unittest.TestCase):
    def test_the_brief_example_is_about_2800(self):
        # r = 0.15 / 12 = 0.0125. Payment = 30,000 x r / (1 - 1.0125^-12) = 2,707.75.
        # Interest = 12 x 2,707.75 - 30,000 = 2,493.00. Fee = 1% of 30,000 = 300.00.
        cost = loan_cost(30000, LOAN)
        self.assertEqual(cost, {
            "monthly_payment": 2707.75, "interest": 2493.00, "fee": 300.00, "total": 2793.00,
        })

    def test_a_second_hand_worked_loan(self):
        # 12,000 at 12% a year: r = 0.01. Payment = 120 / (1 - 1.01^-12) = 1,066.19.
        # Interest = 12 x 1,066.19 - 12,000 = 794.28. No fee.
        cost = loan_cost(12000, {"yearly_rate_percent": 12.0, "fee_percent": 0.0, "term_months": 12})
        self.assertEqual(cost, {
            "monthly_payment": 1066.19, "interest": 794.28, "fee": 0.0, "total": 794.28,
        })

    def test_no_interest_means_equal_parts_and_only_the_fee(self):
        cost = loan_cost(12000, {"yearly_rate_percent": 0.0, "fee_percent": 1.0, "term_months": 12})
        self.assertEqual(cost, {"monthly_payment": 1000.00, "interest": 0.0, "fee": 120.00, "total": 120.00})

    def test_a_loan_costs_far_more_than_a_short_overdraft(self):
        self.assertGreater(loan_cost(8000, LOAN)["total"], 10 * overdraft_cost(8000, 21, OVERDRAFT)["total"])


class ProductFileTests(unittest.TestCase):
    """The finance owners overwrite this file. These are the keys the code reads."""

    def test_file_has_the_keys_the_code_reads(self):
        products = json.loads(DEFAULT_PRODUCTS.read_text())
        self.assertLessEqual({"name", "yearly_rate_percent", "limit"}, set(products["overdraft"]))
        self.assertLessEqual(
            {"name", "yearly_rate_percent", "term_months", "min_amount", "max_amount"},
            set(products["term_loan"]),
        )


if __name__ == "__main__":
    unittest.main()
