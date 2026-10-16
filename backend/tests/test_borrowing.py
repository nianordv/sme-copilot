"""The cost of each way to cover a shortfall, and the draft request the owner approves."""

import json
import os
import unittest
from unittest.mock import patch

from backend.actions import registry
from backend.actions.borrowing import (
    ONLY_A_DRAFT, PLACEHOLDER_TERMS, compare_options, register_borrowing_actions,
)
from backend.actions.owner_only import register_owner_only_actions
from backend.actions.registry import audit_log, confirm, execute
from backend.tests.helpers import RegistryTestCase
from backend.tests.test_lookups import NAMES, ROWS

# In these books two customers are late: Tamada Hotel owes 2,000.00 and Tamada
# Cafe 1,500.50. The test overdraft is 16% a year with a 15,000 limit; the test
# loan is 15% a year plus 1%, over 12 months, from 5,000 to 50,000.
PRODUCTS = {
    "overdraft": {"name": "Test overdraft", "yearly_rate_percent": 16.0, "fee_percent": 0.0,
                  "limit": 15000.0},
    "term_loan": {"name": "Test loan", "yearly_rate_percent": 15.0, "fee_percent": 1.0,
                  "term_months": 12, "min_amount": 5000.0, "max_amount": 50000.0},
}
OWNER_TABLES = ("credit_request_drafts", "outbox", "alerts", "plans")


class BorrowingTestCase(RegistryTestCase):
    rows, names = ROWS, NAMES

    def setUp(self):
        super().setUp()
        self.products = self.folder / "products.json"
        self.write_products(PRODUCTS)
        pointed = patch.dict(os.environ, {"RIGHTSIZE_PRODUCTS": str(self.products)})
        pointed.start()
        self.addCleanup(pointed.stop)
        register_borrowing_actions()

    def write_products(self, products):
        self.products.write_text(json.dumps(products))

    def options(self, amount, days):
        result = compare_options(amount, days)
        return result, {option["option"]: option for option in result["options"]}

    def test_rate_scenarios_hold_principal_term_and_fees_constant(self):
        result = compare_options(4000, 30, quoted_annual_rate=17)
        rows = result['loan_rate_scenarios']
        self.assertEqual([r['yearly_rate_percent'] for r in rows], [12, 15, 17, 18])
        self.assertTrue(all(r['principal'] == 5000 and r['term_months'] == 12 and r['fee'] == 50 for r in rows))
        self.assertEqual([r['cost'] for r in rows], sorted(r['cost'] for r in rows))
        primary = next(r for r in result['options'] if r['option'] == 'term_loan')
        self.assertEqual(primary['cost'], rows[2]['cost'])
        self.assertEqual(primary['yearly_rate_percent'], 17)

    def test_zero_quoted_rate_still_includes_fees(self):
        result = compare_options(6000, 30, quoted_annual_rate=0)
        row = result['loan_rate_scenarios'][0]
        self.assertEqual(row['monthly_payment'], 500)
        self.assertEqual(row['interest'], 0)
        self.assertEqual(row['cost'], 60)

    def ask(self, arguments, session_id="s1"):
        reply = execute("prepare_credit_request", arguments, session_id)
        self.assertEqual(reply["status"], "needs_confirmation", reply)
        return reply

    def approve(self, arguments, session_id="s1"):
        reply = confirm(self.ask(arguments, session_id)["confirmation_id"], True, session_id)
        self.assertEqual(reply["status"], "ok", reply)
        return reply["result"]

    def tables_made(self):
        marks = ", ".join("?" for _ in OWNER_TABLES)
        return [name for (name,) in self.query(
            f"SELECT name FROM sqlite_master WHERE name IN ({marks})", OWNER_TABLES)]

    def drafts(self):
        if "credit_request_drafts" not in self.tables_made():
            return []
        return self.query("SELECT product, amount, details, status, created_on FROM credit_request_drafts")


