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
from rq.command import send_stop_job_command
from rq.exceptions import AbandonedJobError, NoSuchJobError
from rq.job import Callback, Job
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, engine
from app.models import IngestionRun, JobRun, Site
from app.services.alerts import record_alert, send_alert
from app.services.publication_progress import mark_publication_failure
from app.tasks.queues import (
    analysis_queue,
    ingestion_queue,
    publication_preparation_queue,
    publication_queue,
    redis_conn,
)

_ENQUEUE_LOCK_NAMESPACE = 0x4C4A  # "LJ" — serializes enqueues per site
_TENANT_ENQUEUE_LOCK_NAMESPACE = 0x4C54  # "LT" — serializes tenant quota checks

_QUEUES = {
    "ingestion": ingestion_queue,
    "analysis": analysis_queue,
    "publication_preparation": publication_preparation_queue,
    "publication": publication_queue,
}
_RQ_ACTIVE_STATUSES = {"queued", "started", "deferred", "scheduled"}
_RQ_TERMINAL_FAILURE_STATUSES = {"failed", "stopped", "canceled"}
_RQ_CANCELLED_STATUSES = {"stopped", "canceled", "cancelled"}
_PUBLIC_JOB_STATUSES = {
    "queued": "queued",
    "deferred": "queued",
    "scheduled": "queued",
    "started": "running",
    "running": "running",
    "finished": "succeeded",
    "succeeded": "succeeded",
    "failed": "failed",
    "stopped": "cancelled",
    "canceled": "cancelled",
    "cancelled": "cancelled",
}
_DURABLE_ACTIVE_STATUSES = ("queued", "running", "cancel_requested")
_UNASSIGNED_QUEUE_ID_GRACE_SECONDS = 30

logger = logging.getLogger(__name__)


def _count_result_value(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return max(0, int(value))
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return 0


def _job_outcome(result: object) -> str:
    """Classify a successful task result without changing its durable contract."""
    if not isinstance(result, dict):
        return "succeeded"
    if result.get("partial") is True:
        return "partial"

    failure_count = sum(
        _count_result_value(result.get(key))
        for key in ("failed", "errors", "error_count", "rejected", "skipped", "blocked")
    )
    success_count = sum(
        _count_result_value(result.get(key))
        for key in (
            "applied",
            "created",
            "imported",
            "inserted",
            "links",
            "suggestions",
            "updated",
        )
    )
    return "partial" if failure_count and success_count else "succeeded"


def _record_completed_job_alert(run: JobRun, result: dict) -> None:
    outcome = _job_outcome(result)
    label = "partially completed" if outcome == "partial" else "completed"
    record_alert(
        f"LinkMesh {run.kind} job {label}",
        {
            "site_id": run.site_id,
            "kind": run.kind,
            "job_run_id": run.id,
            "attempts": run.attempts,
            "outcome": outcome,
        },
        kind=f"job_{outcome if outcome == 'partial' else 'succeeded'}",
        site_id=run.site_id,
        dedupe=False,
    )


class DuplicateJobError(Exception):
    def __init__(self, run: JobRun):
        self.run = run
        super().__init__(f"{run.kind} job already {run.status} for site {run.site_id}")


class JobCancelled(Exception):
    """Raised by a task at a cooperative cancellation checkpoint."""


class JobCancellationConflict(Exception):
    """Raised when a terminal job cannot be cancelled anymore."""

    def __init__(self, run: JobRun):
        self.run = run
        super().__init__(f"job run {run.id} is already {run.status}")


class JobCapacityError(DuplicateJobError):
    def __init__(self, tenant_id: int, limit: int):
        self.tenant_id = tenant_id
        self.limit = limit
        Exception.__init__(self, f"tenant {tenant_id} already has {limit} active jobs")


def active_job_run_ids(
    db: Session,
    site_ids: int | list[int],
    kinds: str | tuple[str, ...],
) -> list[int]:
    """Return the durable active-job snapshot used by previews and confirms."""

    selected_sites = [site_ids] if isinstance(site_ids, int) else site_ids
    selected_kinds = (kinds,) if isinstance(kinds, str) else kinds
    if not selected_sites:
        return []
    return sorted(
        db.scalars(
            select(JobRun.id).where(
                JobRun.site_id.in_(selected_sites),
                JobRun.kind.in_(selected_kinds),
                JobRun.status.in_(_DURABLE_ACTIVE_STATUSES),
            )
        ).all()
    )


def require_active_job_snapshot(
    db: Session,
    *,
    site_ids: int | list[int],
    kinds: str | tuple[str, ...],
    expected_ids: list[int],
) -> None:
    """Reject a staged start when active jobs changed after its preview."""

    if active_job_run_ids(db, site_ids, kinds) != expected_ids:
        raise ValueError(
            "active jobs changed after this action was previewed; refresh before confirming"
        )


class NonRetryableTaskError(RuntimeError):
    """A terminal task failure that RQ must not schedule again."""


@contextmanager
def _enqueue_locks(site_id: int, tenant_id: int) -> Iterator[None]:
    # The work session commits the durable row before enqueueing. A dedicated
    # transaction keeps the per-site advisory lock across both work commits.
    with engine.begin() as lock_connection:
        lock_connection.execute(
            select(func.pg_advisory_xact_lock(_TENANT_ENQUEUE_LOCK_NAMESPACE, tenant_id))
        ).scalar_one()
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


def _mark_run_cancelled(
    run: JobRun,
    error: str | None = None,
    *,
    now: datetime | None = None,
) -> None:
    run.status = "cancelled"
    run.result = {"cancelled": True}
    if error:
        run.error = error[:2000]
    run.finished_at = now or datetime.now(timezone.utc)


def check_job_cancellation(job_run_id: int | None) -> None:
    """Raise when the API has requested cancellation for this durable run.

    The task's working session can hold a long transaction, so this checkpoint
    deliberately reads through a fresh session and observes the API commit.
    """
    if job_run_id is None:
        return
    db = SessionLocal()
    try:
        status = db.scalar(select(JobRun.status).where(JobRun.id == job_run_id))
    finally:
        db.close()
    if status in {"cancel_requested", "cancelled"}:
        raise JobCancelled("job cancellation requested")


def _mark_run_lost(run: JobRun) -> None:
    if run.status == "cancel_requested":
        _mark_run_cancelled(run, run.error or "job cancellation requested")
        return
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
        dedupe=False,
    )


