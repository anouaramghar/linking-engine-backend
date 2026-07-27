"""Durable job runs (Phase 0, finding 7): every ingestion, analysis, and publication
job gets a job_runs row at enqueue time, updated by task and worker lifecycle hooks.
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
from rq.exceptions import AbandonedJobError, NoSuchJobError
from rq.job import Callback, Job
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal, engine
from app.models import IngestionRun, JobRun
from app.services.alerts import send_alert
from app.tasks.queues import analysis_queue, ingestion_queue, publication_queue, redis_conn

_ENQUEUE_LOCK_NAMESPACE = 0x4C4A  # "LJ" — serializes enqueues per site

_QUEUES = {
    "ingestion": ingestion_queue,
    "analysis": analysis_queue,
    "publication": publication_queue,
}
_RQ_ACTIVE_STATUSES = {"queued", "started", "deferred", "scheduled"}
_RQ_TERMINAL_FAILURE_STATUSES = {"failed", "stopped", "canceled"}

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
        on_stopped=Callback(handle_job_stopped),
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


def _mark_publication_failure_progress(
    run: JobRun, *, terminal: bool, progress_at: datetime
) -> None:
    progress = run.progress
    if not isinstance(progress, dict) or progress.get("stage") != "publishing":
        return
    updated = {
        **progress,
        "failed": 0,
        "failure_state": "terminal" if terminal else "retrying",
    }
    if terminal:
        total = updated.get("total", 0)
        applied = updated.get("applied", 0)
        skipped = updated.get("skipped", 0)
        attempt_failed = updated.get("attempt_failed", 0)
        updated["failed"] = max(total - applied - skipped, attempt_failed, 0)
        updated["skipped"] = max(total - applied - updated["failed"], 0)
    run.progress = updated
    run.progress_at = progress_at


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
                now = datetime.now(timezone.utc)
                # Task progress is committed through independent sessions, while this
                # wrapper deliberately keeps its JobRun identity across the attempt.
                db.refresh(run, attribute_names=["progress", "progress_at"])
                _mark_publication_failure_progress(
                    run,
                    terminal=final_attempt,
                    progress_at=now,
                )
                # RQ schedules the retry only after the task raises. Keep the durable
                # row active during that window so another API trigger cannot enqueue
                # a duplicate job for the same site and stage.
                run.status = "failed" if final_attempt else "queued"
                run.error = error
                run.finished_at = now if final_attempt else None
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


def _reconcile_interrupted_job(
    job: Job,
    error: str,
    *,
    will_retry: bool | None = None,
    alert_kind: str = "job_failed",
    alert_verb: str = "failed",
) -> None:
    """Idempotently reconcile a task RQ cannot finish recording.

    Unexpected-child and abandoned-job hooks run before RQ decrements retries, so
    ``retries_left`` describes the transition RQ is about to make. Intentional stop
    callbacks override it because RQ never retries a stopped job.
    """
    job_id = getattr(job, "id", None)
    error = error[:2000]
    alert_payload: dict | None = None
    alert_subject: str | None = None
    db: Session | None = None
    try:
        db = SessionLocal()
        run = db.scalars(
            select(JobRun)
            .where(JobRun.queue_job_id == job_id)
            .with_for_update()
        ).first()
        if run is None:
            # A fast worker can dequeue before enqueue_job has committed the RQ id
            # back to job_runs. The durable id is already present in RQ kwargs.
            try:
                job_kwargs = getattr(job, "kwargs", {})
            except Exception:
                job_kwargs = {}
            job_run_id = job_kwargs.get("job_run_id") if isinstance(job_kwargs, dict) else None
            if isinstance(job_run_id, int):
                candidate = db.get(JobRun, job_run_id, with_for_update=True)
                if candidate is not None and candidate.queue_job_id in (None, job_id):
                    run = candidate
                    run.queue_job_id = job_id
        if run is None:
            return

        if will_retry is None:
            retries_left = getattr(job, "retries_left", None)
            will_retry = retries_left is not None and retries_left > 0
        if run.status == "failed":
            return
        if run.status == "succeeded" and not will_retry:
            # run_durably commits success only after the task's application work
            # returns. A terminal interruption after that commit must not rewrite
            # known-good work because RQ missed its FINISHED write.
            logger.warning(
                "preserving durable success for RQ job %s after terminal interruption",
                job_id,
            )
            return

        now = datetime.now(timezone.utc)

        # Each ingestion attempt persists its logical job id in the IngestionRun
        # INSERT. Never guess by site or timestamp. Under normal execution there is
        # one running row; updating all running rows also closes any stale attempt
        # from the same RQ job without touching another logical job.
        if run.kind == "ingestion" and run.started_at is not None:
            ingestion_runs = db.scalars(
                select(IngestionRun)
                .where(
                    IngestionRun.job_run_id == run.id,
                    IngestionRun.site_id == run.site_id,
                    IngestionRun.status == "running",
                )
                .with_for_update()
            ).all()
            for ingestion_run in ingestion_runs:
                ingestion_run.status = "failed"
                ingestion_run.error = error
                ingestion_run.finished_at = now

        run.status = "queued" if will_retry else "failed"
        run.error = error
        run.finished_at = None if will_retry else now
        _mark_publication_failure_progress(
            run,
            terminal=not will_retry,
            progress_at=now,
        )
        # This includes the narrow case where the child died after committing
        # durable success but before RQ recorded FINISHED. Do not expose a stale
        # successful result while RQ retries, or after an intentional stop.
        run.result = None

        if not will_retry:
            alert_subject = f"LinkMesh {run.kind} job {alert_verb}"
            alert_payload = {
                "site_id": run.site_id,
                "kind": run.kind,
                "job_run_id": run.id,
                "attempts": run.attempts,
                "error": error,
            }
        db.commit()
    except Exception:
        if db is not None:
            db.rollback()
        logger.exception("failed to reconcile interrupted RQ job %s", job_id)
        return
    finally:
        if db is not None:
            db.close()

    # Only the callback that performs the terminal state transition sends an alert.
    # A duplicate callback sees the already-failed row above and returns early.
    if alert_payload is not None and alert_subject is not None:
        try:
            send_alert(
                alert_subject,
                alert_payload,
                kind=alert_kind,
                site_id=alert_payload["site_id"],
            )
        except Exception:
            # send_alert is best-effort itself, but preserve RQ's failure handling if
            # a replacement/test implementation unexpectedly raises.
            logger.exception("failed to alert for interrupted RQ job %s", job_id)


def handle_work_horse_killed(job: Job, retpid: int, ret_val: int, rusage) -> None:
    """Reconcile durable state when RQ's task child exits unexpectedly."""
    del retpid, rusage  # Included in RQ's callback contract; not needed for persistence.
    try:
        _reconcile_interrupted_job(
            job,
            f"work horse terminated unexpectedly (waitpid status {ret_val})",
        )
    except Exception:
        logger.exception(
            "unexpected error in killed work horse callback for RQ job %s",
            getattr(job, "id", None),
        )