class CompareOptionsTests(BorrowingTestCase):
    def test_the_brief_example_on_overdraft(self):
        _, by_name = self.options(8000, 21)
        # 8,000 x 0.16 x 21 / 365 = 73.6438...
        self.assertEqual(by_name["overdraft"]["cost"], 73.64)
        self.assertEqual(by_name["overdraft"]["yearly_rate_percent"], 16.0)

    def test_small_shortfall_reminder_first_overdraft_as_fallback(self):
        result, by_name = self.options(1000, 21)
        self.assertEqual((result["recommended"], result["fallback"]), ("remind_late_customers", "overdraft"))
        # Cheapest first; the two free options keep their order.
        self.assertEqual([option["option"] for option in result["options"]],
                         ["remind_late_customers", "wait", "overdraft", "term_loan"])
        # The largest late customer alone owes enough.
        remind = by_name["remind_late_customers"]
        self.assertEqual((remind["customers_to_remind"], remind["they_owe"]), (["Tamada Hotel"], 2000.00))
        # 1,000 x 0.16 x 21 / 365 = 9.2054...
        self.assertEqual(by_name["overdraft"]["cost"], 9.21)

    def test_loan_cannot_be_smaller_than_the_banks_minimum(self):
        _, by_name = self.options(1000, 21)
        # Borrow 5,000: payment 451.29, interest 12 x 451.29 - 5,000 = 415.48, fee 50.00.
        self.assertEqual(by_name["term_loan"]["cost"], 465.48)
        self.assertIn("more than is needed", by_name["term_loan"]["note"])
        self.assertTrue(by_name["term_loan"]["covers_amount"])

    def test_reminds_as_many_late_customers_as_the_shortfall_needs(self):
        _, by_name = self.options(3000, 21)
        remind = by_name["remind_late_customers"]
        self.assertEqual(remind["customers_to_remind"], ["Tamada Hotel", "Tamada Cafe"])
        self.assertEqual(remind["they_owe"], 3500.50)
        self.assertTrue(remind["covers_amount"])

    def test_reminders_are_not_recommended_when_late_customers_owe_too_little(self):
        result, by_name = self.options(4000, 10)
        self.assertFalse(by_name["remind_late_customers"]["covers_amount"])
        self.assertEqual((result["recommended"], result["fallback"]), ("overdraft", None))

    def test_loan_is_recommended_only_above_the_overdraft_limit(self):
        result, by_name = self.options(20000, 10)
        self.assertFalse(by_name["overdraft"]["covers_amount"])
        self.assertEqual(result["recommended"], "term_loan")

    def test_nothing_is_recommended_when_nothing_covers_the_shortfall(self):
        result, _ = self.options(60000, 10)
        self.assertEqual((result["recommended"], result["fallback"]), (None, None))
        self.assertFalse(any(option["recommended"] for option in result["options"]))

    def test_waiting_is_never_recommended(self):
        for amount in (1, 1000, 4000, 20000, 60000):
            _, by_name = self.options(amount, 10)
            self.assertFalse(by_name["wait"]["recommended"])
            self.assertFalse(by_name["wait"]["covers_amount"])

    def test_every_option_shows_its_cost_and_yearly_rate(self):
        result, _ = self.options(8000, 21)
        for option in result["options"]:
            self.assertIsInstance(option["cost"], float, option["option"])
            self.assertIsInstance(option["yearly_rate_percent"], float, option["option"])
        costs = [option["cost"] for option in result["options"]]
        self.assertEqual(costs, sorted(costs))

    def test_exactly_one_option_is_marked_recommended(self):
        result, _ = self.options(1000, 21)
        self.assertEqual([o["option"] for o in result["options"] if o["recommended"]], ["remind_late_customers"])
        self.assertEqual([o["option"] for o in result["options"] if o["fallback"]], ["overdraft"])

    def test_no_shortfall_means_no_borrowing(self):
        result = compare_options(0.001, 21)
        self.assertEqual((result["shortfall"], result["options"]), (0.0, []))
        self.assertIn("no borrowing is needed", result["message"])
        for arguments, clue in (({"amount": 0, "days": 21}, "amount"), ({"amount": -5, "days": 21}, "amount"),
                                ({"amount": 1e300, "days": 21}, "amount"), ({"amount": 8000, "days": 0}, "days"),
                                ({"amount": 8000, "days": 366}, "days"), ({"amount": 8000}, "days"),
                                ({"amount": 8000, "days": 21, "rate": 1}, "rate")):
            reply = execute("compare_options", arguments, "s1")
            self.assertEqual(reply["status"], "error", arguments)
            self.assertIn(f"Invalid action arguments: {clue}", reply["message"])

    def test_nobody_late_means_nobody_to_remind(self):
        self.write_books([ROWS[-1]])
        self.run_seed()
        result, by_name = self.options(1000, 21)
        self.assertEqual(by_name["remind_late_customers"]["customers_to_remind"], [])
        self.assertEqual(result["recommended"], "overdraft")

    def test_runs_through_the_registry_and_stays_small(self):
        reply = execute("compare_options", {"amount": "8000", "days": "21"}, "s1")
        self.assertEqual(reply["status"], "ok")
        self.assertEqual(reply["result"]["shortfall"], 8000.0)
        # Includes three compact loan-rate scenarios as well as the options.
        self.assertLess(len(json.dumps(reply)), 3200)
        self.assertEqual(self.decisions(), [("compare_options", "ran")])
        self.assertEqual(self.tables_made(), [])  # A look-up stores nothing.

    def test_placeholder_terms_are_called_placeholders(self):
        self.assertNotIn("note", compare_options(8000, 21))
        self.write_products({**PRODUCTS, "placeholder": True})
        self.assertEqual(compare_options(8000, 21)["note"], PLACEHOLDER_TERMS)

    def test_new_terms_in_the_file_are_used_at_once(self):
        self.write_products({**PRODUCTS, "overdraft": {**PRODUCTS["overdraft"], "yearly_rate_percent": 20.0}})
        _, by_name = self.options(8000, 21)
        # 8,000 x 0.20 x 21 / 365 = 92.0547...
        self.assertEqual((by_name["overdraft"]["cost"], by_name["overdraft"]["yearly_rate_percent"]),
                         (92.05, 20.0))


