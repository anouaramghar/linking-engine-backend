"""Durable job runs (Phase 0, finding 7): rows at enqueue time, duplicate protection,
lost-job reconciliation, durable status after Redis eviction. Real Redis + PostgreSQL;
nothing listens on the per-stage queues during tests, so enqueued jobs never execute."""

from types import SimpleNamespace

import pytest
from rq.exceptions import NoSuchJobError
from rq.job import Job
from sqlalchemy import select

import app.services.job_service as job_service
from app.db import SessionLocal
from app.models import JobRun
from app.services.job_service import enqueue_job, run_durably
from app.tasks.queues import redis_conn


def _delete_rq_job(job_id):
    try:
        Job.fetch(job_id, connection=redis_conn).delete()
    except NoSuchJobError:
        pass


@pytest.fixture
def cleanup_rq():
    job_ids = []
    yield job_ids
    for job_id in job_ids:
        _delete_rq_job(job_id)


def test_trigger_creates_durable_run(client, db, site, cleanup_rq):
    resp = client.post(f"/api/v1/sites/{site.id}/ingest")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    cleanup_rq.append(body["job_id"])

    run = db.get(JobRun, body["job_run_id"])
    assert run.kind == "ingestion"
    assert run.status == "queued"
    assert run.queue_job_id == body["job_id"]
    assert Job.fetch(body["job_id"], connection=redis_conn).origin == "ingestion"


def test_job_run_is_committed_before_enqueue(db, site, monkeypatch):
    observed = {}

    class InspectingQueue:
        def enqueue(self, _fn, _site_id, **kwargs):
            with SessionLocal() as observer:
                visible_run = observer.get(JobRun, kwargs["job_run_id"])
                observed["status"] = visible_run.status
                observed["queue_job_id"] = visible_run.queue_job_id
            return type("QueuedJob", (), {"id": "committed-before-enqueue"})()

    monkeypatch.setitem(job_service._QUEUES, "ingestion", InspectingQueue())

    run = enqueue_job(db, site.id, "ingestion", lambda: None, job_timeout=60)

    assert observed == {"status": "queued", "queue_job_id": None}
    assert run.queue_job_id == "committed-before-enqueue"


def test_enqueue_failure_leaves_reconcilable_queued_run(db, site, monkeypatch):
    class FailingQueue:
        def enqueue(self, _fn, _site_id, **_kwargs):
            raise RuntimeError("Redis unavailable")

    monkeypatch.setitem(job_service._QUEUES, "ingestion", FailingQueue())

    with pytest.raises(RuntimeError, match="Redis unavailable"):
        enqueue_job(db, site.id, "ingestion", lambda: None, job_timeout=60)

    db.expire_all()
    run = db.scalars(
        select(JobRun).where(JobRun.site_id == site.id, JobRun.kind == "ingestion")
    ).one()
    assert run.status == "queued"
    assert run.queue_job_id is None


def test_duplicate_trigger_rejected_while_active(client, db, site, cleanup_rq):
    first = client.post(f"/api/v1/sites/{site.id}/ingest")
    assert first.status_code == 202
    cleanup_rq.append(first.json()["job_id"])

    second = client.post(f"/api/v1/sites/{site.id}/ingest")
    assert second.status_code == 409
    assert "already queued" in second.json()["detail"]

    # a different kind is not blocked by an active ingestion
    analysis = client.post(f"/api/v1/suggestions/{site.id}")
    assert analysis.status_code == 202
    cleanup_rq.append(analysis.json()["job_id"])


def test_lost_job_is_reconciled_and_retriggerable(
    client, db, site, cleanup_rq, monkeypatch
):
    alerts = []
    monkeypatch.setattr(
        job_service, "send_alert", lambda subject, payload: alerts.append((subject, payload))
    )
    first = client.post(f"/api/v1/sites/{site.id}/ingest")
    assert first.status_code == 202
    first_body = first.json()
    _delete_rq_job(first_body["job_id"])  # simulate Redis losing the job (finding 7)

    second = client.post(f"/api/v1/sites/{site.id}/ingest")
    assert second.status_code == 202, second.text
    cleanup_rq.append(second.json()["job_id"])

    db.expire_all()
    lost = db.get(JobRun, first_body["job_run_id"])
    assert lost.status == "failed"
    assert "lost from queue" in lost.error
    assert lost.finished_at is not None
    assert alerts == [
        (
            "LinkMesh ingestion job lost",
            {
                "site_id": site.id,
                "kind": "ingestion",
                "job_run_id": lost.id,
                "attempts": 0,
                "error": "lost from queue before completion",
            },
        )
    ]


