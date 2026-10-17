import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from backend import main  # noqa: F401  (starting the app registers every action)
from backend.actions import registry
from backend.actions.demo import register_demo_actions
from backend.tests.helpers import use_temporary_database

ACTIVITY_LABELS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "activity.ts"


class ActivityLabelTests(unittest.TestCase):
    def test_every_action_has_a_label_on_the_activity_page(self):
        # The page names a log row by its action; a name it does not know
        # shows as "Activity request", as get_cash_risk once did.
        actions = ACTIVITY_LABELS.read_text().split("const decisions")[0]
        labelled = set(re.findall(r"^\s+(\w+): '", actions, re.MULTILINE))
        registered = {action["name"] for action in registry.list_actions()}
        self.assertTrue(registered)
        self.assertEqual(registered - labelled, set())


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.actions = patch.dict(registry._actions, {}, clear=True)
        self.actions.start()
        self.addCleanup(self.actions.stop)
        use_temporary_database(self)
        self.calls = []

    def add_action(self, level="yellow", handler=None):
        def record(payload):
            self.calls.append(payload)
            return {"payload": payload}
        registry.register(registry.Action("example", "Test action",
            {"type": "object"}, level, handler or record))

    def request(self):
        return registry.execute("example", {"payload": {"amount": 100}}, "owner")

    def test_green_runs(self):
        self.add_action("green")
        self.assertEqual(self.request()["status"], "ok")
        self.assertEqual(len(self.calls), 1)

    def test_deleted_chat_cancels_only_its_approvals_and_keeps_audit(self):
        self.add_action()
        pending = self.request()
        other = registry.execute("example", {"payload": {}}, "other")
        result = registry.cancel_chat_requests("owner")
        self.assertEqual(result, {"status": "ok", "result": {"cancelled": 1}})
        self.assertNotEqual(registry.confirm(pending["confirmation_id"], True, "owner")["status"], "ok")
        self.assertEqual(self.calls, [])
        self.assertEqual(registry.confirm(other["confirmation_id"], True, "other")["status"], "ok")
        self.assertTrue(any(row["decision"] == "waiting" for row in registry.audit_log("owner")))
        self.assertTrue(any(row["decision"] == "declined" for row in registry.audit_log("owner")))
        self.assertTrue(registry.verify_log()["intact"])
        self.assertEqual(registry.cancel_chat_requests("owner")["result"]["cancelled"], 0)

    def test_yellow_waits_and_uses_detached_exact_arguments(self):
        self.add_action()
        arguments = {"payload": {"amount": 100}}
        reply = registry.execute("example", arguments, "owner")
        self.assertEqual(reply["status"], "needs_confirmation")
        self.assertEqual(self.calls, [])
        arguments["payload"]["amount"] = 999
        result = registry.confirm(reply["confirmation_id"], True, "owner")
        self.assertEqual(result["result"]["payload"], {"amount": 100})

    def test_decline_consumes_request(self):
        self.add_action()
        cid = self.request()["confirmation_id"]
        self.assertEqual(registry.confirm(cid, False, "owner")["status"], "refused")
        self.assertEqual(registry.confirm(cid, True, "owner")["status"], "error")
        self.assertEqual(self.calls, [])

    def test_other_session_cannot_approve_or_consume(self):
        self.add_action()
        cid = self.request()["confirmation_id"]
        self.assertEqual(registry.confirm(cid, True, "other")["status"], "error")
        self.assertEqual(self.calls, [])
        self.assertEqual(registry.confirm(cid, True, "owner")["status"], "ok")

    def test_duplicate_concurrent_approval_runs_at_most_once(self):
        self.add_action()
        cid = self.request()["confirmation_id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: registry.confirm(cid, True, "owner"), range(2)))
        self.assertEqual(sorted(r["status"] for r in results), ["error", "ok"])
        self.assertEqual(len(self.calls), 1)

    def test_red_never_runs(self):
        self.add_action("red")
        self.assertEqual(self.request()["status"], "refused")
        self.assertEqual(self.calls, [])

    def test_unknown_action_and_missing_session(self):
        self.assertEqual(registry.execute("unknown", {}, "owner")["status"], "error")
        self.add_action("green")
        self.assertEqual(registry.execute("example", {"payload": 1}, "")["status"], "error")
        self.assertEqual(self.calls, [])

    def test_invalid_arguments_do_not_run(self):
        self.add_action("green")
        for arguments in ({}, {"payload": 1, "extra": 2}, [], {"payload": float("nan")}):
            self.assertEqual(registry.execute("example", arguments, "owner")["status"], "error")
        self.assertEqual(self.calls, [])

    def test_string_yes_is_not_boolean_approval(self):
        self.add_action()
        cid = self.request()["confirmation_id"]
        self.assertEqual(registry.confirm(cid, "yes", "owner")["status"], "error")
        self.assertEqual(self.calls, [])

    def test_handler_failure_is_sanitised_and_not_retried(self):
        def fail(payload):
            self.calls.append(payload)
            raise RuntimeError("private connection details")
        self.add_action(handler=fail)
        cid = self.request()["confirmation_id"]
        with self.assertLogs(registry.logger, "ERROR"):
            result = registry.confirm(cid, True, "owner")
        self.assertEqual(result["status"], "error")
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(registry.confirm(cid, True, "owner")["status"], "error")
        self.assertEqual(len(self.calls), 1)

    def test_nonserialisable_result_is_error(self):
        self.add_action("green", lambda payload: {"bad": object()})
        with self.assertLogs(registry.logger, "ERROR"):
            self.assertEqual(self.request()["status"], "error")

    def test_catalog_does_not_expose_handler_or_mutable_schema(self):
        self.add_action()
        catalog = registry.list_actions()
        json.dumps(catalog)
        self.assertNotIn("handler", catalog[0])
        catalog[0]["parameters"]["type"] = "array"
        self.assertEqual(registry.list_actions()[0]["parameters"]["type"], "object")
        with self.assertRaises(ValueError):
            self.add_action()

    def test_demo_is_explicitly_fake_and_never_sends(self):
        register_demo_actions()
        pending = registry.execute("send_reminder", {"customer": "Demo Cafe", "text": "Hello"}, "owner")
        self.assertEqual(pending["summary"], 'Send this reminder to "Demo Cafe"?\n\n> Hello')
        result = registry.confirm(pending["confirmation_id"], True, "owner")["result"]
        self.assertFalse(result["sent_externally"])
        self.assertEqual({a["name"] for a in registry.list_actions()}, {"send_reminder"})


if __name__ == "__main__":
    unittest.main()