class CreditRequestTests(BorrowingTestCase):
    def test_owner_is_told_it_is_a_draft_with_its_price(self):
        self.assertEqual(
            self.ask({"product": "overdraft", "amount": 15000})["summary"],
            f"Save a draft request to the bank: Test overdraft, GEL 15,000.00, 16% a year?\n{ONLY_A_DRAFT}")
        self.assertEqual(
            self.ask({"product": "term_loan", "amount": 30000})["summary"],
            "Save a draft request to the bank: Test loan, GEL 30,000.00, "
            f"15% a year plus a 1% fee, over 12 months?\n{ONLY_A_DRAFT}")

    def test_nothing_is_saved_until_the_owner_says_yes(self):
        reply = self.ask({"product": "term_loan", "amount": 30000})
        self.assertEqual(self.tables_made(), [])  # Asking only reads.
        result = confirm(reply["confirmation_id"], True, "s1")["result"]
        self.assertEqual((result["draft_id"], result["product"], result["amount"], result["submitted"]),
                         (1, "Test loan", 30000.0, False))
        self.assertEqual(result["status"], "draft, not submitted")
        (product, amount, details, status, created_on), = self.drafts()
        self.assertEqual((product, amount, status, created_on), ("term_loan", 30000.0, "draft", "2026-10-03"))
        self.assertEqual(json.loads(details), {
            "applicant": "Test Wholesale", "product": "Test loan", "amount": 30000.0,
            "currency": "GEL", "yearly_rate_percent": 15.0, "fee_percent": 1.0,
            "prepared_on": "2026-10-03", "term_months": 12,
        })
        self.assertEqual(self.tables_made(), ["credit_request_drafts"])

    def test_a_no_saves_nothing(self):
        reply = self.ask({"product": "overdraft", "amount": 15000})
        self.assertEqual(confirm(reply["confirmation_id"], False, "s1")["status"], "refused")
        self.assertEqual(self.drafts(), [])

    def test_amount_outside_the_products_limits_never_reaches_the_owner(self):
        cannot = [
            ({"product": "overdraft", "amount": 15000.01}, "The most an overdraft can be is GEL 15,000.00."),
            ({"product": "term_loan", "amount": 4999},
             "A loan must be between GEL 5,000.00 and GEL 50,000.00."),
            ({"product": "term_loan", "amount": 50001},
             "A loan must be between GEL 5,000.00 and GEL 50,000.00."),
            ({"product": "overdraft", "amount": 0.001}, "The amount must be at least GEL 0.01."),
        ]
        for arguments, reason in cannot:
            self.assertEqual(execute("prepare_credit_request", arguments, "s1"),
                             {"status": "error", "message": reason}, arguments)
        self.assertEqual(self.query("SELECT COUNT(*) FROM confirmations")[0][0], 0)
        self.assertEqual({row["decision"] for row in audit_log("s1")}, {"error"})

    def test_arguments_outside_the_schema_are_turned_down_first(self):
        for arguments, clue in (({"product": "mortgage", "amount": 10000}, "product"),
                                ({"product": "overdraft"}, "amount"),
                                ({"product": "overdraft", "amount": 0}, "amount"),
                                ({"product": "overdraft", "amount": "a lot"}, "amount"),
                                # The model cannot name its own price.
                                ({"product": "overdraft", "amount": 1000, "yearly_rate_percent": 1},
                                 "yearly_rate_percent"),
                                ({"product": "overdraft", "amount": 1000, "name": "Free money"}, "name")):
            reply = execute("prepare_credit_request", arguments, "s1")
            self.assertEqual(reply["status"], "error", arguments)
            self.assertIn(f"Invalid action arguments: {clue}", reply["message"])
        self.assertEqual(self.drafts(), [])

    def test_what_is_approved_is_the_tidied_amount_with_the_terms_written_in(self):
        self.ask({"product": "overdraft", "amount": "14999.996"})
        self.assertEqual(audit_log("s1")[0]["arguments"], {
            "product": "overdraft", "amount": 15000.0, "name": "Test overdraft",
            "yearly_rate_percent": 16.0, "fee_percent": 0.0,
        })

    def test_the_rate_the_owner_approved_is_the_rate_saved(self):
        reply = self.ask({"product": "overdraft", "amount": 15000})
        self.assertIn("16% a year", reply["summary"])
        # The terms file changes while the owner is reading the card.
        self.write_products({**PRODUCTS, "overdraft": {**PRODUCTS["overdraft"], "yearly_rate_percent": 30.0}})
        confirm(reply["confirmation_id"], True, "s1")
        self.assertEqual(json.loads(self.drafts()[0][2])["yearly_rate_percent"], 16.0)

    def test_saving_a_draft_does_not_submit_it(self):
        register_owner_only_actions()
        self.approve({"product": "overdraft", "amount": 15000})
        reply = execute("submit_credit_request", {"draft_id": 1}, "s1")
        self.assertEqual(reply["status"], "refused")
        self.assertEqual([draft[3] for draft in self.drafts()], ["draft"])
        self.assertEqual(self.decisions()[-1], ("submit_credit_request", "refused"))

    def test_asking_again_saves_nothing(self):
        self.ask({"product": "overdraft", "amount": 15000})
        again = execute("prepare_credit_request", {"product": "overdraft", "amount": 15000}, "s1")
        self.assertEqual(again["status"], "needs_confirmation")
        self.assertEqual(self.drafts(), [])

    def test_drafts_survive_rebuilding_the_books(self):
        self.approve({"product": "overdraft", "amount": 15000})
        self.run_seed()
        self.assertEqual(len(self.drafts()), 1)


class ServerTests(unittest.TestCase):
    # The permission table from docs/Idea_md.md, as the running server has it.
    LEVELS = {
        "get_receivables": "green", "get_customer": "green", "get_balance": "green",
        "get_upcoming_outflows": "green", "draft_reminder": "green", "compare_options": "green",
        "send_reminder": "yellow", "set_alert": "yellow", "save_plan": "yellow",
        "prepare_credit_request": "yellow",
        "transfer_money": "red", "take_loan": "red", "repay_loan": "red",
        "submit_credit_request": "red",
    }

    def test_every_action_has_the_level_the_brief_gives_it(self):
        from backend import main  # noqa: F401  Registers the real actions on import.

        levels = {action["name"]: action["level"] for action in registry.list_actions()}
        self.assertEqual({name: levels.get(name) for name in self.LEVELS}, self.LEVELS)

    def test_no_action_can_approve_or_unlock(self):
        from backend import main  # noqa: F401

        names = {action["name"] for action in registry.list_actions()}
        self.assertFalse(names & {"confirm", "unlock", "approve"})


if __name__ == "__main__":
    unittest.main()
