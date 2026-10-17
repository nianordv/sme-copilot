"""The dashboard's data: the same figures as the actions, and nothing written."""

import csv
import hashlib
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import main  # Importing main registers the real actions.
from backend.actions import registry
from backend.actions.borrowing import saved_credit_requests
from backend.actions.planning import newest_plan, saved_alerts
from backend.actions.reminders import saved_reminders, send_reminder
from backend.dashboard import NO_FORECAST, NO_FORECAST_FILES, read_dashboard
from backend.tests import helpers
from backend.tests.helpers import NO_ARGUMENTS, BooksTestCase, RegistryTestCase
from backend.tests.test_lookups import NAMES, ROWS

PRODUCTS = {
    "overdraft": {"name": "Test overdraft", "yearly_rate_percent": 16.0, "fee_percent": 0.0, "limit": 15000.0},
    "term_loan": {"name": "Test loan", "yearly_rate_percent": 15.0, "fee_percent": 1.0,
                  "term_months": 12, "min_amount": 5000.0, "max_amount": 50000.0},
}
CHAT = "chat-1"
KEY = "answer-key"
SAVED_TABLES = ("outbox", "alerts", "plans", "credit_request_drafts")


class DashboardTestCase(BooksTestCase):
    """The books of the look-up tests (today is 2026-10-03), the product terms
    and the forecast files a real set-up has."""

    rows, names = ROWS, NAMES

    def setUp(self):
        super().setUp()
        products = self.folder / "products.json"
        products.write_text(json.dumps(PRODUCTS))
        self.forecast = self.folder / "forecast"
        self.forecast.mkdir()
        self.write_forecast_files()
        for patched in (patch.dict(os.environ, {"RIGHTSIZE_PRODUCTS": str(products)}),
                        patch("backend.actions.forecast.FORECAST", self.forecast)):
            patched.start()
            self.addCleanup(patched.stop)

    def write_forecast_files(self, snapshot="2026-10-03"):
        """One payment curve per outstanding invoice, as the forecast step
        saves them: the chance of having been paid rises by 2% a day."""
        outstanding = self.query(
            "SELECT invoice_id, customer_id, amount FROM invoices "
            "WHERE paid_date IS NULL AND writeoff_date IS NULL ORDER BY invoice_id")
        curves = self.forecast / "open_invoice_curves.csv"
        with curves.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["invoice_id", "client_id", "amount", *[f"day_{day}" for day in range(1, 61)]])
            for invoice_id, customer_id, amount in outstanding:
                writer.writerow([invoice_id, customer_id, amount, *[min(1.0, day / 50) for day in range(1, 61)]])
        (self.forecast / "metadata.json").write_text(json.dumps({
            "snapshot_date": snapshot, "currency": "GEL", "horizon_days": 60,
            "source_csv_sha256": hashlib.sha256(self.csv.read_bytes()).hexdigest(),
            "invoice_count": len(outstanding),
            "forecast_csv_sha256": hashlib.sha256(curves.read_bytes()).hexdigest(),
            "model_used": "test", "fallback": False}))

    def action(self, name, **arguments):
        """What the action returns when Bill calls it."""
        reply = registry.execute(name, arguments, KEY)
        self.assertEqual(reply["status"], "ok", reply)
        return reply["result"]

    def approved(self, name, **arguments):
        """A Yellow action the owner said Yes to. Returns the sentence on its card."""
        asked = registry.execute(name, arguments, CHAT)
        self.assertEqual(asked["status"], "needs_confirmation", asked)
        done = registry.confirm(asked["confirmation_id"], True, CHAT)
        self.assertEqual(done["status"], "ok", done)
        return asked["summary"]

    def log_rows(self):
        return self.query("SELECT COUNT(*) FROM audit_log")[0][0]


