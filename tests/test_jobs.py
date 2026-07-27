"""Durable job runs (Phase 0, finding 7): rows at enqueue time, duplicate protection,
lost-job reconciliation, durable status after Redis eviction. Real Redis + PostgreSQL;
nothing listens on the per-stage queues during tests, so enqueued jobs never execute."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from rq.exceptions import AbandonedJobError, NoSuchJobError
from rq.job import Callback, Job
from sqlalchemy import select

import app.services.job_service as job_service
from app.config import settings
from app.db import SessionLocal
from app.models import Alert, IngestionRun, JobRun
from app.schemas.job import JobRunOut
from app.services import alerts as alert_service
from app.services.job_service import (
    enqueue_job,
    handle_abandoned_job,
    handle_job_stopped,
    handle_work_horse_killed,
    run_durably,
)
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
                observed["on_stopped"] = kwargs["on_stopped"]
            return type("QueuedJob", (), {"id": "committed-before-enqueue"})()

    monkeypatch.setitem(job_service._QUEUES, "ingestion", InspectingQueue())

    run = enqueue_job(db, site.id, "ingestion", lambda: None, job_timeout=60)

    assert observed["status"] == "queued"
    assert observed["queue_job_id"] is None
    assert isinstance(observed["on_stopped"], Callback)
    assert observed["on_stopped"].func is handle_job_stopped
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


def test_lost_job_is_reconciled_and_retriggerable(client, db, site, cleanup_rq, monkeypatch):
    alerts = []
    monkeypatch.setattr(
        job_service,
        "send_alert",
        lambda subject, payload, **metadata: alerts.append((subject, payload, metadata)),
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
            {"kind": "job_lost", "site_id": site.id},
        )
    ]


def test_final_job_failure_sends_alert(db, site, monkeypatch):
    run = JobRun(site_id=site.id, kind="analysis")
    db.add(run)
    db.commit()
    monkeypatch.setattr(settings, "alert_webhook_url", "")
    monkeypatch.setattr(job_service, "get_current_job", lambda: SimpleNamespace(retries_left=0))

    error = "x" * 2100
    with pytest.raises(RuntimeError, match="x+"):
        run_durably(run.id, lambda _site_id: (_ for _ in ()).throw(RuntimeError(error)), site.id)

    db.expire_all()
    stored = db.scalar(select(Alert).where(Alert.site_id == site.id))
    assert stored.kind == "job_failed"
    assert stored.subject == "LinkMesh analysis job failed"
    assert stored.occurrences == 1
    assert stored.payload == {
        "site_id": site.id,
        "kind": "analysis",
        "job_run_id": run.id,
        "attempts": 1,
        "error": "x" * 2000,
    }


def test_nonfinal_job_failure_does_not_send_alert(db, site, monkeypatch):
    run = JobRun(site_id=site.id, kind="analysis")
    db.add(run)
    db.commit()
    monkeypatch.setattr(job_service, "get_current_job", lambda: SimpleNamespace(retries_left=1))
    monkeypatch.setattr(
        job_service,
        "send_alert",
        lambda *_args, **_kwargs: pytest.fail("non-final retry must not alert"),
    )

    with pytest.raises(RuntimeError, match="retry me"):
        run_durably(
            run.id,
            lambda _site_id: (_ for _ in ()).throw(RuntimeError("retry me")),
            site.id,
        )

    db.expire_all()
    stored = db.get(JobRun, run.id)
    assert (stored.status, stored.attempts) == ("queued", 1)
    assert stored.error == "retry me"
    assert stored.finished_at is None

    monkeypatch.setattr(job_service, "_still_in_queue", lambda candidate: candidate.id == run.id)
    with pytest.raises(job_service.DuplicateJobError, match="already queued"):
        enqueue_job(db, site.id, "analysis", lambda: None, job_timeout=60)


def test_killed_work_horse_keeps_job_queued_and_reconciles_ingestion(
    db, site, monkeypatch
):
    started_at = datetime.now(timezone.utc)
    run = JobRun(
        site_id=site.id,
        kind="ingestion",
        status="running",
        queue_job_id="killed-ingestion",
        attempts=1,
        started_at=started_at,
    )
    unrelated_job_run = JobRun(
        site_id=site.id,
        kind="ingestion",
        status="running",
        queue_job_id="unrelated-ingestion-job",
        attempts=1,
        started_at=started_at,
    )
    db.add_all([run, unrelated_job_run])
    db.flush()
    ingestion_run = IngestionRun(
        site_id=site.id,
        job_run_id=run.id,
        status="running",
        started_at=started_at,
    )
    stale_same_job_ingestion_run = IngestionRun(
        site_id=site.id,
        job_run_id=run.id,
        status="running",
        started_at=started_at,
    )
    unrelated_ingestion_run = IngestionRun(
        site_id=site.id,
        job_run_id=unrelated_job_run.id,
        status="running",
        started_at=started_at,
    )
    db.add_all(
        [ingestion_run, stale_same_job_ingestion_run, unrelated_ingestion_run]
    )
    db.commit()
    monkeypatch.setattr(
        job_service,
        "send_alert",
        lambda *_args, **_kwargs: pytest.fail("a retried work horse must not alert"),
    )

    handle_work_horse_killed(
        SimpleNamespace(id=run.queue_job_id, retries_left=1),
        retpid=123,
        ret_val=9,
        rusage=None,
    )

    db.expire_all()
    stored = db.get(JobRun, run.id)
    stored_ingestion = db.get(IngestionRun, ingestion_run.id)
    assert stored.status == "queued"
    assert stored.finished_at is None
    assert "waitpid status 9" in stored.error
    assert stored_ingestion.status == "failed"
    assert stored_ingestion.finished_at is not None
    assert stored_ingestion.error == stored.error
    assert db.get(IngestionRun, stale_same_job_ingestion_run.id).status == "failed"
    assert db.get(IngestionRun, unrelated_ingestion_run.id).status == "running"


def test_terminal_killed_work_horse_fails_once_and_alerts_once(db, site, monkeypatch):
    run = JobRun(
        site_id=site.id,
        kind="analysis",
        status="running",
        queue_job_id="terminally-killed-analysis",
        attempts=3,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    alerts = []
    monkeypatch.setattr(
        job_service,
        "send_alert",
        lambda subject, payload, **metadata: alerts.append((subject, payload, metadata)),
    )
    rq_job = SimpleNamespace(id=run.queue_job_id, retries_left=0)

    handle_work_horse_killed(rq_job, retpid=123, ret_val=9, rusage=None)
    handle_work_horse_killed(rq_job, retpid=123, ret_val=9, rusage=None)

    db.expire_all()
    stored = db.get(JobRun, run.id)
    assert stored.status == "failed"
    assert stored.finished_at is not None
    assert stored.error == "work horse terminated unexpectedly (waitpid status 9)"
    assert alerts == [
        (
            "LinkMesh analysis job failed",
            {
                "site_id": site.id,
                "kind": "analysis",
                "job_run_id": run.id,
                "attempts": 3,
                "error": stored.error,
            },
            {"kind": "job_failed", "site_id": site.id},
        )
    ]


def test_killed_work_horse_requeues_durable_success_when_rq_will_retry(
    db, site, monkeypatch
):
    finished_at = datetime.now(timezone.utc)
    run = JobRun(
        site_id=site.id,
        kind="analysis",
        status="succeeded",
        queue_job_id="killed-after-durable-success",
        attempts=1,
        result={"suggestions_created": 7},
        started_at=finished_at,
        finished_at=finished_at,
    )
    db.add(run)
    db.commit()
    monkeypatch.setattr(
        job_service,
        "send_alert",
        lambda *_args, **_kwargs: pytest.fail("a retried work horse must not alert"),
    )

    handle_work_horse_killed(
        SimpleNamespace(id=run.queue_job_id, retries_left=1),
        retpid=123,
        ret_val=9,
        rusage=None,
    )

    db.expire_all()
    stored = db.get(JobRun, run.id)
    assert stored.status == "queued"
    assert stored.result is None
    assert stored.finished_at is None
    assert "waitpid status 9" in stored.error


def test_terminal_killed_work_horse_preserves_committed_success(
    db, site, monkeypatch, caplog
):
    finished_at = datetime.now(timezone.utc)
    result = {"suggestions_created": 7}
    run = JobRun(
        site_id=site.id,
        kind="analysis",
        status="succeeded",
        queue_job_id="terminal-kill-after-durable-success",
        attempts=1,
        result=result,
        started_at=finished_at,
        finished_at=finished_at,
    )
    db.add(run)
    db.commit()
    monkeypatch.setattr(
        job_service,
        "send_alert",
        lambda *_args, **_kwargs: pytest.fail("committed success must not alert as failed"),
    )

    handle_work_horse_killed(
        SimpleNamespace(id=run.queue_job_id, retries_left=0),
        retpid=123,
        ret_val=9,
        rusage=None,
    )

    db.expire_all()
    stored = db.get(JobRun, run.id)
    assert stored.status == "succeeded"
    assert stored.result == result
    assert stored.finished_at == finished_at
    assert stored.error is None
    assert "preserving durable success" in caplog.text


def test_abandoned_job_stays_queued_when_rq_will_retry(db, site, monkeypatch):
    run = JobRun(
        site_id=site.id,
        kind="analysis",
        status="running",
        queue_job_id="abandoned-with-retry",
        attempts=1,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    monkeypatch.setattr(
        job_service,
        "send_alert",
        lambda *_args, **_kwargs: pytest.fail("an abandoned retry must not alert"),
    )

    fallthrough = handle_abandoned_job(
        SimpleNamespace(id=run.queue_job_id, retries_left=1),
        AbandonedJobError,
        AbandonedJobError(),
        None,
    )

    db.expire_all()
    stored = db.get(JobRun, run.id)
    assert fallthrough is True
    assert stored.status == "queued"
    assert stored.finished_at is None
    assert stored.error == "job abandoned after worker termination"


def test_terminal_abandoned_job_preserves_committed_success(
    db, site, monkeypatch
):
    finished_at = datetime.now(timezone.utc)
    result = {"suggestions_created": 7}
    run = JobRun(
        site_id=site.id,
        kind="analysis",
        status="succeeded",
        queue_job_id="abandoned-after-durable-success",
        attempts=1,
        result=result,
        started_at=finished_at,
        finished_at=finished_at,
    )
    db.add(run)
    db.commit()
    monkeypatch.setattr(
        job_service,
        "send_alert",
        lambda *_args, **_kwargs: pytest.fail("committed success must not alert as failed"),
    )

    assert (
        handle_abandoned_job(
            SimpleNamespace(id=run.queue_job_id, retries_left=0),
            AbandonedJobError,
            AbandonedJobError(),
            None,
        )
        is True
    )

    db.expire_all()
    stored = db.get(JobRun, run.id)
    assert stored.status == "succeeded"
    assert stored.result == result
    assert stored.finished_at == finished_at
    assert stored.error is None


def test_abandoned_handler_ignores_other_exceptions(db, site, monkeypatch):
    run = JobRun(
        site_id=site.id,
        kind="analysis",
        status="running",
        queue_job_id="ordinary-task-error",
        attempts=1,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    monkeypatch.setattr(
        job_service,
        "send_alert",
        lambda *_args, **_kwargs: pytest.fail("ordinary errors are not handled here"),
    )

    fallthrough = handle_abandoned_job(
        SimpleNamespace(id=run.queue_job_id, retries_left=0),
        RuntimeError,
        RuntimeError("ordinary task failure"),
        None,
    )

    db.expire_all()
    assert fallthrough is True
    assert db.get(JobRun, run.id).status == "running"


def test_terminal_abandoned_job_fails_and_alerts_once(db, site, monkeypatch):
    run = JobRun(
        site_id=site.id,
        kind="publication",
        status="running",
        queue_job_id="terminally-abandoned",
        attempts=3,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    alerts = []
    monkeypatch.setattr(
        job_service,
        "send_alert",
        lambda subject, payload, **metadata: alerts.append((subject, payload, metadata)),
    )
    rq_job = SimpleNamespace(id=run.queue_job_id, retries_left=0)

    for _ in range(2):
        assert (
            handle_abandoned_job(
                rq_job,
                AbandonedJobError,
                AbandonedJobError(),
                None,
            )
            is True
        )

    db.expire_all()
    stored = db.get(JobRun, run.id)
    assert stored.status == "failed"
    assert stored.finished_at is not None
    assert alerts == [
        (
            "LinkMesh publication job failed",
            {
                "site_id": site.id,
                "kind": "publication",
                "job_run_id": run.id,
                "attempts": 3,
                "error": "job abandoned after worker termination",
            },
            {"kind": "job_failed", "site_id": site.id},
        )
    ]


def test_stopped_job_fails_linked_ingestion_and_alerts_once(db, site, monkeypatch):
    started_at = datetime.now(timezone.utc)
    run = JobRun(
        site_id=site.id,
        kind="ingestion",
        status="running",
        queue_job_id="intentionally-stopped",
        attempts=1,
        started_at=started_at,
    )
    db.add(run)
    db.flush()
    ingestion_run = IngestionRun(
        site_id=site.id,
        job_run_id=run.id,
        status="running",
        started_at=started_at,
    )
    db.add(ingestion_run)
    db.commit()
    alerts = []
    monkeypatch.setattr(
        job_service,
        "send_alert",
        lambda subject, payload, **metadata: alerts.append((subject, payload, metadata)),
    )
    rq_job = SimpleNamespace(id=run.queue_job_id, retries_left=2)

    handle_job_stopped(rq_job, connection=None)
    handle_job_stopped(rq_job, connection=None)

    db.expire_all()
    stored = db.get(JobRun, run.id)
    stored_ingestion = db.get(IngestionRun, ingestion_run.id)
    assert stored.status == "failed"
    assert stored.error == "job stopped intentionally"
    assert stored.finished_at is not None
    assert stored_ingestion.status == "failed"
    assert stored_ingestion.error == stored.error
    assert alerts == [
        (
            "LinkMesh ingestion job stopped",
            {
                "site_id": site.id,
                "kind": "ingestion",
                "job_run_id": run.id,
                "attempts": 1,
                "error": "job stopped intentionally",
            },
            {"kind": "job_stopped", "site_id": site.id},
        )
    ]


def test_stopped_job_preserves_success_committed_before_rq_stop(
    db, site, monkeypatch
):
    finished_at = datetime.now(timezone.utc)
    result = {"suggestions_created": 7}
    run = JobRun(
        site_id=site.id,
        kind="analysis",
        status="succeeded",
        queue_job_id="stopped-after-durable-success",
        attempts=1,
        result=result,
        started_at=finished_at,
        finished_at=finished_at,
    )
    db.add(run)
    db.commit()
    monkeypatch.setattr(
        job_service,
        "send_alert",
        lambda *_args, **_kwargs: pytest.fail("committed success must not alert as stopped"),
    )

    handle_job_stopped(
        SimpleNamespace(id=run.queue_job_id, retries_left=0),
        connection=None,
    )

    db.expire_all()
    stored = db.get(JobRun, run.id)
    assert stored.status == "succeeded"
    assert stored.result == result
    assert stored.finished_at == finished_at
    assert stored.error is None


def test_killed_work_horse_falls_back_to_job_run_id_from_rq_kwargs(
    db, site, monkeypatch
):
    run = JobRun(
        site_id=site.id,
        kind="publication",
        status="running",
        queue_job_id=None,
        attempts=1,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    monkeypatch.setattr(job_service, "send_alert", lambda *_args, **_kwargs: None)

    handle_work_horse_killed(
        SimpleNamespace(
            id="dequeued-before-rq-id-commit",
            kwargs={"job_run_id": run.id},
            retries_left=0,
        ),
        retpid=123,
        ret_val=9,
        rusage=None,
    )

    db.expire_all()
    stored = db.get(JobRun, run.id)
    assert stored.queue_job_id == "dequeued-before-rq-id-commit"
    assert stored.status == "failed"
    assert stored.finished_at is not None


def test_killed_ingestion_without_started_attempt_does_not_guess_run(
    db, site, monkeypatch
):
    run = JobRun(
        site_id=site.id,
        kind="ingestion",
        status="queued",
        queue_job_id="killed-before-task-start",
        started_at=None,
    )
    db.add(run)
    db.flush()
    ingestion_run = IngestionRun(
        site_id=site.id,
        job_run_id=run.id,
        status="running",
    )
    db.add(ingestion_run)
    db.commit()
    monkeypatch.setattr(job_service, "send_alert", lambda *_args, **_kwargs: None)

    handle_work_horse_killed(
        SimpleNamespace(id=run.queue_job_id, retries_left=0),
        retpid=123,
        ret_val=9,
        rusage=None,
    )

    db.expire_all()
    assert db.get(JobRun, run.id).status == "failed"
    assert db.get(IngestionRun, ingestion_run.id).status == "running"


def test_alert_db_failure_preserves_original_job_error(db, site, monkeypatch, caplog):
    run = JobRun(site_id=site.id, kind="analysis")
    db.add(run)
    db.commit()
    monkeypatch.setattr(settings, "alert_webhook_url", "")
    monkeypatch.setattr(job_service, "get_current_job", lambda: None)

    def fail_session():
        raise RuntimeError("alerts database unavailable")

    monkeypatch.setattr(alert_service, "SessionLocal", fail_session)

    with pytest.raises(RuntimeError, match="original task error"):
        run_durably(
            run.id,
            lambda _site_id: (_ for _ in ()).throw(RuntimeError("original task error")),
            site.id,
        )

    assert "failed to persist alert" in caplog.text
    db.expire_all()
    assert db.get(JobRun, run.id).error == "original task error"


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

    # a successful retry clears the earlier attempt's error
    run_durably(run.id, lambda site_id: {"suggestions_created": 7}, site.id)
    db.expire_all()
    run = db.get(JobRun, run.id)
    assert (run.status, run.attempts, run.error) == ("succeeded", 3, None)


def test_run_durably_tolerates_missing_row(site):
    # pre-table enqueues and cascade-deleted sites must still execute
    assert run_durably(None, lambda site_id: {"ok": True}, site.id) == {"ok": True}
    assert run_durably(999999999, lambda site_id: {"ok": True}, site.id) == {"ok": True}


def test_job_status_survives_redis_eviction(client, db, site):
    progress_at = datetime.now(timezone.utc)
    run = JobRun(
        site_id=site.id,
        kind="publication",
        status="succeeded",
        queue_job_id="evicted-job-id",
        result={"applied": 2, "failed": 0, "skipped": 0},
        progress={"stage": "publishing", "applied": 2, "total": 2},
        progress_at=progress_at,
    )
    db.add(run)
    db.commit()

    resp = client.get("/api/v1/jobs/evicted-job-id")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["result"] == {"applied": 2, "failed": 0, "skipped": 0}
    assert body["progress"] == {"stage": "publishing", "applied": 2, "total": 2}
    assert body["progress_at"] is not None

    assert client.get("/api/v1/jobs/never-existed").status_code == 404


def test_job_status_reports_progress_while_job_is_live_in_redis(client, db, site, cleanup_rq):
    # A UI polls /jobs/{job_id} while the job is still in Redis — progress must
    # come from the durable row even on that live branch.
    accepted = client.post(f"/api/v1/sites/{site.id}/ingest").json()
    cleanup_rq.append(accepted["job_id"])
    run = db.get(JobRun, accepted["job_run_id"])
    run.progress = {"stage": "crawling", "articles": 150}
    run.progress_at = datetime.now(timezone.utc)
    db.commit()

    body = client.get(f"/api/v1/jobs/{accepted['job_id']}").json()
    assert body["status"] == "queued"  # live RQ vocabulary — the job was never drained
    assert body["progress"] == {"stage": "crawling", "articles": 150}
    assert body["progress_at"] is not None


@pytest.mark.parametrize("rq_status", ["failed", "stopped", "canceled"])
def test_live_terminal_rq_status_prefers_committed_durable_success(
    client, db, site, monkeypatch, rq_status
):
    result = {"suggestions_created": 7}
    run = JobRun(
        site_id=site.id,
        kind="analysis",
        status="succeeded",
        queue_job_id=f"split-brain-{rq_status}",
        attempts=1,
        result=result,
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    live_job = SimpleNamespace(
        get_status=lambda: rq_status,
        latest_result=lambda: pytest.fail("durable success must bypass the RQ result"),
        return_value=lambda: pytest.fail("durable success must bypass the RQ result"),
    )
    monkeypatch.setattr(
        job_service.Job,
        "fetch",
        staticmethod(lambda _job_id, connection: live_job),
    )

    body = client.get(f"/api/v1/jobs/{run.queue_job_id}").json()

    assert body["status"] == "succeeded"
    assert body["result"] == result
    runs = client.get(f"/api/v1/jobs/site/{site.id}").json()
    listed = next(candidate for candidate in runs if candidate["id"] == run.id)
    assert listed["status"] == body["status"]
    assert listed["result"] == body["result"]


def test_live_failure_is_not_masked_without_durable_success(
    client, db, site, monkeypatch
):
    run = JobRun(
        site_id=site.id,
        kind="analysis",
        status="queued",
        queue_job_id="retry-will-not-mask-live-failure",
        attempts=1,
    )
    db.add(run)
    db.commit()
    live_job = SimpleNamespace(
        get_status=lambda: "failed",
        latest_result=lambda: SimpleNamespace(
            exc_string="Traceback (most recent call last):\nRuntimeError: live failure"
        ),
        return_value=lambda: None,
    )
    monkeypatch.setattr(
        job_service.Job,
        "fetch",
        staticmethod(lambda _job_id, connection: live_job),
    )

    body = client.get(f"/api/v1/jobs/{run.queue_job_id}").json()

    assert body["status"] == "failed"
    assert body["result"] is None
    assert body["error"] == "RuntimeError: live failure"


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


def test_job_run_out_serializes_progress(db, site):
    progress_at = datetime.now(timezone.utc)
    run = JobRun(
        site_id=site.id,
        kind="ingestion",
        progress={"stage": "crawling", "articles": 50},
        progress_at=progress_at,
    )
    db.add(run)
    db.commit()

    serialized = JobRunOut.model_validate(run)

    assert serialized.progress == {"stage": "crawling", "articles": 50}
    assert serialized.progress_at == progress_at
