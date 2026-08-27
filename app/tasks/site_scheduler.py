"""Repeatable coordinator for managed-site crawl-and-analysis schedules."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from rq import Repeat, Retry
from rq.exceptions import DuplicateJobError as RQDuplicateJobError
from rq.job import Job
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Site, SiteSchedule
from app.services.alerts import send_alert
from app.services.site_schedule_service import (
    ScheduledPipelineBusyError,
    claim_due_schedule,
    record_schedule_attempt,
    start_site_pipeline,
)
from app.tasks.queues import ingestion_queue, redis_conn

logger = logging.getLogger(__name__)

_SCHEDULE_JOB_ID = "linkmesh-site-schedule-coordinator"


def _due_schedule_ids() -> list[int]:
    with SessionLocal() as db:
        return db.scalars(
            select(SiteSchedule.id)
            .join(Site, Site.id == SiteSchedule.site_id)
            .where(
                Site.platform != "pool",
                SiteSchedule.enabled.is_(True),
                SiteSchedule.next_run_at.is_not(None),
                SiteSchedule.next_run_at <= datetime.now(UTC),
            )
            .order_by(SiteSchedule.next_run_at, SiteSchedule.id)
            .limit(100)
        ).all()


def _process_schedule(schedule_id: int) -> str:
    with SessionLocal() as db:
        schedule = claim_due_schedule(db, schedule_id)
        if schedule is None:
            return "not_due"
        site = db.get(Site, schedule.site_id)
        if site is None or site.platform == "pool":
            record_schedule_attempt(
                db,
                schedule.id,
                status="failed",
                error="scheduled site no longer exists or is not a managed site",
            )
            return "failed"
        try:
            start_site_pipeline(
                db,
                site.id,
                schedule_id=schedule.id,
                requested_by="site-scheduler",
            )
        except ScheduledPipelineBusyError as error:
            record_schedule_attempt(db, schedule.id, status="skipped", error=str(error))
            return "skipped"
        except Exception as error:
            logger.exception("failed to enqueue scheduled pipeline for site %s", site.id)
            record_schedule_attempt(
                db,
                schedule.id,
                status="failed",
                error=f"{type(error).__name__}: {error}",
            )
            send_alert(
                "LinkMesh scheduled pipeline could not be queued",
                {
                    "site_id": site.id,
                    "schedule_id": schedule.id,
                    "error": f"{type(error).__name__}: {error}",
                },
                kind="site_schedule_enqueue_failed",
                site_id=site.id,
            )
            return "failed"
        record_schedule_attempt(db, schedule.id, status="queued")
        return "queued"


def enqueue_due_site_schedules() -> dict[str, int]:
    """Queue due schedules without letting one site's failure stop the rest."""

    counts = {"queued": 0, "skipped": 0, "failed": 0, "not_due": 0}
    try:
        for schedule_id in _due_schedule_ids():
            outcome = _process_schedule(schedule_id)
            counts[outcome] += 1
    except Exception as error:
        logger.exception("managed-site schedule coordinator failed")
        send_alert(
            "LinkMesh managed-site schedule coordinator failed",
            {**counts, "error": f"{type(error).__name__}: {error}"},
            kind="site_schedule_coordinator_failed",
            site_id=None,
        )
        counts["failed"] += 1
    return counts


def schedule_site_automation() -> Job:
    """Register the one repeating coordinator used by all managed sites."""

    try:
        return ingestion_queue.enqueue(
            enqueue_due_site_schedules,
            job_id=_SCHEDULE_JOB_ID,
            unique=True,
            repeat=Repeat(
                times=settings.site_schedule_repeat_count,
                interval=settings.site_schedule_poll_interval_seconds,
            ),
            retry=Retry(max=2, interval=[60, 300]),
            job_timeout=300,
            result_ttl=settings.site_schedule_poll_interval_seconds,
            description="Managed-site crawl and analysis schedule coordinator",
        )
    except RQDuplicateJobError:
        return Job.fetch(_SCHEDULE_JOB_ID, connection=redis_conn)