def reconcile_active_job_runs(db: Session, runs: list[JobRun]) -> list[JobRun]:
    """Keep only active work, failing rows whose RQ job has disappeared.

    A newly inserted row briefly has no queue id while enqueueing happens outside
    its first transaction, so that narrow window receives a short grace period.
    If Redis itself is unavailable, retain the durable state instead of falsely
    declaring every job lost.
    """
    now = datetime.now(timezone.utc)
    active: list[JobRun] = []
    changed = False
    for run in runs:
        enqueued_at = run.enqueued_at
        if enqueued_at.tzinfo is None:
            enqueued_at = enqueued_at.replace(tzinfo=timezone.utc)
        if (
            run.queue_job_id is None
            and (now - enqueued_at).total_seconds() < _UNASSIGNED_QUEUE_ID_GRACE_SECONDS
        ):
            active.append(run)
            continue
        try:
            still_in_queue = _still_in_queue(run)
        except Exception:
            logger.exception(
                "could not reconcile active job run %s against Redis; preserving durable state",
                run.id,
            )
            active.append(run)
            continue
        if still_in_queue:
            active.append(run)
            continue
        # The worker commits the durable terminal state before RQ records its own
        # terminal state. Refresh after checking RQ so a completion racing this
        # request is not mislabeled as a lost job from our earlier query snapshot.
        db.refresh(run)
        if run.status not in _DURABLE_ACTIVE_STATUSES:
            continue
        _mark_run_lost(run)
        changed = True
    if changed:
        db.commit()
    return active


