"""Durable managed-site schedule configuration and execution seam.

The public interface is deliberately small: calculate the next occurrence,
save one schedule, claim one due schedule, and start one normal pipeline. The
coordinator task owns polling; this module owns calendar math and the invariant
that a scheduled run is an ordinary crawl-then-analysis pipeline.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PipelineBatch, PipelineSiteRun, Site, SiteSchedule
from app.schemas.site_schedule import SiteScheduleExpected, SiteScheduleOut, SiteScheduleUpdate
from app.services.job_service import active_job_run_ids, enqueue_job_locked, enqueue_locks


class ScheduledPipelineBusyError(RuntimeError):
    """A site already has crawl or analysis work that must finish first."""


def schedule_state(schedule: SiteSchedule | None) -> dict[str, object | None]:
    """Return the small, JSON-safe state used by guarded schedule updates."""

    if schedule is None:
        return {"exists": False}
    return {
        "exists": True,
        "enabled": schedule.enabled,
        "cadence": schedule.cadence,
        "weekday": schedule.weekday,
        "local_time": schedule.local_time.isoformat(),
        "timezone": schedule.timezone,
    }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _local_occurrence(candidate_date: date, local_time: time, zone: ZoneInfo) -> datetime:
    """Resolve one local wall time with explicit daylight-saving semantics.

    A nonexistent wall time is shifted forward by the transition gap: a
    requested 02:30 during a spring-forward transition runs at the next valid
    instant, which is normally 03:30. When a wall time occurs twice during a
    fall-back transition, the first occurrence wins (``fold=0``), so one
    scheduled occurrence never runs twice.
    """
    naive = datetime.combine(candidate_date, local_time.replace(tzinfo=None))
    candidates = [naive.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]

    def round_trip_local(candidate: datetime) -> datetime:
        # Converting through UTC is required here. ``astimezone(zone)`` alone
        # may return the same ZoneInfo object without normalizing a nonexistent
        # wall time.
        return candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)

    valid = [candidate for candidate in candidates if round_trip_local(candidate) == naive]
    if valid:
        # The earlier UTC instant is fold=0 for an ambiguous wall time, and is
        # identical for an ordinary wall time.
        return min(valid, key=lambda candidate: candidate.astimezone(UTC))

    # ZoneInfo represents a gap with one candidate before the transition and
    # one after it. Pick the first candidate whose round-trip local time is
    # later than requested, i.e. the next valid wall-clock instant.
    future = [candidate for candidate in candidates if round_trip_local(candidate) > naive]
    if not future:
        raise ValueError(f"could not resolve local time {naive.isoformat()} in timezone {zone.key}")
    return min(
        future,
        key=round_trip_local,
    )


def next_schedule_run_at(
    *,
    cadence: str,
    local_time: time,
    timezone: str,
    weekday: int | None,
    after: datetime,
) -> datetime:
    """Return the next strictly-future UTC occurrence in the site's timezone.

    See :func:`_local_occurrence` for the explicit DST gap and fold policy.
    """

    zone = ZoneInfo(timezone)
    local_after = _utc(after).astimezone(zone)
    candidate_date: date = local_after.date()

    if cadence == "daily":
        candidate = _local_occurrence(candidate_date, local_time, zone)
        if _utc(candidate) <= _utc(local_after):
            candidate = _local_occurrence(candidate_date + timedelta(days=1), local_time, zone)
        return candidate.astimezone(UTC)

    if cadence != "weekly" or weekday is None:
        raise ValueError("weekly schedules require a weekday")
    days_until = (weekday - local_after.weekday()) % 7
    candidate_date += timedelta(days=days_until)
    candidate = _local_occurrence(candidate_date, local_time, zone)
    if _utc(candidate) <= _utc(local_after):
        candidate = _local_occurrence(candidate_date + timedelta(days=7), local_time, zone)
    return candidate.astimezone(UTC)


def save_schedule(
    db: Session,
    site: Site,
    payload: SiteScheduleUpdate,
    *,
    now: datetime | None = None,
    updated_by: str | None = None,
) -> SiteSchedule:
    """Upsert one schedule and move its durable cursor to the next occurrence."""

    if site.platform == "pool":
        raise ValueError("content-pool sources use their own daily ingestion schedule")
    current_time = _utc(now or datetime.now(UTC))
    schedule = db.scalar(
        select(SiteSchedule).where(SiteSchedule.site_id == site.id).with_for_update()
    )
    if payload.expected is not None:
        current = SiteScheduleExpected.model_validate(schedule_state(schedule))
        if payload.expected != current:
            raise ValueError(
                "site schedule changed after this action was previewed; refresh before confirming"
            )

    if schedule is None:
        schedule = SiteSchedule(site_id=site.id)
        db.add(schedule)

    schedule.enabled = payload.enabled
    schedule.cadence = payload.cadence
    schedule.weekday = payload.weekday
    schedule.local_time = payload.local_time.replace(tzinfo=None)
    schedule.timezone = payload.timezone
    schedule.next_run_at = (
        next_schedule_run_at(
            cadence=payload.cadence,
            local_time=schedule.local_time,
            timezone=payload.timezone,
            weekday=payload.weekday,
            after=current_time,
        )
        if payload.enabled
        else None
    )
    if updated_by is not None:
        if schedule.created_by is None:
            schedule.created_by = updated_by[:255]
        schedule.updated_by = updated_by[:255]
    db.commit()
    db.refresh(schedule)
    return schedule


def load_schedule(db: Session, site_id: int) -> SiteSchedule | None:
    return db.scalar(select(SiteSchedule).where(SiteSchedule.site_id == site_id))


def claim_due_schedule(
    db: Session,
    schedule_id: int,
    *,
    now: datetime | None = None,
) -> SiteSchedule | None:
    """Atomically advance one due cursor before any queue side effect.

    Advancing before enqueueing makes duplicate coordinators harmless. If the
    queue is unavailable, the attempt is recorded as failed and the next normal
    occurrence remains the retry point rather than creating a hot loop.
    """

    current_time = _utc(now or datetime.now(UTC))
    schedule = db.scalar(
        select(SiteSchedule).where(SiteSchedule.id == schedule_id).with_for_update()
    )
    if (
        schedule is None
        or not schedule.enabled
        or schedule.next_run_at is None
        or _utc(schedule.next_run_at) > current_time
    ):
        return None

    schedule.next_run_at = next_schedule_run_at(
        cadence=schedule.cadence,
        local_time=schedule.local_time,
        timezone=schedule.timezone,
        weekday=schedule.weekday,
        after=current_time,
    )
    schedule.last_attempt_at = current_time
    schedule.last_attempt_status = "queued"
    schedule.last_attempt_error = None
    db.commit()
    db.refresh(schedule)
    return schedule


def record_schedule_attempt(
    db: Session,
    schedule_id: int,
    *,
    status: str,
    error: str | None = None,
) -> None:
    schedule = db.get(SiteSchedule, schedule_id, with_for_update=True)
    if schedule is None:
        return
    schedule.last_attempt_status = status
    schedule.last_attempt_error = error[:2000] if error else None
    db.commit()


def start_site_pipeline(
    db: Session,
    site_id: int,
    *,
    schedule_id: int | None = None,
    requested_by: str | None = None,
) -> tuple[PipelineBatch, PipelineSiteRun]:
    """Create and enqueue a one-site crawl-then-analysis pipeline.

    The existing batch workers remain the implementation behind this seam. A
    schedule only supplies timing and provenance; it never bypasses the normal
    job capacity or per-site duplicate checks.
    """

    site = db.get(Site, site_id)
    if site is None:
        raise ValueError(f"site {site_id} not found")
    if site.platform == "pool":
        raise ValueError("content-pool sources cannot generate suggestions")
    if schedule_id is not None:
        schedule = db.get(SiteSchedule, schedule_id)
        if schedule is None or schedule.site_id != site_id:
            raise ValueError("schedule does not belong to this site")

    # Import lazily: the pipeline task imports pipeline_service for its stage
    # state transitions, so importing it at module load would create a cycle.
    from app.tasks.pipeline import ingest_pipeline_site

    # The active-job check, batch creation, and durable enqueue must share the
    # same lock. Otherwise two simultaneous Run now requests can both create a
    # batch before the later enqueue lock rejects one of their jobs.
    with enqueue_locks(site_id, site.tenant_id):
        active_ids = active_job_run_ids(db, site_id, ("ingestion", "analysis"))
        if active_ids:
            raise ScheduledPipelineBusyError(
                f"site {site_id} already has crawl or analysis work queued or running"
            )

        batch = PipelineBatch(schedule_id=schedule_id)
        db.add(batch)
        db.flush()
        item = PipelineSiteRun(batch_id=batch.id, site_id=site_id)
        db.add(item)
        db.commit()
        db.refresh(item)

        try:
            run = enqueue_job_locked(
                db,
                site_id,
                site.tenant_id,
                "ingestion",
                ingest_pipeline_site,
                job_timeout=3600,
                task_kwargs={"batch_site_run_id": item.id},
                requested_by=requested_by,
            )
        except Exception:
            db.rollback()
            from app.services.pipeline_service import update_pipeline_site

            update_pipeline_site(
                db,
                item.id,
                status="failed",
                stage="ingestion",
                error="could not enqueue scheduled pipeline",
            )
            db.commit()
            raise

        db.refresh(item)
        item.ingestion_job_run_id = run.id
        db.commit()
        db.refresh(batch)
        return batch, item


def schedule_output(db: Session, schedule: SiteSchedule) -> SiteScheduleOut:
    """Build the editor-facing schedule, including its latest pipeline state."""

    batch = db.scalar(
        select(PipelineBatch)
        .where(PipelineBatch.schedule_id == schedule.id)
        .order_by(PipelineBatch.id.desc())
        .limit(1)
    )
    site_run = None
    if batch is not None:
        site_run = db.scalar(
            select(PipelineSiteRun)
            .where(PipelineSiteRun.batch_id == batch.id)
            .order_by(PipelineSiteRun.id.desc())
            .limit(1)
        )
    return SiteScheduleOut(
        id=schedule.id,
        site_id=schedule.site_id,
        enabled=schedule.enabled,
        cadence=schedule.cadence,
        weekday=schedule.weekday,
        local_time=schedule.local_time,
        timezone=schedule.timezone,
        next_run_at=schedule.next_run_at,
        last_attempt_at=schedule.last_attempt_at,
        last_attempt_status=schedule.last_attempt_status,
        last_attempt_error=schedule.last_attempt_error,
        last_pipeline_batch_id=batch.id if batch else None,
        last_run_status=batch.status if batch else None,
        last_run_started_at=batch.started_at if batch else None,
        last_run_finished_at=batch.finished_at if batch else None,
        last_run_error=site_run.error
        if site_run and batch and batch.status != "succeeded"
        else None,
        created_by=schedule.created_by,
        updated_by=schedule.updated_by,
    )
