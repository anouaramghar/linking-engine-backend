"""Durable job runs (Phase 0, finding 7): every ingestion, analysis, and publication
job gets a job_runs row at enqueue time, updated by the task body as it executes.
A job Redis loses is still visible as 'queued' and is reconciled to 'failed' on the
next enqueue. Duplicate triggers are refused while a run is active. RQ retries are
limited and safe — all three task bodies are idempotent (upserts, cached embeddings,
publication claims)."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from rq import Retry, get_current_job
from rq.exceptions import NoSuchJobError
from rq.job import Job
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal, engine
from app.models import JobRun
from app.services.alerts import send_alert
from app.tasks.queues import analysis_queue, ingestion_queue, publication_queue, redis_conn

_ENQUEUE_LOCK_NAMESPACE = 0x4C4A  # "LJ" — serializes enqueues per site

_QUEUES = {
    "ingestion": ingestion_queue,
    "analysis": analysis_queue,
    "publication": publication_queue,
}
_RQ_ACTIVE_STATUSES = {"queued", "started", "deferred", "scheduled"}


class DuplicateJobError(Exception):
    def __init__(self, run: JobRun):
        self.run = run
        super().__init__(f"{run.kind} job already {run.status} for site {run.site_id}")


@contextmanager
def _site_enqueue_lock(site_id: int) -> Iterator[None]:
    # The work session commits the durable row before enqueueing. A dedicated
    # transaction keeps the per-site advisory lock across both work commits.
    with engine.begin() as lock_connection:
        lock_connection.execute(
            select(func.pg_advisory_xact_lock(_ENQUEUE_LOCK_NAMESPACE, site_id))
        ).scalar_one()
        yield


def _still_in_queue(run: JobRun) -> bool:
    if run.queue_job_id is None:  # crashed between insert and enqueue
        return False
    try:
        job = Job.fetch(run.queue_job_id, connection=redis_conn)
    except NoSuchJobError:
        return False
    return job.get_status() in _RQ_ACTIVE_STATUSES


def _enqueue_job_locked(
    db: Session, site_id: int, kind: str, fn, job_timeout: int
) -> JobRun:
    """Create the durable run row, then enqueue. Raises DuplicateJobError while an
    active run of this kind exists for the site."""
    active = db.scalars(
        select(JobRun).where(
            JobRun.site_id == site_id,
            JobRun.kind == kind,
            JobRun.status.in_(["queued", "running"]),
        )
    ).all()
    for run in active:
        if not _still_in_queue(run):  # finding 7's exact failure: the job disappeared
            run.status = "failed"
            run.error = "lost from queue before completion"
            run.finished_at = datetime.now(timezone.utc)
            send_alert(
                f"LinkMesh {run.kind} job lost",
                {
                    "site_id": run.site_id,
                    "kind": run.kind,
                    "job_run_id": run.id,
                    "attempts": run.attempts,
                    "error": run.error,
                },
            )
    still_active = [run for run in active if run.status in ("queued", "running")]
    if still_active:
        db.commit()  # keep the reconciliations
        raise DuplicateJobError(still_active[0])

    run = JobRun(site_id=site_id, kind=kind)
    db.add(run)
    db.commit()
    job = _QUEUES[kind].enqueue(
        fn,
        site_id,
        job_run_id=run.id,
        job_timeout=job_timeout,
        retry=Retry(max=2, interval=[30, 120]),  # limited automatic retries
    )
    run.queue_job_id = job.id
    db.commit()
    db.refresh(run)
    return run


def enqueue_job(db: Session, site_id: int, kind: str, fn, job_timeout: int) -> JobRun:
    with _site_enqueue_lock(site_id):
        return _enqueue_job_locked(db, site_id, kind, fn, job_timeout)


def run_durably(job_run_id: int | None, fn, site_id: int) -> dict:
    """Task-body wrapper: records start, attempt count, result or error. Re-raises so
    RQ can retry; the final attempt's failure stays recorded. Tolerates a missing row
    (job enqueued before this table existed, or site deleted meanwhile)."""
    db = SessionLocal()
    try:
        run = db.get(JobRun, job_run_id) if job_run_id is not None else None
        if run is not None:
            run.status = "running"
            run.attempts += 1
            run.started_at = datetime.now(timezone.utc)
            db.commit()
        try:
            result = fn(site_id)
        except Exception as e:
            error = str(e)[:2000]
            if run is not None:
                run.status = "failed"
                run.error = error
                run.finished_at = datetime.now(timezone.utc)
                db.commit()
            current_job = get_current_job()
            retries_left = getattr(current_job, "retries_left", None)
            if current_job is None or retries_left is None or retries_left <= 0:
                send_alert(
                    f"LinkMesh {run.kind if run is not None else 'unknown'} job failed",
                    {
                        "site_id": site_id,
                        "kind": (
                            run.kind
                            if run is not None
                            else getattr(current_job, "origin", "unknown")
                        ),
                        "job_run_id": job_run_id,
                        "attempts": run.attempts if run is not None else 1,
                        "error": error,
                    },
                )
            raise
        if run is not None:
            run.status = "succeeded"
            run.result = result
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
        return result
    finally:
        db.close()


def get_job_status(job_id: str) -> dict | None:
    """RQ's live view when the job is still in Redis; the durable job_runs row after
    Redis has evicted it (Phase 0, finding 7)."""
    try:
        job = Job.fetch(job_id, connection=redis_conn)
    except NoSuchJobError:
        db = SessionLocal()
        try:
            run = db.scalars(select(JobRun).where(JobRun.queue_job_id == job_id)).first()
            if run is None:
                return None
            return {
                "job_id": job_id,
                "status": run.status,
                "result": run.result,
                "error": run.error,
            }
        finally:
            db.close()
    status = job.get_status()
    return {
        "job_id": job_id,
        "status": status,
        "result": job.return_value() if status == "finished" else None,
        "error": job.exc_info.strip().splitlines()[-1] if job.exc_info else None,
    }