class CashAndForecastTests(DashboardTestCase):
    def test_every_figure_is_the_one_the_matching_action_returns(self):
        page = read_dashboard()
        self.assertEqual(set(page), {"cash", "forecast", "work", "log"})
        cash = page["cash"]
        self.assertEqual(cash["as_of"], "2026-10-03")
        self.assertEqual(cash["balance"], self.action("get_balance"))
        self.assertEqual(cash["receivables"], self.action("get_receivables"))
        self.assertEqual(cash["upcoming_outflows"], self.action("get_upcoming_outflows", days=30))
        self.assertEqual(page["forecast"], {"available": True, "result": self.action("forecast_cash", days=30)})

    def test_the_figures_of_these_books(self):
        # Worked out by hand from the rows, so a mistake shared with the actions would show.
        page = read_dashboard()
        cash, forecast = page["cash"], page["forecast"]["result"]
        self.assertEqual(cash["balance"]["balance"], 1000.0)
        self.assertEqual(cash["receivables"]["owed"], {"amount": 4050.75, "invoices": 5, "customers": 3})
        self.assertEqual(cash["receivables"]["of_which_overdue"], {"amount": 3500.5, "invoices": 3, "customers": 2})
        self.assertEqual(cash["receivables"]["overdue_by_days_late"]["over_60"], {"amount": 2000.0, "invoices": 1})
        # Salaries 50 on the 10th, taxes 30 on the 15th, the van 70 on the 20th.
        self.assertEqual((cash["upcoming_outflows"]["total"], cash["upcoming_outflows"]["to"]), (150.0, "2026-11-02"))
        self.assertEqual(forecast["days"], 30)
        # If no customer pays at all, the balance ends 150 lower and never goes below zero.
        self.assertEqual((forecast["worst"]["lowest_balance"], forecast["worst"]["lowest_date"],
                          forecast["worst"]["first_day_below_zero"], forecast["worst"]["shortfall"]),
                         (850.0, "2026-10-20", None, 0.0))
        for case in ("likely", "cautious", "worst"):
            self.assertLessEqual({"lowest_balance", "lowest_date", "first_day_below_zero", "shortfall",
                                  "weekly_balances"}, set(forecast[case]))
            self.assertEqual(len(forecast[case]["weekly_balances"]), 5)

    def test_without_the_forecast_files_the_other_three_parts_still_arrive(self):
        with_forecast = read_dashboard()
        with patch("backend.actions.forecast.FORECAST", self.folder / "nowhere"):
            page = read_dashboard()
        self.assertEqual(page["forecast"], {"available": False, "reason": NO_FORECAST_FILES})
        for part in ("cash", "work", "log"):
            self.assertEqual(page[part], with_forecast[part])

    def test_a_forecast_that_does_not_fit_the_books_is_unavailable_too(self):
        self.write_forecast_files(snapshot="2026-09-30")
        with self.assertLogs("backend.dashboard", "ERROR") as logged:
            page = read_dashboard()
        self.assertEqual(page["forecast"], {"available": False, "reason": NO_FORECAST})
        self.assertIn("Forecast and database disagree", logged.output[0])
        self.assertEqual(page["cash"]["balance"]["balance"], 1000.0)


