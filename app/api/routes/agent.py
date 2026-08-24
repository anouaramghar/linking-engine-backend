"""Dashboard assistant endpoints.

Both chat endpoints are a thin shell over ``agent_service``: authenticate,
bound the transcript, run the tool loop, serialize. They are protected by the
same API key as every other route and perform no writes — the loop's tools are
the shared read-only registry.

``/chat`` answers once, when the whole turn is finished. ``/chat/stream`` sends
the same run as Server-Sent Events while it happens, because a turn is several
model calls long and the panel had nothing to show for any of it. The stream is
an additional way to read one answer, not a second answer: it ends with the
event that carries exactly the body ``/chat`` would have returned.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_api_key
from app.config import settings
from app.ml.llm.openrouter import OpenRouterError
from app.schemas.agent import AgentChatRequest, AgentChatResponse, AgentStatusResponse
from app.services.agent_service import (
    UNAVAILABLE_DETAIL,
    AgentEvent,
    AgentUnavailable,
    AssistantReply,
    TextDelta,
    ToolInvocation,
    answer_question,
    stream_answer,
)
from app.services.authorization import Principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])

#: Said to the operator when the provider fails. The provider's own words stay
#: in the log, because they can carry key and account detail.
PROVIDER_FAILED_DETAIL = "Mesh is temporarily unavailable"


@router.get("/status", response_model=AgentStatusResponse)
def agent_status(principal: Principal = Depends(require_api_key)) -> AgentStatusResponse:
    from app.ml.llm.agent import is_configured, provider_host

    # `provider` is the host, not a brand name we map to: an operator debugging
    # "why is chat down" needs to know which endpoint is actually being called.
    return AgentStatusResponse(
        configured=is_configured(),
        model=settings.agent_model,
        provider=provider_host(),
    )


def _bounded_history(payload: AgentChatRequest) -> list[dict[str, str]]:
    """The client's transcript, cut to the bound this deployment allows.

    Enforced here rather than only in settings so a client cannot negotiate
    around it with a long tail of tiny turns.
    """
    history = [{"role": turn.role, "content": turn.content} for turn in payload.history]
    max_history_messages = settings.agent_max_history_turns * 2
    return history[-max_history_messages:] if max_history_messages else []


def _body(result: AssistantReply) -> AgentChatResponse:
    """The wire body for a finished turn, shared by both endpoints."""
    return AgentChatResponse(
        reply=result.reply,
        tools_used=[
            {"name": t.name, "arguments": t.arguments, "outcome": t.outcome}
            for t in result.tools_used
        ],
        proposals=result.proposals,
    )


@router.post("/chat", response_model=AgentChatResponse)
def chat(
    payload: AgentChatRequest,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> AgentChatResponse:
    try:
        result = answer_question(db, principal, payload.message, _bounded_history(payload))
    except AgentUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except OpenRouterError as exc:
        # Rate limit, timeout, or an unusable body. The operator gets the same
        # honest "temporarily unavailable" they get from an unconfigured
        # deployment rather than a 500, and the provider text stays in the log
        # because it can carry key and account detail.
        logger.warning("assistant model call failed: %s", exc)
        raise HTTPException(503, PROVIDER_FAILED_DETAIL) from exc
    return _body(result)


def _frame(name: str, data: dict) -> str:
    """One SSE event. ``json.dumps`` never emits a raw newline inside a string,
    so the whole payload always fits the single ``data:`` line a reply full of
    line breaks would otherwise split across."""
    return f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n"


def _event_frame(event: AgentEvent) -> str:
    if isinstance(event, TextDelta):
        return _frame("delta", {"text": event.text})
    if isinstance(event, ToolInvocation):
        return _frame(
            "tool",
            {"name": event.name, "arguments": event.arguments, "outcome": event.outcome},
        )
    # The turn, finished. Same body as `/chat`, so a client that missed a
    # fragment — or a provider that streamed none — still has the whole answer.
    return f"event: done\ndata: {_body(event).model_dump_json()}\n\n"


@router.post("/chat/stream")
def chat_stream(
    payload: AgentChatRequest,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """The same answer as ``/chat``, sent as the engine produces it."""
    from app.ml.llm.agent import is_configured

    # Refused before the stream opens. Once the first byte of a body is out the
    # status line is already sent, and 503 is no longer available to say this —
    # so the one refusal that is knowable in advance is made in advance.
    if not is_configured():
        raise HTTPException(503, UNAVAILABLE_DETAIL)

    message = payload.message
    history = _bounded_history(payload)

    def events():
        # A comment frame first: it commits the response through any proxy that
        # would otherwise hold the headers waiting for a body.
        yield ": open\n\n"
        try:
            for event in stream_answer(db, principal, message, history):
                yield _event_frame(event)
        except (AgentUnavailable, OpenRouterError) as exc:
            logger.warning("assistant stream failed: %s", exc)
            yield _frame("error", {"detail": PROVIDER_FAILED_DETAIL})
        except Exception:
            # A stream cannot become a 500 after its headers are out, and the
            # panel would otherwise read an unexplained truncation. Say so in
            # the stream; the traceback goes to the log.
            logger.exception("assistant stream failed unexpectedly")
            yield _frame("error", {"detail": PROVIDER_FAILED_DETAIL})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # X-Accel-Buffering: nginx sits in front of this in every deployment and
        # buffers proxied responses by default, which would hold the whole
        # answer back and hand it over at once — a slower version of `/chat`.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
