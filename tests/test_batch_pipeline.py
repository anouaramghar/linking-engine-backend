import uuid

import pytest
from sqlalchemy import delete

from app.api.routes import pipelines as pipeline_routes
from app.models import JobRun, PipelineBatch, PipelineSiteRun, Site
from app.tasks import pipeline as pipeline_tasks


def _site(db, suffix: str, platform: str = "html") -> Site:
    site = Site(
        name=f"Pipeline {suffix}",
        base_url=f"https://pipeline-{suffix}-{uuid.uuid4().hex[:8]}.example.com",
        platform=platform,
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def _fake_enqueue(db, site_id, kind, _task, job_timeout, task_kwargs=None):
    assert job_timeout == (3600 if kind == "ingestion" else 7200)
    assert isinstance(task_kwargs["batch_site_run_id"], int)
    run = JobRun(
        site_id=site_id,
        kind=kind,
        status="queued",
        queue_job_id=f"pipeline-{kind}-{uuid.uuid4().hex}",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _cleanup(db, batch_id: int, site_ids: list[int]) -> None:
    db.execute(delete(PipelineBatch).where(PipelineBatch.id == batch_id))
    db.execute(delete(Site).where(Site.id.in_(site_ids)))
    db.commit()


def test_batch_launch_enqueues_ingestion_for_every_site(client, db, monkeypatch):
    first = _site(db, "first")
    second = _site(db, "second")
    monkeypatch.setattr(pipeline_routes, "enqueue_job", _fake_enqueue)

    response = client.post(
        "/api/v1/pipelines/batches",
        json={"site_ids": [first.id, second.id]},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "running"
    assert body["total"] == 2
    assert body["active"] == 2
    assert body["failed"] == 0
    assert [item["site_id"] for item in body["sites"]] == [first.id, second.id]
    assert all(item["ingestion_job_run_id"] is not None for item in body["sites"])
    _cleanup(db, body["id"], [first.id, second.id])


def test_batch_launch_isolates_one_enqueue_failure(client, db, monkeypatch):
    failed = _site(db, "failed")
    healthy = _site(db, "healthy")

    def sometimes_fails(db, site_id, kind, task, job_timeout, task_kwargs=None):
        if site_id == failed.id:
            raise RuntimeError("queue unavailable for this site")
        return _fake_enqueue(db, site_id, kind, task, job_timeout, task_kwargs)

    monkeypatch.setattr(pipeline_routes, "enqueue_job", sometimes_fails)
    response = client.post(
        "/api/v1/pipelines/batches",
        json={"site_ids": [failed.id, healthy.id]},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "running"
    assert body["active"] == 1
    assert body["failed"] == 1
    failed_item = next(item for item in body["sites"] if item["site_id"] == failed.id)
    assert failed_item["status"] == "failed"
    assert "queue unavailable" in failed_item["error"]
    _cleanup(db, body["id"], [failed.id, healthy.id])


def test_ingestion_success_chains_analysis_and_completes_batch(db, monkeypatch):
    site = _site(db, "chain")
    batch = PipelineBatch()
    db.add(batch)
    db.flush()
    item = PipelineSiteRun(batch_id=batch.id, site_id=site.id)
    db.add(item)
    ingestion_run = JobRun(site_id=site.id, kind="ingestion")
    db.add(ingestion_run)
    db.commit()
    db.refresh(batch)
    db.refresh(item)
    db.refresh(ingestion_run)

    monkeypatch.setattr(pipeline_tasks, "run_ingestion", lambda _site_id, job_run_id=None: {"articles": 4})
    monkeypatch.setattr(pipeline_tasks, "enqueue_job", _fake_enqueue)
    result = pipeline_tasks.ingest_pipeline_site(
        site.id,
        job_run_id=ingestion_run.id,
        batch_site_run_id=item.id,
    )
    db.refresh(item)
    assert result["articles"] == 4
    assert item.status == "analysis_queued"
    assert item.analysis_job_run_id is not None

    monkeypatch.setattr(
        pipeline_tasks,
        "generate_suggestions",
        lambda _site_id, job_run_id=None: {"suggestions": 3},
    )
    analysis_run_id = item.analysis_job_run_id
    result = pipeline_tasks.analyze_pipeline_site(
        site.id,
        job_run_id=analysis_run_id,
        batch_site_run_id=item.id,
    )
    db.refresh(item)
    db.refresh(batch)
    assert result == {"suggestions": 3}
    assert item.status == "succeeded"
    assert item.stage == "completed"
    assert batch.status == "succeeded"
    _cleanup(db, batch.id, [site.id])


def test_ingestion_failure_marks_site_and_batch_failed(db, monkeypatch):
    site = _site(db, "ingestion-failure")
    batch = PipelineBatch()
    db.add(batch)
    db.flush()
    item = PipelineSiteRun(batch_id=batch.id, site_id=site.id)
    db.add(item)
    db.commit()
    db.refresh(batch)
    db.refresh(item)

    def fail_ingestion(_site_id, job_run_id=None):
        raise RuntimeError("crawler unavailable")

    monkeypatch.setattr(pipeline_tasks, "run_ingestion", fail_ingestion)
    with pytest.raises(RuntimeError, match="crawler unavailable"):
        pipeline_tasks.ingest_pipeline_site(site.id, batch_site_run_id=item.id)

    db.refresh(item)
    db.refresh(batch)
    assert item.status == "failed"
    assert item.stage == "ingestion"
    assert item.error == "crawler unavailable"
    assert batch.status == "failed"
    _cleanup(db, batch.id, [site.id])


def test_pipeline_worker_rejects_a_site_run_from_another_site(db):
    first = _site(db, "ownership-first")
    second = _site(db, "ownership-second")
    batch = PipelineBatch()
    db.add(batch)
    db.flush()
    item = PipelineSiteRun(batch_id=batch.id, site_id=first.id)
    db.add(item)
    db.commit()
    db.refresh(item)

    with pytest.raises(ValueError, match=f"belongs to site {first.id}, not site {second.id}"):
        pipeline_tasks.ingest_pipeline_site(second.id, batch_site_run_id=item.id)

    db.refresh(item)
    assert item.status == "queued"
    assert item.stage == "ingestion"
    _cleanup(db, batch.id, [first.id, second.id])


def test_retry_restarts_only_the_failed_stage(client, db, monkeypatch):
    site = _site(db, "retry")
    batch = PipelineBatch(status="failed")
    db.add(batch)
    db.flush()
    item = PipelineSiteRun(
        batch_id=batch.id,
        site_id=site.id,
        status="failed",
        stage="analysis",
        error="analysis failed",
    )
    db.add(item)
    db.commit()
    db.refresh(batch)
    db.refresh(item)
    called_kinds: list[str] = []

    def capture_enqueue(db, site_id, kind, task, job_timeout, task_kwargs=None):
        called_kinds.append(kind)
        return _fake_enqueue(db, site_id, kind, task, job_timeout, task_kwargs)

    monkeypatch.setattr(pipeline_routes, "enqueue_job", capture_enqueue)
    response = client.post(f"/api/v1/pipelines/batches/{batch.id}/sites/{site.id}/retry")

    assert response.status_code == 202, response.text
    assert called_kinds == ["analysis"]
    retried = next(row for row in response.json()["sites"] if row["site_id"] == site.id)
    assert retried["status"] == "analysis_queued"
    assert retried["retry_count"] == 1
    assert retried["analysis_job_run_id"] is not None
    _cleanup(db, batch.id, [site.id])


def test_batch_rejects_duplicate_missing_and_pool_sites(client, db):
    site = _site(db, "valid")
    pool = _site(db, "pool", platform="pool")
    assert (
        client.post("/api/v1/pipelines/batches", json={"site_ids": [site.id, site.id]}).status_code
        == 422
    )
    assert client.post("/api/v1/pipelines/batches", json={"site_ids": [999999]}).status_code == 404
    assert client.post("/api/v1/pipelines/batches", json={"site_ids": [pool.id]}).status_code == 409
    db.delete(site)
    db.delete(pool)
    db.commit()


def test_cancel_batch_marks_unfinished_sites_terminal_and_workers_stop(client, db, monkeypatch):
    site = _site(db, "cancel")
    batch = PipelineBatch(status="running")
    db.add(batch)
    db.flush()
    item = PipelineSiteRun(
        batch_id=batch.id,
        site_id=site.id,
        status="analysis_running",
        stage="analysis",
    )
    db.add(item)
    db.commit()
    db.refresh(batch)
    db.refresh(item)
    monkeypatch.setattr(pipeline_routes, "_stop_queue_job", lambda *_args: None)

    response = client.post(f"/api/v1/pipelines/batches/{batch.id}/cancel")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert response.json()["cancelled"] == 1
    assert response.json()["sites"][0]["status"] == "cancelled"
    assert pipeline_tasks.analyze_pipeline_site(
        site.id, batch_site_run_id=item.id
    ) == {"cancelled": True}
    _cleanup(db, batch.id, [site.id])


def test_terminal_batch_stream_emits_snapshot_and_done(client, db):
    site = _site(db, "stream")
    batch = PipelineBatch(status="succeeded")
    db.add(batch)
    db.flush()
    db.add(
        PipelineSiteRun(
            batch_id=batch.id,
            site_id=site.id,
            status="succeeded",
            stage="completed",
        )
    )
    db.commit()
    db.refresh(batch)

    response = client.get(f"/api/v1/pipelines/batches/{batch.id}/events")
    assert response.status_code == 200
    assert "event: batch" in response.text
    assert '"status":"succeeded"' in response.text
    assert "event: done" in response.text
    _cleanup(db, batch.id, [site.id])