class ReadOnlyTests(DashboardTestCase):
    def test_reading_adds_no_log_rows_and_changes_nothing(self):
        self.approved("send_reminder", customer="Tamada Hotel")
        self.approved("save_plan", text="Remind the two late customers first.")
        registry.execute("set_alert", {"balance_below": 500}, CHAT)  # Left waiting.
        registry.execute("transfer_money", {"amount": 5000}, CHAT)
        rows, stored = self.log_rows(), self.database.read_bytes()
        first = read_dashboard()
        for _ in range(3):
            self.assertEqual(read_dashboard(), first)
        self.assertEqual(self.log_rows(), rows)
        self.assertEqual(self.database.read_bytes(), stored)

    def test_on_books_nothing_was_saved_to_no_table_is_made(self):
        page = read_dashboard()
        self.assertEqual(page["work"], {
            "waiting": {"items": [], "not_listed": 0}, "reminders": {"items": [], "not_listed": 0},
            "alerts": {"items": [], "not_listed": 0}, "plan": None,
            "credit_request_drafts": {"items": [], "not_listed": 0}, "alert_checks": []})
        self.assertEqual(page["log"], {"check": {"intact": True, "rows": 0, "last_fingerprint": None}, "rows": []})
        made = {name for name, in self.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(made & set(SAVED_TABLES), set())
        self.assertEqual(self.log_rows(), 0)


class WorkTests(DashboardTestCase):
    def test_a_waiting_request_shows_the_sentence_on_its_card(self):
        asked = registry.execute("send_reminder", {"customer": "tamada hotel"}, CHAT)
        page = read_dashboard()
        waiting, = page["work"]["waiting"]["items"]
        self.assertEqual(waiting, {"session_id": CHAT, "action": "send_reminder", "summary": asked["summary"],
                                   "expires_at": waiting["expires_at"]})
        self.assertIn("Send this payment reminder to Tamada Hotel (CL0002)?", waiting["summary"])
        # The ID that approves it stays with the card in the chat.
        self.assertNotIn(asked["confirmation_id"], json.dumps(page))
        self.assertEqual(page["work"]["reminders"]["items"], [])

        registry.confirm(asked["confirmation_id"], True, CHAT)
        page = read_dashboard()
        self.assertEqual(page["work"]["waiting"], {"items": [], "not_listed": 0})
        reminder, = page["work"]["reminders"]["items"]
        self.assertEqual((reminder["customer"], reminder["customer_id"], reminder["saved_on"]),
                         ("Tamada Hotel", "CL0002", "2026-10-03"))
        self.assertEqual(reminder["text"], self.query("SELECT text FROM outbox")[0][0])

    def test_a_request_past_its_ten_minutes_is_not_listed_and_not_touched(self):
        registry.execute("send_reminder", {"customer": "Tamada Hotel"}, CHAT)
        later = datetime.now(timezone.utc) + timedelta(minutes=registry.CONFIRM_MINUTES, seconds=1)
        with patch.object(registry, "_now", return_value=later):
            self.assertEqual(registry.waiting_requests(), {"items": [], "not_listed": 0})
        self.assertEqual(self.query("SELECT COUNT(*) FROM confirmations"), [(1,)])
        self.assertEqual([row["decision"] for row in registry.audit_log(CHAT)], ["waiting"])

    def test_alerts_the_newest_plan_and_drafts(self):
        self.approved("set_alert", date="2026-10-09", note="Before salaries")
        self.approved("set_alert", balance_below=500)
        self.approved("save_plan", text="First plan.")
        self.approved("save_plan", text="Remind Tamada Hotel, then use the overdraft for 21 days.")
        self.approved("prepare_credit_request", product="overdraft", amount=8000)
        work = read_dashboard()["work"]
        self.assertEqual(work["alerts"], {"not_listed": 0, "items": [
            {"alert_id": 2, "saved_on": "2026-10-03", "date": None, "balance_below": 500.0, "note": None},
            {"alert_id": 1, "saved_on": "2026-10-03", "date": "2026-10-09", "balance_below": None,
             "note": "Before salaries"}]})
        self.assertEqual(work["plan"], {"plan_id": 2, "saved_on": "2026-10-03",
                                        "text": "Remind Tamada Hotel, then use the overdraft for 21 days."})
        self.assertEqual(work["credit_request_drafts"], {"not_listed": 0, "items": [{
            "draft_id": 1, "saved_on": "2026-10-03", "product": "overdraft", "product_name": "Test overdraft",
            "amount": 8000.0, "currency": "GEL", "yearly_rate_percent": 16.0, "fee_percent": 0.0,
            "term_months": None, "status": "draft"}]})

    def test_long_lists_show_the_newest_and_say_how_many_more_there_are(self):
        for number in range(12):
            send_reminder("CL0002", f"Reminder {number}")
        reminders = saved_reminders(10)
        self.assertEqual([item["text"] for item in reminders["items"]],
                         [f"Reminder {number}" for number in range(11, 1, -1)])
        self.assertEqual(reminders["not_listed"], 2)
        self.assertEqual(read_dashboard()["work"]["reminders"], reminders)
        for number in range(25):
            registry.execute("save_plan", {"text": f"Plan {number}"}, f"chat-{number}")
        waiting = registry.waiting_requests(10)
        self.assertEqual((len(waiting["items"]), waiting["not_listed"]), (10, 15))
        self.assertEqual(waiting["items"][0]["session_id"], "chat-0")  # The one that expires first.

    def test_text_the_model_wrote_arrives_as_plain_text_with_nothing_hidden(self):
        override, unseen = chr(0x202E), chr(0x200B)
        written = f"Please pay.\n**Urgent** <b>now</b> [link](http://x) {override}tfel{unseen}"
        self.approved("send_reminder", customer="Tamada Hotel", text=written)
        self.approved("save_plan", text=written)
        self.approved("set_alert", balance_below=500, note=f"Check{override} it")
        page = read_dashboard()
        expected = "Please pay.\n**Urgent** <b>now</b> [link](http://x) \\u202etfel\\u200b"
        self.assertEqual(page["work"]["reminders"]["items"][0]["text"], expected)
        self.assertEqual(page["work"]["plan"]["text"], expected)
        self.assertEqual(page["work"]["alerts"]["items"][0]["note"], "Check\\u202e it")
        for row in page["log"]["rows"]:
            self.assertIsInstance(row["arguments"], str)
        sent = json.dumps(page, ensure_ascii=False)
        self.assertNotIn(override, sent)
        self.assertNotIn(unseen, sent)
        # What was saved, and what a customer would be sent, is unchanged.
        self.assertEqual(self.query("SELECT text FROM outbox")[0][0], written)

    def test_the_readers_are_not_actions(self):
        names = {action["name"] for action in registry.list_actions()}
        self.assertEqual(names & {"saved_reminders", "saved_alerts", "newest_plan", "saved_credit_requests",
                                  "waiting_requests", "read_dashboard", "dashboard"}, set())
        self.assertEqual((saved_alerts(), newest_plan(), saved_credit_requests()),
                         ({"items": [], "not_listed": 0}, None, {"items": [], "not_listed": 0}))


class LogTests(DashboardTestCase):
    def test_the_newest_twenty_rows_newest_first_and_the_check(self):
        registry.execute("transfer_money", {"to": "GE00XX", "amount": 5000}, CHAT)
        self.approved("send_reminder", customer="Tamada Hotel")
        log = read_dashboard()["log"]
        self.assertEqual(log["check"], registry.verify_log())
        self.assertEqual((log["check"]["intact"], log["check"]["rows"]), (True, 4))
        self.assertEqual([(row["action"], row["level"], row["decision"]) for row in log["rows"]], [
            ("send_reminder", "yellow", "ran"), ("send_reminder", "yellow", "approved"),
            ("send_reminder", "yellow", "waiting"), ("transfer_money", "red", "refused")])
        self.assertEqual(set(log["rows"][0]), {"log_id", "time", "session_id", "action", "level", "decision",
                                               "result", "arguments"})
        self.assertEqual(log["rows"][3]["arguments"], '{"to": "GE00XX", "amount": 5000}')
        self.assertEqual(log["rows"][3]["result"], registry.OWNER_ONLY)

        for _ in range(30):
            registry.execute("get_balance", {}, "another-chat")
        log = read_dashboard()["log"]
        self.assertEqual([row["log_id"] for row in log["rows"]], list(range(34, 14, -1)))
        self.assertEqual(log["check"]["rows"], 34)

    def test_only_the_rows_shown_are_read(self):
        # The page refreshes often and the log only grows: reading all of it
        # cost 62 ms per 10,000 rows to show 20.
        for _ in range(25):
            registry.execute("get_balance", {}, CHAT)
        expected = list(reversed(registry.audit_log()))[:20]
        with patch.object(registry, "audit_log", side_effect=AssertionError("read the whole log")):
            log = read_dashboard()["log"]
        self.assertEqual([row["log_id"] for row in log["rows"]], [row["log_id"] for row in expected])
        self.assertEqual(registry.newest_log_rows(3), expected[:3])
        self.assertEqual(registry.newest_log_rows(-1), [])

    def test_a_changed_row_is_reported(self):
        registry.execute("get_balance", {}, CHAT)
        registry.execute("transfer_money", {"amount": 5000}, CHAT)
        self.query("DROP TRIGGER audit_log_no_update")
        self.query("UPDATE audit_log SET decision = 'ran' WHERE log_id = 2")
        self.assertEqual(read_dashboard()["log"]["check"], {"intact": False, "rows": 2, "first_bad_row": 2})


class EndpointTests(DashboardTestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(main.app, base_url="http://127.0.0.1:8000")
        self.addCleanup(self.client.close)
        # The login (phase A6) is built beside this one. Where it is merged, the
        # endpoint needs a signed-in owner, and its own tests check that a
        # caller who is not signed in gets 401 from every endpoint.
        sign_in = getattr(helpers, "sign_in_as_owner", None)
        if sign_in is not None:
            sign_in(self, self.client)

    def test_one_get_returns_all_four_parts(self):
        self.approved("send_reminder", customer="Tamada Hotel")
        rows = self.log_rows()
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), read_dashboard())
        self.assertEqual(self.log_rows(), rows)

    def test_it_only_answers_a_get(self):
        for method in ("post", "put", "patch", "delete"):
            self.assertEqual(getattr(self.client, method)("/dashboard").status_code, 405)

    def test_without_the_books_it_says_how_to_build_them(self):
        with patch.dict(os.environ, {"RIGHTSIZE_DB": str(self.folder / "missing.db")}):
            response = self.client.get("/dashboard")
        self.assertIn(response.status_code, (401, 503))
        if response.status_code == 503:
            self.assertIn("python -m backend.bank.seed", response.json()["detail"])


