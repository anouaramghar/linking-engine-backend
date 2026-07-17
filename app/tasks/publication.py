"""Publication worker (sequence 4.3): apply approved links via each site's connector.

Safe publication (Phase 0, finding 5): each suggestion is claimed atomically
(approved -> applying) inside a transaction that also holds a per-source-article
advisory lock for the whole remote write. Concurrent workers cannot double-publish,
writes to one source article are serialized, and a reviewer rejecting mid-flight
blocks on the row lock and gets a clean 409 once the publish commits. Any failure
or crash rolls the claim back to 'approved' for retry (A8); apply_link's exact-href
check keeps retries idempotent when WordPress succeeded but the commit didn't.

One suggestion's failure never interrupts the batch. Publication never writes
internal_links — the applied link is detected at the next crawl (A9, ingestion is
the single source of truth). Durable per-attempt records land with the job-run
slice (finding 7); until then failures go to the log.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import joinedload

from app.connectors.registry import get_connector
from app.db import SessionLocal
from app.models import Article, Site, Suggestion
from app.services.job_service import run_durably

logger = logging.getLogger(__name__)

_PUBLISH_LOCK_NAMESPACE = 0x4C50  # "LP" - keyed by source article; see namespace registry


def publish_approved(site_id: int, job_run_id: int | None = None) -> dict:
    return run_durably(job_run_id, _publish_approved, site_id)


def _publish_approved(site_id: int) -> dict:
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
        db.commit()  # end the read transaction — each claim below gets its own

        applied = failed = skipped = 0
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
                    skipped += 1
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
                db.commit()  # releases the advisory and row locks
                applied += 1
            except Exception:
                db.rollback()  # claim undone — suggestion is 'approved' again for retry
                logger.exception("apply_link failed for suggestion %s", suggestion_id)
                failed += 1
        return {"applied": applied, "failed": failed, "skipped": skipped}
    finally:
        db.close()
