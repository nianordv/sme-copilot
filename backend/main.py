from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from backend.llm import generate_reply, stream_reply
import json
from fastapi.responses import StreamingResponse

app = FastAPI(title="Rightsize API")


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(
        min_length=1, max_length=20
    )


class ChatResponse(BaseModel):
    reply: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    if request.messages[-1].role != "user":
        raise HTTPException(
            status_code=422,
            detail="The final message must be from the user.",
        )

    messages = [
        message.model_dump() for message in request.messages
    ]

    try:
        reply = await generate_reply(messages)
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail="The model took too long to respond. Try again.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503,
            detail="Cannot connect to Ollama. Check that it is running.",
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail="Ollama rejected the request. Check the model setup.",
        ) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Ollama returned an invalid reply.",
        ) from exc

    return ChatResponse(reply=reply)


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    if request.messages[-1].role != "user":
        raise HTTPException(
            status_code=422,
            detail="The final message must be from the user.",
        )

    messages = [
        message.model_dump() for message in request.messages
    ]

    async def events():
        try:
            async for chunk in stream_reply(messages):
                yield json.dumps({
                    "type": "chunk",
                    "content": chunk,
                }) + "\n"

            yield json.dumps({"type": "done"}) + "\n"

        except httpx.TimeoutException:
            yield json.dumps({
                "type": "error",
                "message": "The model took too long to respond.",
            }) + "\n"

        except httpx.RequestError:
            yield json.dumps({
                "type": "error",
                "message": "Lost connection to Ollama.",
            }) + "\n"

        except httpx.HTTPStatusError:
            yield json.dumps({
                "type": "error",
                "message": "Ollama rejected the request.",
            }) + "\n"

        except (ValueError, KeyError, TypeError, AttributeError):
            yield json.dumps({
                "type": "error",
                "message": "The model returned an invalid or incomplete reply.",
            }) + "\n"

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )