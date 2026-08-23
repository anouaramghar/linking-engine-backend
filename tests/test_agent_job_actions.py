"""Review-gated agent proposals for costly or interrupting job actions."""

import uuid

from sqlalchemy import delete, func, select

from app.agent_tools import call_tool
from app.models import Article, JobRun, PipelineBatch, PipelineSiteRun, Site
from app.services.authorization import Principal


def _admin() -> Principal:
    return Principal(is_admin=True, source="legacy_env")


def _site(db, tenant_id: int, suffix: str) -> Site:
    site = Site(
        tenant_id=tenant_id,
        name=f"Agent jobs {suffix}",
        base_url=f"https://agent-jobs-{suffix}-{uuid.uuid4().hex[:8]}.example.com",
        platform="html",
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def _cleanup(db, *, batch_ids: list[int] | None = None, site_ids: list[int] | None = None) -> None:
    if batch_ids:
        db.execute(delete(PipelineBatch).where(PipelineBatch.id.in_(batch_ids)))
    if site_ids:
        db.execute(delete(Site).where(Site.id.in_(site_ids)))
    db.commit()


def test_site_job_preview_is_exact_and_stale_confirmation_is_refused(client, db, site):
    db.add(
        Article(
            site_id=site.id,
            url=f"{site.base_url}/scope",
            title="Scope article",
            content_text="scope",
        )
    )
    db.commit()

    preview = call_tool(
        db,
        _admin(),
        "preview_site_job",
        {"site_id": site.id, "kind": "analysis"},
    )

    assert preview["ready"] is True
    assert preview["scope"]["active_article_count"] == 1
    assert preview["proposal"] == {
        "kind": "site_job_start",
        "risk": "sensitive",
        "method": "POST",
        "endpoint": f"/api/v1/suggestions/{site.id}",
        "payload": {"expected_active_job_run_ids": []},
        "impact": {
            "site_count": 1,
            "active_article_count": 1,
            "active_internal_link_count": 0,
            "active_suggestion_count": 0,
        },
    }

    # Another request queues the same kind before this card is confirmed.
    db.add(JobRun(site_id=site.id, kind="analysis", status="queued"))
    db.commit()
    stale = client.post(
        f"/api/v1/suggestions/{site.id}",
        json=preview["proposal"]["payload"],
    )
    assert stale.status_code == 409
    assert "changed after this action was previewed" in stale.json()["detail"]


def test_site_job_preview_does_not_stage_duplicate_work(db, site):
    run = JobRun(site_id=site.id, kind="ingestion", status="running")
    db.add(run)
    db.commit()
    db.refresh(run)

    preview = call_tool(
        db,
        _admin(),
        "preview_site_job",
        {"site_id": site.id, "kind": "ingestion"},
    )

    assert preview["ready"] is False
    assert preview["active_same_kind_job_run_ids"] == [run.id]
    assert "proposal" not in preview


def test_pipeline_batch_preview_binds_all_selected_sites_to_idle_state(client, db, site):
    second = _site(db, site.tenant_id, "batch-second")
    preview = call_tool(
        db,
        _admin(),
        "preview_pipeline_batch",
        {"site_ids": [second.id, site.id]},
    )

    proposal = preview["proposal"]
    assert proposal["kind"] == "pipeline_batch_start"
    assert proposal["risk"] == "sensitive"
    assert proposal["payload"] == {
        "site_ids": sorted([site.id, second.id]),
        "expected_active_job_run_ids": [],
    }

    db.add(JobRun(site_id=second.id, kind="ingestion", status="queued"))
    db.commit()
    before = db.scalar(select(func.count()).select_from(PipelineBatch)) or 0
    stale = client.post("/api/v1/pipelines/batches", json=proposal["payload"])
    after = db.scalar(select(func.count()).select_from(PipelineBatch)) or 0
    assert stale.status_code == 409
    assert after == before
    _cleanup(db, site_ids=[second.id])


def test_pipeline_retry_confirmation_is_bound_to_stage_and_retry_count(client, db, site):
    batch = PipelineBatch(status="failed")
    db.add(batch)
    db.flush()
    item = PipelineSiteRun(
        batch_id=batch.id,
        site_id=site.id,
        status="failed",
        stage="analysis",
        retry_count=1,
        error="model unavailable",
    )
    db.add(item)
    db.commit()
    db.refresh(batch)

    preview = call_tool(
        db,
        _admin(),
        "preview_pipeline_retry",
        {"batch_id": batch.id, "site_id": site.id},
    )
    assert preview["proposal"]["payload"] == {
        "expected_batch_status": "failed",
        "expected_site_status": "failed",
        "expected_stage": "analysis",
        "expected_retry_count": 1,
    }

    item.retry_count = 2
    db.commit()
    stale = client.post(
        f"/api/v1/pipelines/batches/{batch.id}/sites/{site.id}/retry",
        json=preview["proposal"]["payload"],
    )
    assert stale.status_code == 409
    assert "changed after it was previewed" in stale.json()["detail"]
    _cleanup(db, batch_ids=[batch.id])


def test_pipeline_cancel_confirmation_is_bound_to_exact_unfinished_sites(client, db, site):
    second = _site(db, site.tenant_id, "cancel-second")
    batch = PipelineBatch(status="running")
    db.add(batch)
    db.flush()
    db.add(
        PipelineSiteRun(
            batch_id=batch.id,
            site_id=site.id,
            status="analysis_running",
            stage="analysis",
        )
    )
    db.commit()
    db.refresh(batch)

    preview = call_tool(
        db,
        _admin(),
        "preview_pipeline_cancel",
        {"batch_id": batch.id},
    )
    assert preview["proposal"]["payload"]["expected_sites"] == [
        {
            "site_id": site.id,
            "status": "analysis_running",
            "stage": "analysis",
            "ingestion_job_run_id": None,
            "analysis_job_run_id": None,
        }
    ]

    db.add(
        PipelineSiteRun(
            batch_id=batch.id,
            site_id=second.id,
            status="queued",
            stage="ingestion",
        )
    )
    db.commit()
    stale = client.post(
        f"/api/v1/pipelines/batches/{batch.id}/cancel",
        json=preview["proposal"]["payload"],
    )
    assert stale.status_code == 409
    assert "sites changed after" in stale.json()["detail"]
    _cleanup(db, batch_ids=[batch.id], site_ids=[second.id])