def test_final_job_failure_sends_alert(db, site, monkeypatch):
    run = JobRun(site_id=site.id, kind="analysis")
    db.add(run)
    db.commit()
    sent = []
    monkeypatch.setattr(
        job_service, "get_current_job", lambda: SimpleNamespace(retries_left=0)
    )
    monkeypatch.setattr(
        job_service, "send_alert", lambda subject, payload: sent.append((subject, payload))
    )

    error = "x" * 2100
    with pytest.raises(RuntimeError, match="x+"):
        run_durably(run.id, lambda _site_id: (_ for _ in ()).throw(RuntimeError(error)), site.id)

    assert sent == [
        (
            "LinkMesh analysis job failed",
            {
                "site_id": site.id,
                "kind": "analysis",
                "job_run_id": run.id,
                "attempts": 1,
                "error": "x" * 2000,
            },
        )
    ]


def test_nonfinal_job_failure_does_not_send_alert(db, site, monkeypatch):
    run = JobRun(site_id=site.id, kind="analysis")
    db.add(run)
    db.commit()
    monkeypatch.setattr(
        job_service, "get_current_job", lambda: SimpleNamespace(retries_left=1)
    )
    monkeypatch.setattr(
        job_service,
        "send_alert",
        lambda *_args: pytest.fail("non-final retry must not alert"),
    )

    with pytest.raises(RuntimeError, match="retry me"):
        run_durably(
            run.id,
            lambda _site_id: (_ for _ in ()).throw(RuntimeError("retry me")),
            site.id,
        )


def test_run_durably_records_attempts_result_and_error(db, site):
    run = JobRun(site_id=site.id, kind="analysis")
    db.add(run)
    db.commit()

    result = run_durably(run.id, lambda site_id: {"suggestions_created": 7}, site.id)
    assert result == {"suggestions_created": 7}
    db.expire_all()
    run = db.get(JobRun, run.id)
    assert (run.status, run.attempts, run.result) == ("succeeded", 1, {"suggestions_created": 7})
    assert run.started_at is not None and run.finished_at is not None

    def boom(site_id):
        raise RuntimeError("embedding model OOM")

    with pytest.raises(RuntimeError):  # re-raised so RQ can retry
        run_durably(run.id, boom, site.id)
    db.expire_all()
    run = db.get(JobRun, run.id)
    assert (run.status, run.attempts) == ("failed", 2)
    assert "embedding model OOM" in run.error


def test_run_durably_tolerates_missing_row(site):
    # pre-table enqueues and cascade-deleted sites must still execute
    assert run_durably(None, lambda site_id: {"ok": True}, site.id) == {"ok": True}
    assert run_durably(999999999, lambda site_id: {"ok": True}, site.id) == {"ok": True}


def test_job_status_survives_redis_eviction(client, db, site):
    run = JobRun(
        site_id=site.id,
        kind="publication",
        status="succeeded",
        queue_job_id="evicted-job-id",
        result={"applied": 2, "failed": 0, "skipped": 0},
    )
    db.add(run)
    db.commit()

    resp = client.get("/api/v1/jobs/evicted-job-id")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["result"] == {"applied": 2, "failed": 0, "skipped": 0}

    assert client.get("/api/v1/jobs/never-existed").status_code == 404


def test_list_job_runs_per_site(client, db, site):
    db.add_all(
        [
            JobRun(site_id=site.id, kind="ingestion", status="succeeded"),
            JobRun(site_id=site.id, kind="analysis", status="failed", error="x"),
        ]
    )
    db.commit()

    all_runs = client.get(f"/api/v1/jobs/site/{site.id}").json()
    assert {r["kind"] for r in all_runs} == {"ingestion", "analysis"}

    only_analysis = client.get(f"/api/v1/jobs/site/{site.id}", params={"kind": "analysis"}).json()
    assert [r["kind"] for r in only_analysis] == ["analysis"]
    assert only_analysis[0]["error"] == "x"
