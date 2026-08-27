import logging
from datetime import UTC, datetime

from rq import get_current_job
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Site
from app.services.ingestion_service import run_ingestion
from app.services.job_service import check_job_cancellation, run_durably
from app.services.pool_source_policy import (
    PoolSourceFetchError,
    PoolSourcePolicyError,
    expire_pool_target_suggestions,
    require_approved_pool_source,
)
from app.services.pool_source_audit import record_pool_source_audit_event

logger = logging.getLogger(__name__)


def ingest_site(site_id: int, job_run_id: int | None = None) -> dict:
    return run_durably(job_run_id, run_ingestion, site_id)


def _run_pool_ingestion(site_id: int, job_run_id: int | None = None) -> dict:
    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        require_approved_pool_source(site)
    result = run_ingestion(site_id, job_run_id=job_run_id)
    check_job_cancellation(job_run_id)
    _record_pool_ingestion_success(site_id)
    return result


def _is_terminal_attempt() -> bool:
    job = get_current_job()
    retries_left = getattr(job, "retries_left", None)
    return job is None or retries_left is None or retries_left <= 0


def _record_pool_ingestion_success(site_id: int) -> None:
    with SessionLocal() as db:
        site = db.scalar(select(Site).where(Site.id == site_id).with_for_update())
        if site is None or site.platform != "pool":
            return
        site.pool_source_consecutive_failures = 0
        site.pool_source_quarantine_reason = None
        db.commit()


def _record_pool_ingestion_failure(site_id: int, error: Exception) -> None:
    with SessionLocal() as db:
        site = db.scalar(select(Site).where(Site.id == site_id).with_for_update())
        if site is None or site.platform != "pool":
            return
        site.pool_source_consecutive_failures += 1
        should_quarantine = (
            site.pool_source_consecutive_failures >= settings.pool_quarantine_failure_threshold
        )
        if should_quarantine:
            newly_quarantined = not site.pool_source_quarantined
            site.pool_source_quarantined = True
            site.pool_source_quarantined_at = datetime.now(UTC)
            site.pool_source_quarantine_reason = str(error)[:2000]
            expire_pool_target_suggestions(db, site.id, reason="quarantined")
            if newly_quarantined:
                record_pool_source_audit_event(
                    db,
                    site,
                    "quarantined",
                    "system",
                    reason=site.pool_source_quarantine_reason,
                )
        db.commit()


def ingest_pool_site(site_id: int, job_run_id: int | None = None) -> dict:
    """Run an approved pool ingestion and quarantine repeated terminal failures."""
    try:
        result = run_durably(job_run_id, _run_pool_ingestion, site_id)
    except PoolSourceFetchError as error:
        if _is_terminal_attempt():
            try:
                _record_pool_ingestion_failure(site_id, error)
            except Exception:
                logger.exception("failed to update pool source state for site %s", site_id)
        raise
    except PoolSourcePolicyError:
        raise
    return result
