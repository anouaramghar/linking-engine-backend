"""Publication worker (sequence 4.3): apply approved links via each site's connector.

Safe publication (Phase 0, finding 5): each suggestion is claimed atomically
(approved -> applying) inside a transaction that also holds a per-source-article
advisory lock for the whole remote write. Concurrent workers cannot double-publish,
writes to one source article are serialized, and a reviewer rejecting mid-flight
blocks on the row lock and gets a clean 409 once the publish commits. Any failure
or crash rolls the claim back to 'approved' for retry (A8); apply_link's exact-href
check keeps retries idempotent when WordPress succeeded but the commit didn't.

A suggestion failure does not stop later batch entries; after the loop, any
failure is re-raised so RQ retries it and terminal failures produce durable alerts.
Publication never writes internal_links — the applied link is detected at the next
crawl (A9, ingestion is the single source of truth). Durable accounting spans all
attempts while retaining latest-attempt failure and skip diagnostics.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import joinedload

from app.connectors.registry import get_connector
from app.db import SessionLocal
from app.models import Article, JobRun, Site, Suggestion
from app.services.publication_progress import (
    begin_publication_attempt,
    complete_publication_success,
    record_publication_applied,
    record_publication_failure,
    record_publication_skip,
)
from app.services.job_service import record_progress, record_progress_durably, run_durably

logger = logging.getLogger(__name__)

_PUBLISH_LOCK_NAMESPACE = 0x4C50  # "LP" - keyed by source article; see namespace registry


def publish_approved(site_id: int, job_run_id: int | None = None) -> dict:
    return run_durably(job_run_id, _publish_approved, site_id)


def _publish_approved(site_id: int, job_run_id: int | None = None) -> dict:
    db = SessionLocal()
    progress_db = SessionLocal() if job_run_id is not None else None
    try:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        connector = get_connector(site)
        batch = db.execute(
            select(Suggestion.id, Suggestion.source_article_id)
            .where(Suggestion.site_id == site_id, Suggestion.status == "approved")
            .order_by(Suggestion.id)
        ).all()
        run = db.get(JobRun, job_run_id) if job_run_id is not None else None
        progress = begin_publication_attempt(
            run.progress if run is not None else None,
            len(batch),
        )
        failure_details: list[str] = []
        record_progress(db, job_run_id, **progress)
        db.commit()  # end the read transaction — each claim below gets its own

        for suggestion_id, source_article_id in batch:
            try:
                # one publication update per source article at a time
                db.execute(
                    select(func.pg_advisory_xact_lock(_PUBLISH_LOCK_NAMESPACE, source_article_id))
                ).scalar_one()
                claim = db.execute(
                    update(Suggestion)
                    .where(
                        Suggestion.id == suggestion_id,
                        Suggestion.status == "approved",
                        Suggestion.source_article.has(Article.is_active.is_(True)),
                        Suggestion.target_article.has(Article.is_active.is_(True)),
                    )
                    .values(status="applying")
                )
                if claim.rowcount == 0:  # Rejected, claimed, or inactive (Phase 0, finding 3).
                    db.rollback()
                    progress = record_publication_skip(progress)
                    # The claim rollback also discards same-session progress. Reusing
                    # one independent session avoids connection churn; committing each
                    # skip keeps a killed all-skipped attempt fully accounted for.
                    record_progress_durably(
                        job_run_id,
                        session=progress_db,
                        **progress,
                    )
                    continue
                suggestion = db.get(
                    Suggestion,
                    suggestion_id,
                    options=[
                        joinedload(Suggestion.source_article),
                        joinedload(Suggestion.target_article),
                    ],
                )
                connector.apply_link(suggestion)
                # 'applied' is set exclusively here — lifecycle guarantee
                suggestion.status = "applied"
                suggestion.applied_at = datetime.now(timezone.utc)
                committed_progress = record_publication_applied(progress)
                record_progress(db, job_run_id, **committed_progress)
                db.commit()  # releases the advisory and row locks
                # counted only after the commit — a failed commit is a 'failed', not an 'applied'
                progress = committed_progress
            except Exception as error:
                db.rollback()  # claim undone — suggestion is 'approved' again for retry
                logger.exception("apply_link failed for suggestion %s", suggestion_id)
                progress = record_publication_failure(progress)
                failure_details.append(
                    f"suggestion {suggestion_id}: {type(error).__name__}: {error}"
                )
                record_progress_durably(
                    job_run_id,
                    session=progress_db,
                    **progress,
                )
        if progress["attempt_failed"]:
            # Successful suggestions were committed individually and are skipped on
            # retry. Failed claims rolled back to "approved", so raising lets RQ
            # retry them and emit the exhausted-retry alert if they keep failing.
            details = "; ".join(failure_details[:3])
            raise RuntimeError(
                f"{progress['attempt_failed']} publication suggestion(s) failed: {details}"
            )
        progress = complete_publication_success(progress)
        record_progress_durably(
            job_run_id,
            session=progress_db,
            **progress,
        )
        return {
            "applied": progress["applied"],
            "failed": progress["failed"],
            "skipped": progress["skipped"],
        }
    finally:
        db.close()
        if progress_db is not None:
            progress_db.close()
