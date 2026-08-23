"""Atomic claim and execution of human-issued MCP action receipts."""

import logging
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentActionReceipt, DashboardUser
from app.services.agent_action_executor import execute_proposal
from app.services.agent_action_tokens import hash_receipt, principal_binding, proposal_hash
from app.services.authorization import Principal

logger = logging.getLogger(__name__)


def execute_receipt(db: Session, principal: Principal, plaintext: str) -> dict:
    """Spend before dispatch, so a crash or failed action can never replay."""
    now = datetime.now(UTC)
    row = db.scalar(
        select(AgentActionReceipt)
        .where(AgentActionReceipt.receipt_hash == hash_receipt(plaintext))
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "action receipt not found")
    if row.consumed_at is not None:
        raise HTTPException(409, "action receipt has already been used")
    if row.expires_at <= now:
        raise HTTPException(
            410, "action receipt has expired; confirm a fresh preview in the dashboard"
        )
    if row.principal_binding != principal_binding(principal):
        raise HTTPException(403, "action receipt belongs to a different MCP identity")
    if proposal_hash(row.proposal) != row.proposal_hash:
        raise HTTPException(409, "action receipt integrity check failed")
    user = db.get(DashboardUser, row.confirmed_by_user_id)
    if user is None or user.status != "approved":
        raise HTTPException(403, "the confirming dashboard user is no longer approved")
    if row.requires_admin and not user.is_admin:
        raise HTTPException(403, "the confirming user is no longer a dashboard admin")

    receipt_id = row.id
    actor = f"telegram:{row.confirmed_by_telegram_id}"
    proposal = row.proposal
    row.consumed_at = now
    row.execution_status = "executing"
    db.commit()

    try:
        outcome = execute_proposal(db, principal, proposal, actor=actor)
    except HTTPException as error:
        db.rollback()
        failed = db.get(AgentActionReceipt, receipt_id)
        if failed is not None:
            failed.execution_status = "failed"
            failed.executed_at = datetime.now(UTC)
            failed.execution_error = str(error.detail)[:10_000]
            db.commit()
        raise
    except Exception as error:  # noqa: BLE001 - fixed client error, full server log
        db.rollback()
        logger.exception("agent action receipt %s failed unexpectedly", receipt_id)
        failed = db.get(AgentActionReceipt, receipt_id)
        if failed is not None:
            failed.execution_status = "failed"
            failed.executed_at = datetime.now(UTC)
            failed.execution_error = "action failed unexpectedly"
            db.commit()
        raise HTTPException(500, "confirmed action failed unexpectedly") from error

    succeeded = db.get(AgentActionReceipt, receipt_id)
    if succeeded is not None:
        succeeded.execution_status = "succeeded"
        succeeded.executed_at = datetime.now(UTC)
        succeeded.execution_result = outcome
        db.commit()
    return {"receipt_id": receipt_id, **outcome}