class WaitingRequestsTests(RegistryTestCase):
    def test_a_request_whose_sentence_cannot_be_built_is_still_listed(self):
        def broken(**arguments):
            raise KeyError("customer")

        self.add("tidy", "yellow")
        self.add("fragile", "yellow", summarize=lambda: "Do the fragile thing?")
        registry.execute("tidy", {}, "s1")
        registry.execute("fragile", {}, "s2")
        with patch.dict(registry._actions):
            del registry._actions["tidy"]
            registry._actions["fragile"] = registry.Action(
                "fragile", "Test.", NO_ARGUMENTS, "yellow", lambda: {}, summarize=broken)
            with self.assertLogs(registry.logger, "ERROR"):
                waiting = registry.waiting_requests()
        self.assertEqual([(item["action"], item["summary"]) for item in waiting["items"]],
                         [("tidy", registry.CANNOT_BE_SHOWN), ("fragile", registry.CANNOT_BE_SHOWN)])
        self.assertEqual(waiting["not_listed"], 0)
        self.assertEqual(self.calls, [])  # Listing a request runs nothing.

    def test_an_action_with_no_sentence_of_its_own_shows_its_arguments(self):
        self.add("plain", "yellow", parameters={"type": "object"})
        asked = registry.execute("plain", {"amount": 5}, "s1")
        self.assertEqual(registry.waiting_requests()["items"][0]["summary"], asked["summary"])
        self.assertIn('{"amount": 5}', asked["summary"])


if __name__ == "__main__":
    unittest.main()
