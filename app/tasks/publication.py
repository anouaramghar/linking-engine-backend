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
crawl (A9, ingestion is the single source of truth). Durable per-attempt state is
recorded through the job-run wrapper (finding 7).
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import joinedload

from app.connectors.registry import get_connector
from app.db import SessionLocal
from app.models import Article, JobRun, Site, Suggestion
from app.services.job_service import record_progress, record_progress_durably, run_durably

logger = logging.getLogger(__name__)

_PUBLISH_LOCK_NAMESPACE = 0x4C50  # "LP" - keyed by source article; see namespace registry


def publish_approved(site_id: int, job_run_id: int | None = None) -> dict:
    return run_durably(job_run_id, _publish_approved, site_id)


def _publish_approved(site_id: int, job_run_id: int | None = None) -> dict:
    db = SessionLocal()
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
        previous = (
            run.progress
            if run is not None
            and isinstance(run.progress, dict)
            and run.progress.get("stage") == "publishing"
            else {}
        )
        applied = previous.get("applied", 0)
        skipped = previous.get("skipped", 0)
        attempt_skipped = 0
        attempt_failures = previous.get("attempt_failures", previous.get("failed", 0))
        attempt_failed = 0
        failure_details: list[str] = []
        total = previous.get("total", len(batch))
        record_progress(
            db,
            job_run_id,
            stage="publishing",
            applied=applied,
            failed=0,
            skipped=skipped,
            total=total,
            attempt_skipped=attempt_skipped,
            attempt_failed=attempt_failed,
            attempt_failures=attempt_failures,
            failure_state=None,
        )
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
                    attempt_skipped += 1
                    skipped = max(skipped, attempt_skipped)
                    record_progress_durably(
                        job_run_id,
                        stage="publishing",
                        applied=applied,
                        failed=0,
                        skipped=skipped,
                        total=total,
                        attempt_skipped=attempt_skipped,
                        attempt_failed=attempt_failed,
                        attempt_failures=attempt_failures,
                        failure_state=None,
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
                record_progress(
                    db,
                    job_run_id,
                    stage="publishing",
                    applied=applied + 1,
                    failed=0,
                    skipped=skipped,
                    total=total,
                    attempt_skipped=attempt_skipped,
                    attempt_failed=attempt_failed,
                    attempt_failures=attempt_failures,
                    failure_state=None,
                )
                db.commit()  # releases the advisory and row locks
                # counted only after the commit — a failed commit is a 'failed', not an 'applied'
                applied += 1
            except Exception as error:
                db.rollback()  # claim undone — suggestion is 'approved' again for retry
                logger.exception("apply_link failed for suggestion %s", suggestion_id)
                attempt_failed += 1
                attempt_failures += 1
                failure_details.append(
                    f"suggestion {suggestion_id}: {type(error).__name__}: {error}"
                )
                record_progress_durably(
                    job_run_id,
                    stage="publishing",
                    applied=applied,
                    failed=0,
                    skipped=skipped,
                    total=total,
                    attempt_skipped=attempt_skipped,
                    attempt_failed=attempt_failed,
                    attempt_failures=attempt_failures,
                    failure_state=None,
                )
        if attempt_failed:
            # Successful suggestions were committed individually and are skipped on
            # retry. Failed claims rolled back to "approved", so raising lets RQ
            # retry them and emit the exhausted-retry alert if they keep failing.
            details = "; ".join(failure_details[:3])
            raise RuntimeError(
                f"{attempt_failed} publication suggestion(s) failed: {details}"
            )
        skipped = max(total - applied, 0)
        record_progress_durably(
            job_run_id,
            stage="publishing",
            applied=applied,
            failed=0,
            skipped=skipped,
            total=total,
            attempt_skipped=attempt_skipped,
            attempt_failed=0,
            attempt_failures=attempt_failures,
            failure_state=None,
        )
        return {"applied": applied, "failed": 0, "skipped": skipped}
    finally:
        db.close()