def _enqueue_job_locked(
    db: Session,
    site_id: int,
    tenant_id: int,
    kind: str,
    fn,
    job_timeout: int,
    task_kwargs: dict | None = None,
    requested_by: str | None = None,
) -> JobRun:
    """Create the durable run row, then enqueue. Raises DuplicateJobError while an
    active run of this kind exists for the site."""
    active = db.scalars(
        select(JobRun).where(
            JobRun.site_id == site_id,
            JobRun.kind == kind,
            JobRun.status.in_(_DURABLE_ACTIVE_STATUSES),
        )
    ).all()
    for run in active:
        if not _still_in_queue(run):  # finding 7's exact failure: the job disappeared
            db.refresh(run)
            if run.status in _DURABLE_ACTIVE_STATUSES:
                _mark_run_lost(run)
    still_active = [run for run in active if run.status in _DURABLE_ACTIVE_STATUSES]
    if still_active:
        db.commit()  # keep the reconciliations
        raise DuplicateJobError(still_active[0])

    tenant_active = db.scalars(
        select(JobRun)
        .join(Site, Site.id == JobRun.site_id)
        .where(
            Site.tenant_id == tenant_id,
            JobRun.status.in_(_DURABLE_ACTIVE_STATUSES),
        )
    ).all()
    tenant_active = reconcile_active_job_runs(db, list(tenant_active))
    if len(tenant_active) >= settings.max_active_jobs_per_tenant:
        raise JobCapacityError(tenant_id, settings.max_active_jobs_per_tenant)

    run = JobRun(site_id=site_id, kind=kind, requested_by=requested_by)
    db.add(run)
    db.commit()
    job = _QUEUES[kind].enqueue(
        fn,
        site_id,
        job_run_id=run.id,
        **(task_kwargs or {}),
        job_timeout=job_timeout,
        retry=Retry(max=2, interval=[30, 120]),  # limited automatic retries
        on_stopped=Callback(handle_job_stopped),
    )
    run.queue_job_id = job.id
    db.commit()
    db.refresh(run)
    return run


def cancel_job_run(
    db: Session,
    job_run_id: int,
    reason: str = "Cancelled by operator",
    *,
    commit: bool = True,
    allow_terminal: bool = False,
) -> JobRun:
    """Request and, when possible, complete cancellation of one durable run.

    ``cancel_requested`` is committed before the RQ command is sent so a worker
    that is already inside a task can observe the request. Started jobs remain in
    that state until a cooperative checkpoint or the RQ stop callback reaches the
    terminal ``cancelled`` state. Queued jobs have no useful checkpoint, so they
    are finalized immediately after RQ removes them.
    """
    run = db.get(JobRun, job_run_id, with_for_update=True)
    if run is None:
        raise LookupError(f"job run {job_run_id} not found")
    if run.status == "cancelled":
        return run
    if run.status not in _DURABLE_ACTIVE_STATUSES:
        if allow_terminal:
            return run
        raise JobCancellationConflict(run)

    reason = reason[:2000]
    run.status = "cancel_requested"
    run.error = reason
    run.finished_at = None
    run.result = None
    if commit:
        db.commit()

    if not run.queue_job_id:
        _mark_run_cancelled(run, reason)
        if commit:
            db.commit()
        return run

    try:
        job = Job.fetch(run.queue_job_id, connection=redis_conn)
        rq_status = job.get_status(refresh=True)
        rq_status = getattr(rq_status, "value", rq_status)
        if rq_status in {"queued", "deferred", "scheduled"}:
            job.cancel()
            _mark_run_cancelled(run, reason)
        elif rq_status == "started":
            send_stop_job_command(redis_conn, job.id)
        elif rq_status in _RQ_CANCELLED_STATUSES:
            _mark_run_cancelled(run, reason)
        elif rq_status in {"finished", "succeeded"}:
            # If the wrapper has not committed success yet, the cancellation
            # request wins when its final row lock is acquired.
            _mark_run_cancelled(run, reason)
    except NoSuchJobError:
        # An explicitly cancelled job that is already gone from Redis cannot
        # execute any more work, so the durable row can safely settle here.
        _mark_run_cancelled(run, reason)
    except Exception:
        # The durable request is still useful if Redis is temporarily down. The
        # worker's fresh-session checkpoints, or a later retry of this action,
        # can finish the transition.
        logger.exception("could not send cancellation for RQ job %s", run.queue_job_id)

    if commit:
        db.commit()
        db.refresh(run)
    return run


