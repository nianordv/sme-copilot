import os

import httpx

import json
from collections.abc import AsyncIterator

OLLAMA_URL = os.getenv(
    "OLLAMA_URL", "http://127.0.0.1:11434"
)
OLLAMA_MODEL = os.getenv(
    "OLLAMA_MODEL", "qwen2.5:7b"
)

SYSTEM_PROMPT = (
    "You are Rightsize, an assistants for small businesses. "
    "Reply clearly and concisely. "
    "You do not currently have access to invoices, bank balances, "
    "or payment forecasts. Do not invent business figures or "
    "claim to have accessed financial data."
)


async def generate_reply(messages: list[dict[str, str]]) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            *messages,
        ],
        "stream": False,
        "options": {
            "num_ctx": 4096,
            "num_predict": 512,
        },
    }

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=5.0)
    ) as client:
        response = await client.post(
            f"{OLLAMA_URL.rstrip('/')}/api/chat",
            json=payload,
        )
        response.raise_for_status()

    data = response.json()
    reply = data["message"]["content"].strip()

    if not reply:
        raise ValueError("Ollama returned an empty reply.")

    return reply


async def stream_reply(
    messages: list[dict[str, str]],
) -> AsyncIterator[str]:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            *messages,
        ],
        "stream": True,
        "options": {
            "num_ctx": 4096,
            "num_predict": 512,
        },
    }

    received_text = False

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=5.0)
    ) as client:
        async with client.stream(
            "POST",
            f"{OLLAMA_URL.rstrip('/')}/api/chat",
            json=payload,
        ) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                if not line.strip():
                    continue

                event = json.loads(line)

                if event.get("error"):
                    raise ValueError("Ollama reported a streaming error.")

                content = event.get("message", {}).get("content", "")

                if not isinstance(content, str):
                    raise ValueError("Ollama returned invalid text.")

                if content:
                    received_text = True
                    yield content

                if event.get("done"):
                    if not received_text:
                        raise ValueError("Ollama returned an empty reply.")
                    return

    raise ValueError("The reply stream ended before completion.")