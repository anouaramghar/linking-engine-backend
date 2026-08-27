"""Human review channel for MCP-staged actions.

These routes deliberately sit outside the API-key router loop: a signed MCP
envelope is not enough. A live, approved dashboard session must inspect and
confirm it before any executable authority is minted.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.agent_tools import REGISTRY, call_tool, error_of
from app.api.deps import get_db
from app.api.routes.auth import require_dashboard_session
from app.config import settings
from app.models import AgentActionReceipt, DashboardUser
from app.schemas.agent_action import (
    AgentActionEnvelopeIn,
    AgentActionPreviewOut,
    AgentActionReceiptIn,
    AgentActionReceiptOut,
)
from app.services.agent_action_tokens import (
    new_receipt,
    principal_from_binding,
    proposal_hash,
    verify_preview_envelope,
)

router = APIRouter(prefix="/agent-actions", tags=["agent-actions"])


def _preview(db: Session, token: str) -> tuple[dict, dict, bool]:
    envelope = verify_preview_envelope(token)
    tool_name = envelope.get("tool")
    tool = REGISTRY.get(tool_name)
    if tool is None:
        raise HTTPException(400, "this MCP action tool is no longer available")
    result = call_tool(
        db,
        principal_from_binding(envelope["principal"]),
        tool_name,
        envelope["arguments"],
    )
    failure = error_of(result)
    if failure is not None:
        raise HTTPException(int(result.get("status", 409)), failure)
    raw = result.get("proposal")
    if not isinstance(raw, dict):
        raise HTTPException(400, "this MCP tool did not stage an executable action")
    proposal = {"tool": tool_name, **raw}
    return envelope, proposal, tool.admin_only


def _scope_label(binding: dict) -> str:
    if binding.get("operator_id"):
        return f"operator {binding['operator_id']}"
    if binding.get("key_id"):
        return f"API key #{binding['key_id']}"
    if binding.get("tenant_id"):
        return f"scope #{binding['tenant_id']}"
    return "admin service key"


@router.post("/preview", response_model=AgentActionPreviewOut)
def preview_agent_action(
    payload: AgentActionEnvelopeIn,
    db: Session = Depends(get_db),
    _: DashboardUser = Depends(require_dashboard_session),
) -> AgentActionPreviewOut:
    envelope, proposal, requires_admin = _preview(db, payload.envelope)
    return AgentActionPreviewOut(
        proposal=proposal,
        proposal_hash=proposal_hash(proposal),
        envelope_expires_at=datetime.fromtimestamp(envelope["exp"], UTC),
        originating_scope=_scope_label(envelope["principal"]),
        requires_admin=requires_admin,
    )


@router.post("/receipts", response_model=AgentActionReceiptOut, status_code=201)
def issue_agent_action_receipt(
    payload: AgentActionReceiptIn,
    db: Session = Depends(get_db),
    user: DashboardUser = Depends(require_dashboard_session),
) -> AgentActionReceiptOut:
    envelope, proposal, requires_admin = _preview(db, payload.envelope)
    current_hash = proposal_hash(proposal)
    if current_hash != payload.expected_proposal_hash:
        raise HTTPException(
            409,
            "the action changed after review; reopen the MCP link and review the current preview",
        )
    if requires_admin and not user.is_admin:
        raise HTTPException(403, "a dashboard admin must confirm this action")

    plaintext, receipt_hash = new_receipt()
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.agent_action_receipt_ttl_seconds)
    db.add(
        AgentActionReceipt(
            receipt_hash=receipt_hash,
            principal_binding=envelope["principal"],
            proposal=proposal,
            proposal_hash=current_hash,
            action_kind=str(proposal.get("kind", "unknown")),
            requires_admin=requires_admin,
            confirmed_by_user_id=user.id,
            confirmed_by_telegram_id=str(user.telegram_id),
            expires_at=expires_at,
        )
    )
    db.commit()
    return AgentActionReceiptOut(
        receipt=plaintext,
        expires_at=expires_at,
        proposal_hash=current_hash,
    )
