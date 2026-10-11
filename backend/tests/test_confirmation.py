"""Check approval through the HTTP boundary without running Ollama."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend import agent
from backend.main import app
from backend.actions import registry
from backend.tests.helpers import use_temporary_database


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.actions = patch.dict(registry._actions, {}, clear=True)
        self.actions.start()
        self.addCleanup(self.actions.stop)
        use_temporary_database(self)
        self.calls = []

        def reminder(customer, text):
            self.calls.append((customer, text))
            return {"customer": customer, "simulated": True, "sent_externally": False}

        registry.register(registry.Action(
            "send_reminder", "Test reminder", {"type": "object"}, "yellow", reminder))
        self.client = TestClient(app, base_url="http://127.0.0.1:8000")
        self.addCleanup(self.client.close)

    def request(self):
        result = registry.execute("send_reminder", {"customer": "Demo Cafe", "text": "Please pay."}, "owner")
        return {"confirmation_id": result["confirmation_id"], "approved": True, "session_id": "owner"}

    def test_yes_runs_exact_stored_call_once(self):
        request = self.request()
        self.assertEqual(self.calls, [])
        response = self.client.post("/confirm", json=request)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(self.calls, [("Demo Cafe", "Please pay.")])
        self.assertEqual(self.client.post("/confirm", json=request).json()["status"], "error")
        self.assertEqual(len(self.calls), 1)

    def test_no_declines_and_consumes_request(self):
        request = self.request()
        request["approved"] = False
        self.assertEqual(self.client.post("/confirm", json=request).json()["status"], "refused")
        request["approved"] = True
        self.assertEqual(self.client.post("/confirm", json=request).json()["status"], "error")
        self.assertEqual(self.calls, [])

    def test_other_session_cannot_consume_request(self):
        request = self.request()
        request["session_id"] = "other"
        self.assertEqual(self.client.post("/confirm", json=request).json()["status"], "error")
        request["session_id"] = "owner"
        self.assertEqual(self.client.post("/confirm", json=request).json()["status"], "ok")

    def test_foreign_host_name_cannot_approve(self):
        request = self.request()
        response = self.client.post("/confirm", json=request, headers={"Host": "evil.example:8000"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, [])
        # The request is still waiting: the owner's own press works afterwards.
        self.assertEqual(self.client.post("/confirm", json=request).json()["status"], "ok")

    def test_approval_must_be_boolean_and_arguments_cannot_be_replaced(self):
        request = self.request()
        for value in ("yes", "true", 1, None):
            response = self.client.post("/confirm", json={**request, "approved": value})
            self.assertEqual(response.status_code, 422)
        response = self.client.post("/confirm", json={**request, "arguments": {"text": "Changed"}})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.calls, [])

    def test_unavailable_request_is_reported_without_running(self):
        request = self.request()
        request["confirmation_id"] = "never-issued"
        response = self.client.post("/confirm", json=request)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(self.calls, [])

    def test_typed_yes_cannot_approve_even_if_model_requests_it(self):
        request = self.request()
        model = AsyncMock(side_effect=[
            {"role": "assistant", "tool_calls": [{"function": {
                "name": "confirm", "arguments": request}}]},
            {"role": "assistant", "content": "Use the approval buttons."},
        ])
        with patch.object(agent, "request_action_turn", model):
            response = self.client.post("/chat/stream", json={
                "session_id": "owner", "messages": [{"role": "user", "content": "yes, send it"}]})
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(events[0]["status"], "error")
        self.assertEqual(self.calls, [])
        self.assertNotIn("confirm", [action["name"] for action in registry.list_actions()])
        # The request is still waiting: only the owner's press approves it.
        self.assertEqual(self.client.post("/confirm", json=request).json()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