def enqueue_job(
    db: Session,
    site_id: int,
    kind: str,
    fn,
    job_timeout: int,
    task_kwargs: dict | None = None,
    requested_by: str | None = None,
) -> JobRun:
    tenant_id = db.scalar(select(Site.tenant_id).where(Site.id == site_id))
    if tenant_id is None:
        raise ValueError(f"site {site_id} not found")
    with _enqueue_locks(site_id, tenant_id):
        return _enqueue_job_locked(
            db,
            site_id,
            tenant_id,
            kind,
            fn,
            job_timeout,
            task_kwargs=task_kwargs,
            requested_by=requested_by,
        )


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


def record_progress_durably(
    job_run_id: int | None,
    *,
    session: Session | None = None,
    **fields,
) -> None:
    """Commit progress independently from a task transaction that may roll back.

    A caller may reuse a session to avoid per-item construction; every call still
    commits so the latest checkpoint survives a task-session rollback or worker death.
    """
    if job_run_id is None:
        return
    owns_session = session is None
    db = session if session is not None else SessionLocal()
    try:
        record_progress(db, job_run_id, **fields)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("failed to commit progress for job run %s", job_run_id)
    finally:
        if owns_session:
            db.close()


def _mark_job_failure_progress(run: JobRun, *, terminal: bool, progress_at: datetime) -> None:
    if run.kind != "publication":
        return
    updated = mark_publication_failure(run.progress, terminal=terminal)
    if updated is None:
        return
    run.progress = updated
    run.progress_at = progress_at


def _run_task_body(fn, site_id: int, job_run_id: int | None, task_kwargs: dict) -> dict:
    parameters = signature(fn).parameters.values()
    kwargs = dict(task_kwargs)
    if any(
        parameter.name == "job_run_id" or parameter.kind == Parameter.VAR_KEYWORD
        for parameter in parameters
    ):
        kwargs["job_run_id"] = job_run_id
    return fn(site_id, **kwargs)


