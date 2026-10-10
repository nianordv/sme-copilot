import json
import unittest
from unittest.mock import AsyncMock, patch

from backend import agent
from backend.actions import registry


def tool_reply(name="get_receivables", arguments=None, count=1):
    return {"role": "assistant", "tool_calls": [
        {"function": {"name": name, "arguments": arguments or {}}}
        for _ in range(count)
    ]}


def final_reply():
    return {"role": "assistant", "content": "Demo total: 1,500 GEL."}


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def test_no_argument_tool_has_explicit_instruction(self):
        action = {"name": "get_receivables", "description": "Demo lookup",
                  "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}
        with patch.object(registry, "list_actions", return_value=[action]):
            tools = agent.tool_catalog()
        self.assertIn("empty object: {}", tools[0]["function"]["description"])

    async def test_action_error_step_shows_reason(self):
        request = AsyncMock(side_effect=[tool_reply(), final_reply()])
        with patch.object(agent, "request_action_turn", request), \
             patch.object(registry, "execute", return_value={"status": "error", "message": "Invalid action arguments."}):
            events = await self.collect()
        self.assertIn("Invalid action arguments.", events[0]["label"])

    async def collect(self):
        return [event async for event in agent.stream_agent(
            [{"role": "user", "content": "Who owes me money?"}], "owner")]

    async def test_action_result_goes_back_to_model(self):
        request = AsyncMock(side_effect=[tool_reply(), final_reply()])
        with patch.object(agent, "request_action_turn", request), \
             patch.object(registry, "execute", return_value={"status": "ok", "result": {"total": 1500}}) as execute:
            events = await self.collect()
        self.assertEqual(request.await_count, 2)
        self.assertEqual(events[1]["content"], "Demo total: 1,500 GEL.")
        execute.assert_called_once_with("get_receivables", {}, "owner")
        tool = request.call_args_list[1].args[0][-1]
        self.assertEqual(tool["role"], "tool")
        self.assertEqual(json.loads(tool["content"])["result"]["total"], 1500)
        self.assertEqual([e["type"] for e in events], ["action", "chunk", "done"])

    async def test_plain_answer_uses_one_model_call(self):
        with patch.object(agent, "request_action_turn", AsyncMock(return_value=final_reply())) as request, \
             patch.object(registry, "execute") as execute:
            events = await self.collect()
        self.assertEqual(request.await_count, 1)
        execute.assert_not_called()
        self.assertEqual([event["type"] for event in events], ["chunk", "done"])

    async def test_text_alongside_tool_call_is_not_displayed(self):
        reply = tool_reply()
        reply["content"] = "The action already succeeded."
        with patch.object(agent, "request_action_turn", AsyncMock(side_effect=[reply, final_reply()])), \
             patch.object(registry, "execute", return_value={"status": "ok", "result": {}}):
            events = await self.collect()
        chunks = [event["content"] for event in events if event["type"] == "chunk"]
        self.assertEqual(chunks, [final_reply()["content"]])

    async def test_empty_answer_is_rejected(self):
        with patch.object(agent, "request_action_turn", AsyncMock(return_value={"role": "assistant", "content": ""})):
            with self.assertRaises(ValueError):
                await self.collect()

    async def test_five_actions_can_be_followed_by_final_answer(self):
        request = AsyncMock(side_effect=[tool_reply(count=5), final_reply()])
        with patch.object(agent, "request_action_turn", request), \
             patch.object(registry, "execute", return_value={"status": "ok", "result": {}}) as execute:
            events = await self.collect()
        self.assertEqual(execute.call_count, 5)
        self.assertEqual(request.await_count, 2)
        self.assertEqual(events[-1]["type"], "done")

    async def test_repeated_requests_stop_at_five(self):
        with patch.object(agent, "request_action_turn", AsyncMock(return_value=tool_reply())), \
             patch.object(registry, "execute", return_value={"status": "ok", "result": {}}) as execute:
            events = await self.collect()
        self.assertEqual(execute.call_count, 5)
        self.assertEqual(events[-1]["type"], "error")

    async def test_batched_requests_cannot_bypass_limit(self):
        with patch.object(agent, "request_action_turn", AsyncMock(return_value=tool_reply(count=8))), \
             patch.object(registry, "execute", return_value={"status": "ok", "result": {}}) as execute:
            await self.collect()
        self.assertEqual(execute.call_count, 5)

    async def test_confirmation_pauses_remaining_calls(self):
        request = AsyncMock(return_value=tool_reply("send_reminder", count=2))
        result = {"status": "needs_confirmation", "confirmation_id": "example", "summary": "Send demo reminder?"}
        with patch.object(agent, "request_action_turn", request), \
             patch.object(registry, "execute", return_value=result) as execute, \
             patch.object(registry, "confirm") as confirm:
            events = await self.collect()
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(request.await_count, 1)
        confirm.assert_not_called()
        self.assertEqual([e["type"] for e in events], ["action", "confirmation", "chunk", "done"])

    async def test_refusal_is_returned_as_data(self):
        request = AsyncMock(side_effect=[tool_reply("transfer_money"), final_reply()])
        with patch.object(agent, "request_action_turn", request), \
             patch.object(registry, "execute", return_value={"status": "refused", "reason": "Prohibited"}):
            events = await self.collect()
        self.assertEqual(events[0]["status"], "refused")
        self.assertEqual(json.loads(request.call_args_list[1].args[0][-1]["content"])["status"], "refused")

    async def test_malformed_call_never_executes(self):
        request = AsyncMock(return_value={"role": "assistant", "tool_calls": [{"function": {"name": "x", "arguments": "bad"}}]})
        with patch.object(agent, "request_action_turn", request), patch.object(registry, "execute") as execute:
            with self.assertRaises(ValueError):
                await self.collect()
        execute.assert_not_called()

    async def test_large_history_stops_before_model_call(self):
        with patch.object(agent, "request_action_turn", AsyncMock()) as request:
            with self.assertRaises(ValueError):
                _ = [event async for event in agent.stream_agent(
                    [{"role": "user", "content": "x" * 25000}], "owner")]
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
