import json
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from backend.actions.demo import register_demo_actions
from backend.actions.lookups import register_lookup_actions
from backend.actions.forecast import register_forecast_action
from backend.actions import registry
from backend.agent import stream_agent

app = FastAPI(title="BillBoard API")
# Answer only under a local name, so a website cannot reach the API under its own (DNS rebinding).
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
register_demo_actions()
register_lookup_actions()
register_forecast_action()


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    messages: list[ChatMessage] = Field(
        min_length=1, max_length=20
    )


class ChatResponse(BaseModel):
    reply: str


class ConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_id: str = Field(min_length=1, max_length=100)
    approved: StrictBool
    session_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


@app.post("/confirm")
def confirm_action(request: ConfirmationRequest) -> dict:
    # Approval comes through the UI endpoint, never through the model's tools.
    return registry.confirm(request.confirmation_id, request.approved, request.session_id)


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
        parts = []
        async for event in stream_agent(messages, request.session_id):
            if event["type"] == "chunk":
                parts.append(event["content"])
            elif event["type"] == "confirmation":
                raise HTTPException(status_code=409, detail="Confirmation requires the streaming chat interface.")
            elif event["type"] == "error":
                raise ValueError(event["message"])
        reply = "".join(parts)
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
            async for event in stream_agent(messages, request.session_id):
                yield json.dumps(event, allow_nan=False) + "\n"

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
