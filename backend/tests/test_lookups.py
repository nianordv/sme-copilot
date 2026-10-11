import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import agent
from backend.actions import registry
from backend.actions.demo import register_demo_actions
from backend.actions.lookups import (
    as_flag, as_whole_number, get_balance, get_customer, get_receivables,
    get_upcoming_outflows, register_lookup_actions,
)
from backend.bank.seed import DEFAULT_CSV, DEFAULT_METADATA, seed
from backend.tests.helpers import BooksTestCase, invoice

# Today in these books is 2026-10-03.
ROWS = [
    # Tamada Cafe: three paid in the last year (3 late, 2 early, 10 late), three unpaid.
    invoice("INV-01", "CL0001", "Cafe", "Tbilisi", "2026-06-01", "2026-07-01", 100, paid="2026-07-04"),
    invoice("INV-02", "CL0001", "Cafe", "Tbilisi", "2026-07-01", "2026-07-31", 100, paid="2026-07-29"),
    invoice("INV-03", "CL0001", "Cafe", "Tbilisi", "2026-07-31", "2026-08-30", 100, paid="2026-09-09"),
    invoice("INV-04", "CL0001", "Cafe", "Tbilisi", "2026-08-24", "2026-09-23", 1000.00),   # 10 days late
    invoice("INV-05", "CL0001", "Cafe", "Tbilisi", "2026-07-25", "2026-08-24", 500.50),    # 40 days late
    invoice("INV-06", "CL0001", "Cafe", "Tbilisi", "2026-09-20", "2026-10-20", 250.25),    # not due yet
    # Tamada Hotel: never paid anything, one very late invoice, one written off.
    invoice("INV-07", "CL0002", "Hotel", "Batumi", "2026-06-05", "2026-07-05", 2000.00),   # 90 days late
    invoice("INV-08", "CL0002", "Hotel", "Batumi", "2026-03-01", "2026-03-31", 400.40, writeoff="2026-06-29"),
    # Vake Restaurant: due today, so not late; last paid long ago.
    invoice("INV-09", "CL0003", "Restaurant", "Tbilisi", "2026-09-03", "2026-10-03", 300.00),
    invoice("INV-10", "CL0003", "Restaurant", "Tbilisi", "2024-01-01", "2024-01-31", 100, paid="2024-02-10"),
    # Vake Bar: owes nothing.
    invoice("INV-11", "CL0004", "Bar/Pub", "Kutaisi", "2026-08-01", "2026-08-31", 100, paid="2026-08-31"),
]
NAMES = {
    "CL0001": "Tamada Cafe", "CL0002": "Tamada Hotel",
    "CL0003": "Vake Restaurant", "CL0004": "Vake Bar",
}


class ReceivablesTests(BooksTestCase):
    rows, names = ROWS, NAMES

    def test_question_2_who_owes_and_who_is_late(self):
        result = get_receivables()
        self.assertEqual(result["as_of"], "2026-10-03")
        self.assertEqual(result["owed"], {"amount": 4050.75, "invoices": 5, "customers": 3})
        self.assertEqual(result["of_which_overdue"], {"amount": 3500.50, "invoices": 3, "customers": 2})
        self.assertEqual(result["overdue_by_days_late"], {
            "1-30": {"amount": 1000.00, "invoices": 1},
            "31-60": {"amount": 500.50, "invoices": 1},
            "over_60": {"amount": 2000.00, "invoices": 1},
        })
        self.assertEqual(
            [(r["customer"], r["owed"]) for r in result["largest_owed"]],
            [("Tamada Hotel", 2000.00), ("Tamada Cafe", 1750.75), ("Vake Restaurant", 300.00)],
        )
        self.assertEqual(
            [(r["customer"], r["overdue"], r["oldest_days_late"]) for r in result["largest_overdue"]],
            [("Tamada Hotel", 2000.00, 90), ("Tamada Cafe", 1500.50, 40)],
        )

    def test_invoice_due_today_is_not_late(self):
        result = get_receivables(customer="Vake Restaurant")
        self.assertEqual(result["owed"]["amount"], 300.00)
        self.assertEqual(result["of_which_overdue"]["invoices"], 0)
        self.assertEqual(result["invoices"][0]["days_late"], 0)

    def test_only_overdue(self):
        result = get_receivables(only_overdue=True)
        self.assertEqual(result["showing"], "unpaid invoices that are overdue")
        self.assertEqual(result["owed"]["amount"], 3500.50)
        self.assertEqual([r["customer"] for r in result["largest_overdue"]], ["Tamada Hotel", "Tamada Cafe"])
        self.assertNotIn("largest_owed", result)

    def test_minimum_days_overdue(self):
        result = get_receivables(min_days_overdue=31)
        self.assertEqual(result["owed"], {"amount": 2500.50, "invoices": 2, "customers": 2})
        self.assertEqual(result["overdue_by_days_late"]["1-30"]["invoices"], 0)

    def test_one_customer_lists_invoices_oldest_first(self):
        result = get_receivables(customer="tamada cafe")
        self.assertEqual(result["showing"], "all unpaid invoices of Tamada Cafe")
        self.assertEqual(
            [(i["invoice_id"], i["days_late"]) for i in result["invoices"]],
            [("INV-05", 40), ("INV-04", 10), ("INV-06", 0)],
        )

    def test_ambiguous_customer_returns_candidates(self):
        result = get_receivables(customer="Tamada")
        self.assertFalse(result["found"])
        self.assertEqual([c["name"] for c in result["candidates"]], ["Tamada Cafe", "Tamada Hotel"])
        self.assertNotIn("owed", result)

    def test_written_off_and_paid_invoices_are_not_owed(self):
        self.assertEqual(get_receivables(customer="Tamada Hotel")["owed"]["amount"], 2000.00)
        self.assertEqual(get_receivables(customer="CL0004")["owed"]["invoices"], 0)

    def test_nothing_owed_at_all(self):
        self.write_books([ROWS[-1]])
        self.run_seed()
        result = get_receivables()
        self.assertEqual(result["owed"], {"amount": 0.0, "invoices": 0, "customers": 0})
        self.assertEqual(result["largest_owed"], [])
        self.assertEqual(result["largest_overdue"], [])

    def test_never_more_than_ten_rows(self):
        many = [
            invoice(f"INV-{n:02d}", f"CL{n:04d}", "Cafe", "Tbilisi", "2026-08-01", "2026-08-31", 100 + n)
            for n in range(1, 31)
        ]
        self.write_books(many)
        self.run_seed()
        everything = get_receivables()
        self.assertEqual(everything["owed"]["invoices"], 30)
        self.assertEqual(len(everything["largest_owed"]) + len(everything["largest_overdue"]), 10)
        late = get_receivables(only_overdue=True)["largest_overdue"]
        self.assertEqual(len(late), 10)
        self.assertEqual(late[0]["overdue"], 130.00)  # Largest first.


class CustomerTests(BooksTestCase):
    rows, names = ROWS, NAMES

    def test_found_by_name_or_id_in_any_case(self):
        for text in ("Tamada Cafe", "  tamada   CAFE ", "cl0001", "cafe tamada"):
            self.assertEqual(get_customer(text)["customer"]["customer_id"], "CL0001", text)

    def test_what_they_owe_and_how_late_they_usually_pay(self):
        result = get_customer("Tamada Cafe")
        self.assertEqual(result["customer"], {
            "customer_id": "CL0001", "name": "Tamada Cafe", "segment": "Cafe", "city": "Tbilisi",
        })
        self.assertEqual(result["owed"], {"amount": 1750.75, "invoices": 3})
        self.assertEqual(result["of_which_overdue"], {"amount": 1500.50, "invoices": 2, "oldest_days_late": 40})
        self.assertEqual(len(result["open_invoices"]), 3)
        # Paid 3 days late, 2 days early and 10 days late.
        self.assertEqual(result["payment_habit"], {
            "period": "last 12 months", "invoices_paid": 3,
            "median_days_late": 3, "average_days_late": 3.7, "share_paid_late": 0.67,
        })

    def test_old_payments_are_used_when_there_are_no_recent_ones(self):
        habit = get_customer("Vake Restaurant")["payment_habit"]
        self.assertEqual(habit["period"], "all history")
        self.assertEqual(habit["median_days_late"], 10)

    def test_customer_who_never_paid_and_has_a_write_off(self):
        result = get_customer("Tamada Hotel")
        self.assertEqual(result["payment_habit"], {"period": "all history", "invoices_paid": 0})
        self.assertEqual(result["written_off"], {"amount": 400.40, "invoices": 1})

    def test_customer_who_owes_nothing(self):
        result = get_customer("Vake Bar")
        self.assertEqual(result["owed"], {"amount": 0.0, "invoices": 0})
        self.assertEqual(result["open_invoices"], [])

    def test_ambiguous_name_returns_candidates(self):
        result = get_customer("vake")
        self.assertFalse(result["found"])
        self.assertEqual(result["candidates_total"], 2)
        self.assertEqual(
            result["candidates"],
            [{"customer_id": "CL0004", "name": "Vake Bar", "city": "Kutaisi"},
             {"customer_id": "CL0003", "name": "Vake Restaurant", "city": "Tbilisi"}],
        )

    def test_unknown_name_returns_no_candidates(self):
        for text in ("Zzzz Trading", ""):
            result = get_customer(text)
            self.assertFalse(result["found"])
            self.assertEqual(result["candidates"], [])

    def test_misspelled_name_is_offered_not_assumed(self):
        result = get_customer("Tamda Cafe")
        self.assertFalse(result["found"])
        # Closest spelling first; "Tamada Hotel" is near enough to be offered too.
        self.assertEqual(result["candidates"][0]["name"], "Tamada Cafe")


