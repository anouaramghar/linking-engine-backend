from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.config import settings
from app.models import AgentActionReceipt, DashboardSession, DashboardUser, Site
from app.services import dashboard_auth
from app.services.agent_action_receipts import execute_receipt
from app.services.agent_action_tokens import (
    new_receipt,
    principal_binding,
    proposal_hash,
    sign_preview_envelope,
)
from app.services.authorization import Principal


@pytest.fixture(autouse=True)
def action_security_config(monkeypatch):
    monkeypatch.setattr(settings, "api_key_pepper", "receipt-test-pepper")
    monkeypatch.setattr(settings, "agent_action_envelope_ttl_seconds", 600)
    monkeypatch.setattr(settings, "agent_action_receipt_ttl_seconds", 300)


@pytest.fixture
def dashboard_operator(db):
    user = DashboardUser(
        telegram_id=8675309,
        display_name="Receipt editor",
        status="approved",
        approved_at=datetime.now(UTC),
        approved_by="test",
        is_admin=True,
    )
    db.add(user)
    db.flush()
    token = dashboard_auth.issue_session(db, user)
    db.commit()
    yield user, token
    db.rollback()
    db.query(AgentActionReceipt).delete()
    db.query(DashboardSession).delete()
    db.query(DashboardUser).filter(DashboardUser.id == user.id).delete()
    db.commit()


def _site_arguments() -> dict:
    return {
        "sites": [
            {
                "name": "Receipt site",
                "base_url": f"https://receipt-{uuid4().hex[:10]}.example.com",
                "platform": "html",
            }
        ]
    }


def test_dashboard_review_issues_receipt_and_original_identity_spends_it_once(
    client, db, dashboard_operator
):
    user, token = dashboard_operator
    principal = Principal(is_admin=True, source="legacy_env")
    arguments = _site_arguments()
    envelope, _ = sign_preview_envelope("preview_site_creation", arguments, principal)
    client.cookies.set(dashboard_auth.SESSION_COOKIE, token)

    preview = client.post("/api/v1/agent-actions/preview", json={"envelope": envelope})
    assert preview.status_code == 200
    body = preview.json()
    assert body["proposal"]["kind"] == "site_create"
    assert body["originating_scope"] == "admin service key"

    issued = client.post(
        "/api/v1/agent-actions/receipts",
        json={"envelope": envelope, "expected_proposal_hash": body["proposal_hash"]},
    )
    assert issued.status_code == 201
    receipt = issued.json()["receipt"]
    assert receipt.startswith("lmar_")

    outcome = execute_receipt(db, principal, receipt)
    assert outcome["message"].startswith("Connected Receipt site")
    created = db.query(Site).filter(Site.base_url == arguments["sites"][0]["base_url"]).one()
    db.delete(created)
    db.commit()

    with pytest.raises(HTTPException, match="already been used") as replay:
        execute_receipt(db, principal, receipt)
    assert replay.value.status_code == 409
    row = db.query(AgentActionReceipt).one()
    assert row.execution_status == "succeeded"
    assert row.confirmed_by_user_id == user.id


def test_receipt_refuses_another_mcp_identity(db, dashboard_operator):
    user, _ = dashboard_operator
    owner = Principal(is_admin=False, source="db", tenant_id=7, key_id=12)
    plaintext, secret_hash = new_receipt()
    proposal = {
        "tool": "preview_suggestion_review",
        "kind": "review_suggestion",
        "risk": "reversible",
        "method": "PUT",
        "endpoint": "/api/v1/suggestions/999",
        "payload": {"status": "approved", "expected_status": "pending"},
    }
    db.add(
        AgentActionReceipt(
            receipt_hash=secret_hash,
            principal_binding=principal_binding(owner),
            proposal=proposal,
            proposal_hash=proposal_hash(proposal),
            action_kind="review_suggestion",
            confirmed_by_user_id=user.id,
            confirmed_by_telegram_id=str(user.telegram_id),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    db.commit()

    intruder = Principal(is_admin=False, source="db", tenant_id=7, key_id=13)
    with pytest.raises(HTTPException, match="different MCP identity") as denied:
        execute_receipt(db, intruder, plaintext)
    assert denied.value.status_code == 403
    assert db.query(AgentActionReceipt).one().consumed_at is None


def test_action_routes_require_a_live_dashboard_session(client):
    principal = Principal(is_admin=True, source="legacy_env")
    envelope, _ = sign_preview_envelope("preview_site_creation", _site_arguments(), principal)
    response = client.post("/api/v1/agent-actions/preview", json={"envelope": envelope})
    assert response.status_code == 401
