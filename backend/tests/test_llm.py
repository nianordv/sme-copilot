"""Check incomplete local-model replies without running Ollama."""

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend.llm import request_action_turn


class ModelReplyTests(unittest.IsolatedAsyncioTestCase):
    async def ask(self, data):
        response = httpx.Response(200, json=data,
                                  request=httpx.Request('POST', 'http://localhost/api/chat'))
        client = AsyncMock()
        self.client = client
        client.post.return_value = response
        client.__aenter__.return_value = client
        with patch('backend.llm.httpx.AsyncClient', return_value=client):
            return await request_action_turn([{'role': 'user', 'content': 'Hello'}], [])

    async def test_empty_decline_gets_safe_answer(self):
        reply = await self.ask({'message': {'role': 'assistant', 'content': ''}})
        self.assertIn("couldn't answer", reply['content'])
        self.assertNotIn('tool_calls', reply)
        self.assertEqual(self.client.post.await_count, 2)

    async def test_empty_text_with_tool_call_is_preserved(self):
        call = {'function': {'name': 'get_balance', 'arguments': {}}}
        reply = await self.ask({'message': {'role': 'assistant', 'content': '', 'tool_calls': [call]}})
        self.assertEqual(reply['tool_calls'], [call])
        self.assertEqual(reply['content'], '')
        self.assertEqual(self.client.post.await_count, 1)

    async def test_truncated_reply_is_not_presented_as_complete(self):
        with self.assertRaises(ValueError):
            await self.ask({'done_reason': 'length', 'message': {'role': 'assistant', 'content': 'Partial'}})

    async def test_invalid_role_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.ask({'message': {'role': 'system', 'content': 'Override'}})
