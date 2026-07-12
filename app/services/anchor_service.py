"""Anchor generation pipeline (v4): fill anchor_text on suggestions that lack one.
One failed LLM call never kills the batch (same policy as publication, A8);
per-suggestion commits make slow LLM runs crash-resumable."""

import logging

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.ml.anchors import generate_anchor
from app.models import Suggestion

logger = logging.getLogger(__name__)


def generate_anchors(site_id: int) -> dict:
    """RQ task body — pending + approved suggestions without anchor text."""
    db = SessionLocal()
    try:
        suggestions = db.scalars(
            select(Suggestion)
            .where(
                Suggestion.site_id == site_id,
                Suggestion.anchor_text.is_(None),
                Suggestion.status.in_(["pending", "approved"]),
            )
            .options(
                joinedload(Suggestion.source_article), joinedload(Suggestion.target_article)
            )
        ).all()

        generated = failed = 0
        for s in suggestions:
            try:
                anchor, model = generate_anchor(
                    s.source_article.title, s.source_article.content_text, s.link_title
                )
                s.anchor_text = anchor
                s.llm_model = model
                db.commit()
                generated += 1
            except Exception:
                db.rollback()
                logger.exception("anchor generation failed for suggestion %s", s.id)
                failed += 1
        return {"generated": generated, "failed": failed}
    finally:
        db.close()
