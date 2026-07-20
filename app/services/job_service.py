"""Durable job runs (Phase 0, finding 7): every ingestion, analysis, and publication
job gets a job_runs row at enqueue time, updated by the task body as it executes.
A job Redis loses is still visible as 'queued' and is reconciled to 'failed' on the
next enqueue. Duplicate triggers are refused while a run is active. RQ retries are
limited and safe — all three task bodies are idempotent (upserts, cached embeddings,
publication claims)."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
from inspect import Parameter, signature

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

logger = logging.getLogger(__name__)


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


def _enqueue_job_locked(db: Session, site_id: int, kind: str, fn, job_timeout: int) -> JobRun:
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
                kind="job_lost",
                site_id=run.site_id,
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


def record_progress(
    db: Session,
    job_run_id: int | None,
    **fields,
) -> None:
    """Advisory progress update that participates in the caller's next commit."""
    if job_run_id is None:
        return
    try:
        # Flush the update inside a savepoint so a progress-only database error can
        # be rolled back without discarding the task's surrounding work transaction.
        with db.begin_nested():
            run = db.get(JobRun, job_run_id)
            if run is None:
                return
            run.progress = {**(run.progress or {}), **fields}
            run.progress_at = datetime.now(timezone.utc)
            db.flush()
    except Exception:
        logger.exception("failed to record progress for job run %s", job_run_id)


def record_progress_durably(job_run_id: int | None, **fields) -> None:
    """Commit progress independently from a task transaction that may roll back."""
    if job_run_id is None:
        return
    db = SessionLocal()
    try:
        record_progress(db, job_run_id, **fields)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("failed to commit progress for job run %s", job_run_id)
    finally:
        db.close()


def _run_task_body(fn, site_id: int, job_run_id: int | None) -> dict:
    parameters = signature(fn).parameters.values()
    if any(
        parameter.name == "job_run_id" or parameter.kind == Parameter.VAR_KEYWORD
        for parameter in parameters
    ):
        return fn(site_id, job_run_id=job_run_id)
    return fn(site_id)


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
            run.finished_at = None
            run.result = None
            db.commit()
        try:
            result = _run_task_body(fn, site_id, job_run_id)
        except Exception as e:
            error = str(e)[:2000]
            current_job = get_current_job()
            retries_left = getattr(current_job, "retries_left", None)
            final_attempt = current_job is None or retries_left is None or retries_left <= 0
            if run is not None:
                # RQ schedules the retry only after the task raises. Keep the durable
                # row active during that window so another API trigger cannot enqueue
                # a duplicate job for the same site and stage.
                run.status = "failed" if final_attempt else "queued"
                run.error = error
                run.finished_at = datetime.now(timezone.utc) if final_attempt else None
                db.commit()
            if final_attempt:
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
                    kind="job_failed",
                    site_id=site_id,
                )
            raise
        if run is not None:
            run.status = "succeeded"
            run.result = result
            run.error = None  # clear any earlier attempt's failure
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
                "progress": run.progress,
                "progress_at": run.progress_at,
                "error": run.error,
            }
        finally:
            db.close()
    status = job.get_status()
    db = SessionLocal()
    try:
        # The live RQ view knows nothing of progress — merge it from the durable row,
        # because a UI polls this endpoint precisely while the job is still in Redis.
        run = db.scalars(select(JobRun).where(JobRun.queue_job_id == job_id)).first()
        progress = run.progress if run is not None else None
        progress_at = run.progress_at if run is not None else None
    finally:
        db.close()
    return {
        "job_id": job_id,
        "status": status,
        "result": job.return_value() if status == "finished" else None,
        "progress": progress,
        "progress_at": progress_at,
        "error": job.exc_info.strip().splitlines()[-1] if job.exc_info else None,
    }