class OwnMoneyTests(BooksTestCase):
    rows, names = ROWS, NAMES

    def test_balance(self):
        self.assertEqual(get_balance(), {
            "as_of": "2026-10-03", "currency": "GEL", "balance": 1000.00,
            "account_holder": "Test Wholesale",
        })

    def test_outflows_in_the_next_week(self):
        result = get_upcoming_outflows(days=7)
        self.assertEqual((result["from"], result["to"]), ("2026-10-04", "2026-10-10"))
        self.assertEqual(result["payments"], [{"date": "2026-10-10", "label": "Salaries", "amount": 50.00}])
        self.assertEqual(result["total"], 50.00)

    def test_outflows_default_is_30_days(self):
        result = get_upcoming_outflows()
        self.assertEqual(result["to"], "2026-11-02")
        self.assertEqual(result["by_category"], {"salaries": 50.00, "taxes": 30.00, "other": 70.00})
        self.assertEqual(result["total"], 150.00)

    def test_outflows_stop_at_60_days(self):
        result = get_upcoming_outflows(days=999)
        self.assertEqual((result["days"], result["to"]), (60, "2026-12-02"))
        self.assertEqual(result["total"], 230.00)

    def test_no_outflows_before_the_first_one(self):
        result = get_upcoming_outflows(days=3)
        self.assertEqual((result["total"], result["payments"]), (0.0, []))


class ThroughTheRegistryTests(BooksTestCase):
    """The look-ups as the model reaches them: registered, then run by execute()."""

    rows, names = ROWS, NAMES

    def setUp(self):
        super().setUp()
        emptied = patch.dict(registry._actions, {}, clear=True)
        emptied.start()
        self.addCleanup(emptied.stop)
        register_lookup_actions()

    def run_action(self, name, arguments=None):
        reply = registry.execute(name, arguments or {}, "owner")
        self.assertEqual(reply["status"], "ok", reply)
        return reply["result"]

    def test_four_green_actions_are_registered(self):
        levels = {action["name"]: action["level"] for action in registry.list_actions()}
        self.assertEqual(levels, {
            "get_receivables": "green", "get_customer": "green",
            "get_balance": "green", "get_upcoming_outflows": "green",
        })

    def test_every_lookup_runs_without_confirmation(self):
        calls = [
            ("get_receivables", {}), ("get_customer", {"customer": "Vake Bar"}),
            ("get_balance", {}), ("get_upcoming_outflows", {"days": 14}),
        ]
        for name, arguments in calls:
            reply = registry.execute(name, arguments, "owner")
            self.assertEqual(reply["status"], "ok", name)
            json.dumps(reply, allow_nan=False)

    def test_question_2_is_answered_by_one_call(self):
        result = self.run_action("get_receivables")
        self.assertEqual(result["owed"]["amount"], 4050.75)
        self.assertEqual(result["of_which_overdue"], {"amount": 3500.50, "invoices": 3, "customers": 2})
        self.assertEqual([r["customer"] for r in result["largest_overdue"]], ["Tamada Hotel", "Tamada Cafe"])

    def test_numbers_and_flags_sent_as_text_are_read_correctly(self):
        everything = self.run_action("get_receivables", {"only_overdue": "false"})
        self.assertEqual(everything["showing"], "all unpaid invoices")
        late = self.run_action("get_receivables", {"only_overdue": "true"})
        self.assertEqual(late["owed"]["amount"], 3500.50)
        older = self.run_action("get_receivables", {"min_days_overdue": "31"})
        self.assertEqual(older["owed"]["invoices"], 2)
        week = self.run_action("get_upcoming_outflows", {"days": "7"})
        self.assertEqual(week["to"], "2026-10-10")

    def test_arguments_left_empty_mean_the_default(self):
        result = self.run_action("get_receivables", {"only_overdue": None, "min_days_overdue": None, "customer": None})
        self.assertEqual(result["showing"], "all unpaid invoices")
        self.assertEqual(self.run_action("get_upcoming_outflows", {"days": None})["days"], 30)

    def test_argument_the_action_does_not_have_is_an_error_not_a_guess(self):
        reply = registry.execute("get_receivables", {"city": "Batumi"}, "owner")
        self.assertEqual(reply["status"], "error")

    def test_ambiguous_customer_is_a_result_the_model_can_ask_about(self):
        result = self.run_action("get_customer", {"customer": "Tamada"})
        self.assertFalse(result["found"])
        self.assertEqual([c["name"] for c in result["candidates"]], ["Tamada Cafe", "Tamada Hotel"])

    def test_missing_books_are_an_error_and_the_log_says_how_to_build_them(self):
        self.database.unlink()
        with self.assertLogs(registry.logger, level="ERROR") as logged:
            reply = registry.execute("get_balance", {}, "owner")
        self.assertEqual(reply["status"], "error")
        self.assertIn("backend.bank.seed", logged.output[0])

    def test_registers_beside_the_demo_reminder_as_the_server_does(self):
        register_demo_actions()
        names = [action["name"] for action in registry.list_actions()]
        self.assertEqual(sorted(names), sorted([
            "get_receivables", "get_customer", "get_balance", "get_upcoming_outflows", "send_reminder",
        ]))
        for action in registry.list_actions():
            if action["level"] == "green":
                self.assertNotIn("FAKE", action["description"])

    def test_agent_offers_them_to_the_model_as_tools(self):
        tools = {tool["function"]["name"]: tool["function"] for tool in agent.tool_catalog()}
        self.assertEqual(len(tools), 4)
        # The agent adds this hint only to actions that take no arguments.
        self.assertIn("empty object: {}", tools["get_balance"]["description"])
        self.assertNotIn("empty object: {}", tools["get_receivables"]["description"])
        self.assertEqual(tools["get_customer"]["parameters"]["required"], ["customer"])


