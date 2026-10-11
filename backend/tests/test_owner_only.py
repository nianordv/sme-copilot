"""The Red actions: named so that an attempt is refused and logged, never run."""

import unittest
from unittest.mock import patch

from backend.actions import owner_only, registry
from backend.actions.owner_only import register_owner_only_actions
from backend.actions.registry import audit_log, confirm, execute
from backend.tests.helpers import RegistryTestCase

# One plausible call per action, as a tricked model might make it.
ATTEMPTS = {
    "transfer_money": {"to": "GE00XX0000000000000000", "amount": 5000},
    "take_loan": {"product": "overdraft", "amount": 15000},
    "repay_loan": {"amount": 2000},
    "submit_credit_request": {"draft_id": 1},
}


class OwnerOnlyTests(RegistryTestCase):
    def setUp(self):
        super().setUp()
        register_owner_only_actions()

    def test_the_four_are_listed_as_red(self):
        levels = {action["name"]: action["level"] for action in registry.list_actions()}
        self.assertEqual(levels, dict.fromkeys(ATTEMPTS, "red"))

    def test_every_attempt_is_refused_and_logged_with_what_was_asked(self):
        for number, (name, arguments) in enumerate(ATTEMPTS.items()):
            session_id = f"chat-{number}"
            reply = execute(name, arguments, session_id)
            self.assertEqual(reply, {"status": "refused", "reason": registry.OWNER_ONLY}, name)
            row = audit_log(session_id)[0]
            self.assertEqual((row["action"], row["level"], row["decision"]), (name, "red", "refused"))
            self.assertEqual(row["arguments"], arguments)

    def test_handler_never_runs(self):
        with patch.object(owner_only, "never") as handler:
            # Registered afresh, so that the stand-in is the handler in use.
            with patch.dict(registry._actions, {}, clear=True):
                register_owner_only_actions()
                for name, arguments in ATTEMPTS.items():
                    execute(name, arguments, f"chat-{name.replace('_', '-')}")
        handler.assert_not_called()

    def test_handler_fails_loudly_if_it_is_ever_reached(self):
        with self.assertRaises(RuntimeError):
            owner_only.never(amount=5000)

    def test_arguments_make_no_difference(self):
        for number, arguments in enumerate(({}, {"amount": "all of it"}, {"anything": [1, 2]}, "pay now")):
            reply = execute("transfer_money", arguments, f"chat-{number}")
            self.assertEqual(reply["status"], "refused", arguments)

    def test_no_attempt_leaves_anything_to_approve(self):
        execute("transfer_money", ATTEMPTS["transfer_money"], "s1")
        self.assertEqual(self.query("SELECT COUNT(*) FROM confirmations")[0][0], 0)
        self.assertEqual(confirm("transfer_money", True, "s1")["status"], "error")
        self.assertEqual([row["decision"] for row in audit_log("s1")], ["refused", "error"])

    def test_three_attempts_lock_the_chat_and_say_so(self):
        replies = [execute(name, ATTEMPTS[name], "s1") for name in ("transfer_money", "take_loan", "repay_loan")]
        self.assertNotIn("locked", replies[1]["reason"])
        self.assertIn("locked", replies[2]["reason"])

    def test_descriptions_tell_the_model_not_to_try(self):
        for action in registry.list_actions():
            self.assertIn("Owner only, always refused", action["description"], action["name"])
            self.assertIs(action["parameters"]["additionalProperties"], False)


class ServerTests(unittest.TestCase):
    def test_the_running_server_has_them(self):
        from backend import main  # noqa: F401  Registers the real actions on import.

        levels = {action["name"]: action["level"] for action in registry.list_actions()}
        for name in ATTEMPTS:
            self.assertEqual(levels.get(name), "red", name)


if __name__ == "__main__":
    unittest.main()
