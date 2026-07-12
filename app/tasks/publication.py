"""Publication worker (sequence 4.3): apply approved links via each site's connector.

One suggestion's failure never interrupts the batch; failures stay 'approved' and are
retried on the next run (A8). Publication never writes internal_links — the applied link
is detected at the next crawl (A9, ingestion is the single source of truth).
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.connectors.registry import get_connector
from app.db import SessionLocal
from app.models import Site, Suggestion

logger = logging.getLogger(__name__)


def publish_approved(site_id: int) -> dict:
    db = SessionLocal()
    try:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        connector = get_connector(site)
        approved = db.scalars(
            select(Suggestion)
            .where(Suggestion.site_id == site_id, Suggestion.status == "approved")
            .options(
                joinedload(Suggestion.source_article), joinedload(Suggestion.target_article)
            )
        ).all()

        applied = failed = 0
        for suggestion in approved:
            try:
                connector.apply_link(suggestion)
                # 'applied' is set exclusively here — lifecycle guarantee
                suggestion.status = "applied"
                suggestion.applied_at = datetime.now(timezone.utc)
                db.commit()
                applied += 1
            except Exception:
                db.rollback()
                logger.exception("apply_link failed for suggestion %s", suggestion.id)
                failed += 1
        return {"applied": applied, "failed": failed}
    finally:
        db.close()
