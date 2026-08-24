"""Race-safe agent proposals for small dashboard-parity actions."""

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import delete, select

from app.agent_tools import call_tool
from app.models import Alert, Article, PoolSourceAuditEvent, Site, Suggestion
from app.services.authorization import Principal


def _admin() -> Principal:
    return Principal(is_admin=True, source="legacy_env")


@pytest.fixture(autouse=True)
def _remove_created_pool_sources(db):
    """Keep generated shared-pool rows from leaking into later account tests."""

    before = set(db.scalars(select(Site.id).where(Site.name.like("Agent pool %"))))
    yield
    created = list(
        db.scalars(select(Site.id).where(Site.name.like("Agent pool %"), Site.id.not_in(before)))
    )
    if created:
        db.execute(delete(PoolSourceAuditEvent).where(PoolSourceAuditEvent.site_id.in_(created)))
        db.execute(delete(Site).where(Site.id.in_(created)))
        db.commit()


def _pool(db, *, approved: bool, quarantined: bool = False, failures: int = 0) -> Site:
    site = Site(
        name=f"Agent pool {uuid.uuid4().hex[:8]}",
        base_url=f"https://agent-pool-{uuid.uuid4().hex[:8]}.wikipedia.org/feed.xml",
        platform="pool",
        crawl_frequency="daily",
        pool_source_approved=approved,
        pool_source_quarantined=quarantined,
        pool_source_consecutive_failures=failures,
        pool_source_quarantined_at=datetime.now(UTC) if quarantined else None,
        pool_source_quarantine_reason="repeated timeout" if quarantined else None,
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def test_alert_preview_binds_the_exact_unread_occurrence(client, db, site):
    alert = Alert(
        site_id=site.id,
        kind="job_failed",
        subject="Analysis failed",
        payload={"job_run_id": 41},
        occurrences=2,
        last_seen_at=datetime.now(UTC),
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    preview = call_tool(
        db,
        _admin(),
        "preview_alert_acknowledgement",
        {"alert_id": alert.id},
    )

    assert preview["ready"] is True
    assert preview["proposal"] == {
        "kind": "alert_acknowledgement",
        "risk": "sensitive",
        "method": "POST",
        "endpoint": f"/api/v1/alerts/{alert.id}/acknowledge",
        "payload": {
            "expected_unacknowledged": True,
            "expected_occurrences": 2,
            "expected_last_seen_at": alert.last_seen_at.isoformat(),
        },
        "context": {
            "alert_id": alert.id,
            "alert_subject": "Analysis failed",
            "alert_kind": "job_failed",
            "site_id": site.id,
            "site_name": site.name,
        },
        "impact": {"alert_count": 1, "occurrence_count": 2},
    }

    response = client.post(
        f"/api/v1/alerts/{alert.id}/acknowledge",
        json=preview["proposal"]["payload"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["acknowledged_at"] is not None


def test_new_alert_occurrence_invalidates_an_older_confirmation(client, db, site):
    alert = Alert(
        site_id=site.id,
        kind="job_failed",
        subject="Crawl failed",
        payload={},
        last_seen_at=datetime.now(UTC),
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    proposal = call_tool(
        db,
        _admin(),
        "preview_alert_acknowledgement",
        {"alert_id": alert.id},
    )["proposal"]

    alert.occurrences += 1
    alert.last_seen_at += timedelta(seconds=1)
    db.commit()

    response = client.post(
        f"/api/v1/alerts/{alert.id}/acknowledge",
        json=proposal["payload"],
    )
    assert response.status_code == 409
    assert "state changed after preview" in response.json()["detail"]


def test_pool_approval_and_reactivation_bind_lifecycle_state(client, db):
    pool = _pool(db, approved=False)
    approval = call_tool(
        db,
        _admin(),
        "preview_pool_source_action",
        {"site_id": pool.id, "action": "approve"},
    )
    assert approval["ready"] is True
    assert approval["proposal"]["method"] == "POST"
    approved = client.post(
        f"/api/v1/sites/{pool.id}/pool-source/approval",
        json=approval["proposal"]["payload"],
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["pool_source_approved"] is True

    db.expire_all()
    pool = db.get(Site, pool.id)
    pool.pool_source_quarantined = True
    pool.pool_source_quarantined_at = datetime.now(UTC)
    pool.pool_source_quarantine_reason = "still unavailable"
    pool.pool_source_consecutive_failures = 3
    db.commit()
    reactivation = call_tool(
        db,
        _admin(),
        "preview_pool_source_action",
        {"site_id": pool.id, "action": "reactivate"},
    )
    assert reactivation["ready"] is True
    assert reactivation["impact"]["consecutive_failure_count"] == 3

    pool.pool_source_consecutive_failures = 4
    db.commit()
    stale = client.post(
        f"/api/v1/sites/{pool.id}/pool-source/reactivate",
        json=reactivation["proposal"]["payload"],
    )
    assert stale.status_code == 409
    assert "state changed after preview" in stale.json()["detail"]


def test_pool_revocation_binds_every_suggestion_it_will_expire(client, db, site):
    pool = _pool(db, approved=True)
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/agent-pool-source",
        title="Source",
        content_text="source",
    )
    target = Article(
        site_id=pool.id,
        url=f"{pool.base_url}#target",
        title="Pool target",
        content_text="target",
    )
    db.add_all([source, target])
    db.flush()
    first = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="hybrid_bm25",
        score=0.8,
        rank_score=0.8,
        status="pending",
    )
    db.add(first)
    db.commit()
    db.refresh(first)

    preview = call_tool(
        db,
        _admin(),
        "preview_pool_source_action",
        {"site_id": pool.id, "action": "revoke"},
    )
    assert preview["proposal"]["method"] == "DELETE"
    assert preview["proposal"]["payload"]["expected_expiring_suggestion_ids"] == [first.id]
    assert preview["impact"]["pending_count"] == 1

    second = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="baseline_cosine",
        score=0.7,
        rank_score=0.7,
        status="approved",
    )
    db.add(second)
    db.commit()
    stale = client.request(
        "DELETE",
        f"/api/v1/sites/{pool.id}/pool-source/approval",
        json=preview["proposal"]["payload"],
    )
    assert stale.status_code == 409
    assert "impact changed after preview" in stale.json()["detail"]

    fresh = call_tool(
        db,
        _admin(),
        "preview_pool_source_action",
        {"site_id": pool.id, "action": "revoke"},
    )["proposal"]
    revoked = client.request(
        "DELETE",
        f"/api/v1/sites/{pool.id}/pool-source/approval",
        json=fresh["payload"],
    )
    assert revoked.status_code == 200, revoked.text
    db.refresh(first)
    db.refresh(second)
    assert [first.status, second.status] == ["expired", "expired"]
