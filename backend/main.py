import json
from datetime import date
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from backend.actions.borrowing import register_borrowing_actions
from backend.actions.lookups import register_lookup_actions
from backend.actions.forecast import register_forecast_action
from backend.actions.owner_only import register_owner_only_actions
from backend.actions.planning import register_planning_actions
from backend.actions.reminders import register_reminder_actions
from backend.actions import registry
from backend.dashboard import read_dashboard
from backend.agent import ConversationTooLarge, stream_agent
from backend import auth
from backend.actions.bookkeeping import register_bookkeeping_actions
from backend.insights import workspace, register_insight_actions

# Both checks are set on the whole app, so they hold for every endpoint below
# and for any added later. Only /health, /login and /me answer without a sign-in.
app = FastAPI(title="BillBoard API",
              dependencies=[Depends(auth.same_origin), Depends(auth.require_owner)])
# Answer only under a local name, so a website cannot reach the API under its own (DNS rebinding).
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
register_lookup_actions()
register_forecast_action()
register_owner_only_actions()
register_reminder_actions()
register_planning_actions()
register_borrowing_actions()
register_bookkeeping_actions()
register_insight_actions()
app.include_router(auth.router)
app.add_exception_handler(RequestValidationError, auth.invalid_request)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    messages: list[ChatMessage] = Field(
        min_length=1, max_length=20
    )


class DeleteChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


@app.post("/chats/delete")
def delete_chat(request: DeleteChatRequest) -> dict:
    return registry.cancel_chat_requests(request.session_id)


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


@app.get("/dashboard")
def dashboard(days: int = Query(30, ge=1, le=60)) -> JSONResponse:
    # Read-only, and never through the registry or the model: see backend/dashboard.py.
    try:
        content = read_dashboard(days)
    except FileNotFoundError as missing:
        raise HTTPException(
            status_code=503,
            detail="The company's books are not built. Build them with: python -m backend.bank.seed",
        ) from missing
    # The company's figures are not for a browser or a proxy to keep.
    return JSONResponse(content, headers={"Cache-Control": "no-store"})


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
    except ConversationTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
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

        except ConversationTooLarge as problem:
            # Caught before ValueError: the owner should start a new chat,
            # not wonder what is wrong with the model.
            yield json.dumps({
                "type": "error",
                "message": str(problem),
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


class BookkeepingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal['set_opening_balance','record_cash_transaction','reverse_cash_transaction','set_reporting_date','record_existing_loan','cancel_recorded_loan']
    arguments: dict
    session_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


@app.post('/bookkeeping')
def bookkeeping(request: BookkeepingRequest):
    # The form has no privileged write path: it requests the same Yellow
    # action as Bill and receives a normal confirmation card in the chat.
    return registry.execute(request.action, request.arguments, request.session_id)


@app.get('/workspace')
def read_workspace():
    return JSONResponse(workspace(), headers={"Cache-Control": "no-store"})


@app.get('/payment-record-options')
def payment_record_options(kind: Literal['invoice', 'loan', 'expense'],
                           payment_date: date, query: str = Query('', max_length=120)):
    from backend.bank.db import connect
    from backend.bank.ledger import obligations
    from backend.actions.lookups import open_invoices
    with connect() as conn:
        conn.execute('BEGIN')
        if kind == 'invoice':
            from backend.bank.db import reporting_date
            rows = open_invoices(conn, reporting_date(conn))
            rows = [row for row in rows if row['issue_date'] <= payment_date.isoformat()
                    and query.casefold() in f"{row['name']} {row['invoice_id']}".casefold()]
            rows.sort(key=lambda row: (row['name'], row['due_date'], row['invoice_id']))
            result = {'invoices': [{key: row[key] for key in ('invoice_id', 'name', 'amount', 'due_date')} for row in rows[:50]],
                      'bills': [], 'total': len(rows)}
        else:
            rows = [row for row in obligations(conn)
                    if (row['category'] == 'loan') == (kind == 'loan')
                    and query.casefold() in f"{row['label']} {row['obligation_id']}".casefold()]
            rows.sort(key=lambda row: (row['due_date'], row['obligation_id']))
            result = {'invoices': [], 'bills': [{key: row[key] for key in ('obligation_id', 'label', 'amount', 'due_date', 'category')} for row in rows[:50]],
                      'total': len(rows)}
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


class FundingRequest(BaseModel):
    amount: float = Field(gt=0, le=1000000000, allow_inf_nan=False)
    days: int = Field(ge=1, le=365)
    quoted_annual_rate: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)


@app.post('/funding-comparison')
def funding_comparison(request: FundingRequest):
    from backend.actions.borrowing import compare_options
    return compare_options(request.amount, request.days, request.quoted_annual_rate)


class FundingPreviewRequest(FundingRequest):
    model_config = ConfigDict(extra='forbid')
    plan_id: str = Field(pattern=r'^[a-f0-9]{24}$')


@app.post('/funding-preview')
def funding_preview(request: FundingPreviewRequest):
    from backend.funding_plans import preview
    try:
        return preview(request.amount, request.days, request.plan_id, request.quoted_annual_rate)
    except registry.ActionError as problem:
        raise HTTPException(status_code=400, detail=str(problem)) from problem
    except Exception as problem:
        raise HTTPException(status_code=503, detail='Cash preview is unavailable. Check the forecast data and try again.') from problem