def handle_abandoned_job(job: Job, exc_type, exc_value, traceback) -> bool:
    """Reconcile a job abandoned by a dead worker, then preserve handler fallthrough."""
    del traceback
    if exc_type is not AbandonedJobError and not isinstance(exc_value, AbandonedJobError):
        return True
    try:
        _reconcile_interrupted_job(
            job,
            "job abandoned after worker termination",
        )
    except Exception:
        logger.exception(
            "unexpected error in abandoned job handler for RQ job %s",
            getattr(job, "id", None),
        )
    return True


def handle_job_stopped(job: Job, connection) -> None:
    """Mark an intentionally stopped RQ job terminal; this callback never raises."""
    del connection
    try:
        _reconcile_interrupted_job(
            job,
            "job stopped intentionally",
            will_retry=False,
            alert_kind="job_stopped",
            alert_verb="stopped",
        )
    except Exception:
        logger.exception(
            "unexpected error in stopped job callback for RQ job %s",
            getattr(job, "id", None),
        )


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
        if (
            run is not None
            and run.status == "succeeded"
            and status in _RQ_TERMINAL_FAILURE_STATUSES
        ):
            return {
                "job_id": job_id,
                "status": "succeeded",
                "result": run.result,
                "progress": progress,
                "progress_at": progress_at,
                "error": run.error,
            }
    finally:
        db.close()
    latest_result = job.latest_result()
    error = (
        latest_result.exc_string.strip().splitlines()[-1]
        if latest_result is not None and latest_result.exc_string
        else None
    )
    return {
        "job_id": job_id,
        "status": status,
        "result": job.return_value() if status == "finished" else None,
        "progress": progress,
        "progress_at": progress_at,
        "error": error,
    }
