"""Dashboard assistant endpoints.

The chat endpoint is a thin shell over ``agent_service``: authenticate, bound
the transcript, run the tool loop, serialize. It is protected by the same API
key as every other route and performs no writes — the loop's tools are the
shared read-only registry.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_api_key
from app.config import settings
from app.schemas.agent import AgentChatRequest, AgentChatResponse, AgentStatusResponse
from app.services.agent_service import AgentUnavailable, answer_question
from app.services.authorization import Principal

router = APIRouter(prefix="/agent", tags=["agent"])


@router.get("/status", response_model=AgentStatusResponse)
def agent_status(principal: Principal = Depends(require_api_key)) -> AgentStatusResponse:
    from app.ml.llm.openrouter import is_configured

    return AgentStatusResponse(configured=is_configured(), model=settings.agent_model)


@router.post("/chat", response_model=AgentChatResponse)
def chat(
    payload: AgentChatRequest,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> AgentChatResponse:
    history = [{"role": turn.role, "content": turn.content} for turn in payload.history]
    # The transcript bound is enforced here rather than only in settings so a
    # client cannot negotiate around it with a long tail of tiny turns.
    history = history[-settings.agent_max_history_turns :]
    try:
        result = answer_question(db, principal, payload.message, history)
    except AgentUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return AgentChatResponse(
        reply=result.reply,
        tools_used=[
            {"name": t.name, "arguments": t.arguments, "outcome": t.outcome}
            for t in result.tools_used
        ],
        proposals=result.proposals,
    )
