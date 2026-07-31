"""One repeating coordinator for every daily content-pool source."""

import logging

from rq import Repeat, Retry
from rq.exceptions import DuplicateJobError as RQDuplicateJobError
from rq.job import Job
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Site
from app.services.alerts import send_alert
from app.services.job_service import DuplicateJobError, enqueue_job
from app.tasks.ingestion import ingest_site
from app.tasks.queues import ingestion_queue, redis_conn

logger = logging.getLogger(__name__)

_SCHEDULE_JOB_ID = "linkmesh-pool-daily"


def enqueue_daily_pool_ingestions() -> dict:
    """Create normal durable ingestion jobs for every daily pool source.

    This function deliberately never raises. RQ schedules the next repeat only
    from the success path (`Worker.handle_job_success`); a coordinator that ends
    in `failed` takes the whole repeat chain with it, and every pool source stops
    refreshing silently until someone re-runs the registration script by hand.
    A failure therefore costs at most one day's crawl of one source, and is
    reported through the durable alert channel rather than through the job state.
    """
    queued = 0
    skipped = 0
    failed = 0
    try:
        with SessionLocal() as db:
            site_ids = db.scalars(
                select(Site.id)
                .where(Site.platform == "pool", Site.crawl_frequency == "daily")
                .order_by(Site.id)
            ).all()
            for site_id in site_ids:
                try:
                    enqueue_job(db, site_id, "ingestion", ingest_site, job_timeout=3600)
                    queued += 1
                except DuplicateJobError:
                    skipped += 1
                except Exception as error:
                    # One unreachable source must not cost the others their crawl.
                    # enqueue_job commits as it goes, so end whatever transaction
                    # the failure left open before the next site reuses the session.
                    db.rollback()
                    failed += 1
                    logger.exception("failed to enqueue pool ingestion for site %s", site_id)
                    send_alert(
                        "LinkMesh content-pool ingestion could not be queued",
                        {
                            "site_id": site_id,
                            "error": f"{type(error).__name__}: {error}",
                        },
                        kind="pool_ingestion_enqueue_failed",
                        site_id=site_id,
                    )
    except Exception as error:
        # Reached when the coordinator cannot even enumerate its sources.
        logger.exception("daily content-pool coordinator failed")
        send_alert(
            "LinkMesh content-pool coordinator failed",
            {
                "queued": queued,
                "skipped": skipped,
                "failed": failed,
                "error": f"{type(error).__name__}: {error}",
            },
            kind="pool_coordinator_failed",
            site_id=None,
        )
        return {
            "queued": queued,
            "skipped": skipped,
            "failed": failed,
            "error": f"{type(error).__name__}: {error}",
        }
    return {"queued": queued, "skipped": skipped, "failed": failed}


def schedule_pool_ingestion() -> Job:
    """Register one immediate coordinator that repeats daily and discovers new pools."""
    try:
        return ingestion_queue.enqueue(
            enqueue_daily_pool_ingestions,
            job_id=_SCHEDULE_JOB_ID,
            unique=True,
            repeat=Repeat(
                times=settings.pool_poll_repeat_count,
                interval=settings.pool_poll_interval_seconds,
            ),
            # The body swallows its own failures, so this only covers a worker
            # dying mid-run. Without it that death is terminal for the chain.
            retry=Retry(max=2, interval=[60, 300]),
            job_timeout=300,
            result_ttl=settings.pool_poll_interval_seconds,
            description="Daily content-pool ingestion coordinator",
        )
    except RQDuplicateJobError:
        return Job.fetch(_SCHEDULE_JOB_ID, connection=redis_conn)