class ArgumentReadingTests(unittest.TestCase):
    def test_flags(self):
        for yes in (True, 1, "true", "True", " yes ", "1"):
            self.assertTrue(as_flag(yes), yes)
        for no in (False, 0, None, "", "false", "False", "no", "0"):
            self.assertFalse(as_flag(no), no)

    def test_whole_numbers(self):
        self.assertEqual([as_whole_number(v, 9) for v in (30, "30", 30.0, "30.0", None, "")], [30, 30, 30, 30, 9, 9])
        with self.assertRaises(ValueError):
            as_whole_number("soon", 9)


@unittest.skipUnless(
    DEFAULT_CSV.exists() and DEFAULT_METADATA.exists(),
    "generate the data first: python -m ml.generate_invoices_ge",
)
class RealBooksTests(unittest.TestCase):
    """The model's memory is 4096 tokens, so results on the full books stay small."""

    @classmethod
    def setUpClass(cls):
        cls.previous = os.environ.get("RIGHTSIZE_DB")
        os.environ["RIGHTSIZE_DB"] = str(Path(tempfile.mkdtemp()) / "books.db")
        seed(database=Path(os.environ["RIGHTSIZE_DB"]))

    @classmethod
    def tearDownClass(cls):
        if cls.previous is None:
            del os.environ["RIGHTSIZE_DB"]
        else:
            os.environ["RIGHTSIZE_DB"] = cls.previous

    def test_results_fit_the_models_memory(self):
        biggest = get_receivables()["largest_owed"][0]["customer"]
        results = [
            get_receivables(), get_receivables(only_overdue=True),
            get_receivables(customer=biggest), get_customer(biggest),
            get_customer("a"), get_balance(), get_upcoming_outflows(days=60),
        ]
        for result in results:
            # About 4 characters to a token: 2,400 characters is about 600 tokens.
            self.assertLess(len(json.dumps(result)), 2400)
            self.assertLess(len(json.dumps({"status": "ok", "result": result})), agent.MAX_RESULT_CHARS)

    def test_overdue_bands_add_up_to_the_overdue_total(self):
        result = get_receivables()
        bands = result["overdue_by_days_late"].values()
        self.assertAlmostEqual(sum(b["amount"] for b in bands), result["of_which_overdue"]["amount"], places=2)
        self.assertEqual(sum(b["invoices"] for b in bands), result["of_which_overdue"]["invoices"])


if __name__ == "__main__":
    unittest.main()