def run_durably(job_run_id: int | None, fn, site_id: int, **task_kwargs) -> dict:
    """Task-body wrapper: records start, attempt count, result or error. Re-raises so
    RQ can retry; the final attempt's failure stays recorded. Tolerates a missing row
    (job enqueued before this table existed, or site deleted meanwhile)."""
    db = SessionLocal()
    try:
        run = db.get(JobRun, job_run_id) if job_run_id is not None else None
        if run is not None:
            if run.status in {"cancel_requested", "cancelled"}:
                _mark_run_cancelled(run, run.error)
                db.commit()
                return {"cancelled": True}
            run.status = "running"
            run.attempts += 1
            run.started_at = datetime.now(timezone.utc)
            run.finished_at = None
            run.result = None
            db.commit()
        try:
            result = _run_task_body(fn, site_id, job_run_id, task_kwargs)
        except JobCancelled as error:
            if run is not None:
                current = db.get(JobRun, run.id, with_for_update=True, populate_existing=True)
                if current is not None and current.status != "succeeded":
                    _mark_run_cancelled(current, current.error or str(error))
                    db.commit()
            return {"cancelled": True}
        except Exception as e:
            error = str(e)[:2000]
            current = (
                db.get(JobRun, run.id, with_for_update=True, populate_existing=True)
                if run is not None
                else None
            )
            if current is not None and current.status in {"cancel_requested", "cancelled"}:
                _mark_run_cancelled(current, current.error or error)
                db.commit()
                return {"cancelled": True}
            current_job = get_current_job()
            retries_left = getattr(current_job, "retries_left", None)
            non_retryable = isinstance(e, NonRetryableTaskError)
            if non_retryable and current_job is not None:
                # RQ checks this same in-memory Job after the function raises.
                current_job.retries_left = 0
            final_attempt = (
                non_retryable or current_job is None or retries_left is None or retries_left <= 0
            )
            if current is not None:
                now = datetime.now(timezone.utc)
                run = current
                if run.kind == "publication":
                    # Publication progress commits through an independent session,
                    # while this wrapper retains its JobRun identity for the attempt.
                    db.refresh(run, attribute_names=["progress", "progress_at"])
                    _mark_job_failure_progress(
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
                    dedupe=False,
                )
            raise
        if run is not None:
            run = db.get(JobRun, run.id, with_for_update=True, populate_existing=True)
            if run is not None and (
                run.status in {"cancel_requested", "cancelled"}
                or (isinstance(result, dict) and result.get("cancelled") is True)
            ):
                _mark_run_cancelled(run, run.error)
                db.commit()
                return {"cancelled": True}
            if run is not None:
                run.status = "succeeded"
                run.result = result
                run.error = None  # clear any earlier attempt's failure
                run.finished_at = datetime.now(timezone.utc)
            db.commit()
            if run is not None:
                _record_completed_job_alert(run, result)
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
            select(JobRun).where(JobRun.queue_job_id == job_id).with_for_update()
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

        cancellation_requested = run.status == "cancel_requested" or alert_kind == "job_cancelled"
        if cancellation_requested:
            will_retry = False
        if will_retry is None:
            retries_left = getattr(job, "retries_left", None)
            will_retry = retries_left is not None and retries_left > 0
        if run.status in {"failed", "cancelled"}:
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
        transition_error = run.error if cancellation_requested and run.error else error
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
                ingestion_run.status = "cancelled" if cancellation_requested else "failed"
                ingestion_run.error = transition_error
                ingestion_run.finished_at = now

        run.status = (
            "cancelled" if cancellation_requested else ("queued" if will_retry else "failed")
        )
        run.error = transition_error
        run.finished_at = None if will_retry else now
        if run.kind == "publication":
            _mark_job_failure_progress(
                run,
                terminal=not will_retry,
                progress_at=now,
            )
        # This includes the narrow case where the child died after committing
        # durable success but before RQ recorded FINISHED. Do not expose a stale
        # successful result while RQ retries, or after an intentional stop.
        run.result = {"cancelled": True} if cancellation_requested else None

        if not will_retry:
            alert_subject = f"LinkMesh {run.kind} job {alert_verb}"
            alert_payload = {
                "site_id": run.site_id,
                "kind": run.kind,
                "job_run_id": run.id,
                "attempts": run.attempts,
                "error": transition_error,
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
                dedupe=False,
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
    """Mark an intentionally stopped RQ job cancelled; this callback never raises."""
    del connection
    try:
        _reconcile_interrupted_job(
            job,
            "job stopped intentionally",
            will_retry=False,
            alert_kind="job_cancelled",
            alert_verb="cancelled",
        )
    except Exception:
        logger.exception(
            "unexpected error in stopped job callback for RQ job %s",
            getattr(job, "id", None),
        )


def get_job_status(job_id: str) -> dict | None:
    """Return one stable status vocabulary across live RQ and durable DB views."""
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
    rq_status = job.get_status()
    status_value = getattr(rq_status, "value", rq_status)
    rq_status_name = str(status_value)
    status = _PUBLIC_JOB_STATUSES.get(rq_status_name, "failed")
    db = SessionLocal()
    try:
        # The live RQ view knows nothing of progress — merge it from the durable row,
        # because a UI polls this endpoint precisely while the job is still in Redis.
        run = db.scalars(select(JobRun).where(JobRun.queue_job_id == job_id)).first()
        progress = run.progress if run is not None else None
        progress_at = run.progress_at if run is not None else None
        durable_error = run.error if run is not None else None
        durable_result = run.result if run is not None and run.status == "succeeded" else None
        if run is not None and run.status in {"cancel_requested", "cancelled"}:
            return {
                "job_id": job_id,
                "status": run.status,
                "result": run.result,
                "progress": progress,
                "progress_at": progress_at,
                "error": run.error,
            }
        if (
            run is not None
            and run.status == "succeeded"
            and rq_status_name in _RQ_TERMINAL_FAILURE_STATUSES
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
        else durable_error
    )
    result = None
    if status == "succeeded":
        result = job.return_value()
        if result is None:
            result = durable_result
    return {
        "job_id": job_id,
        "status": status,
        "result": result,
        "progress": progress,
        "progress_at": progress_at,
        "error": error,
    }
