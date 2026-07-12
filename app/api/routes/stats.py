"""Validation dashboard stats (v5)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Article, InternalLink, Site, Suggestion

router = APIRouter(prefix="/stats", tags=["stats"])


def _site_stats(db: Session, site_id: int) -> dict:
    articles = db.scalar(select(func.count()).where(Article.site_id == site_id))
    links = db.scalar(
        select(func.count())
        .select_from(InternalLink)
        .join(Article, Article.id == InternalLink.source_article_id)
        .where(Article.site_id == site_id)
    )
    orphans = db.scalar(
        select(func.count()).where(
            Article.site_id == site_id,
            ~select(InternalLink.id)
            .where(InternalLink.target_article_id == Article.id)
            .exists(),
        )
    )
    by_status = dict(
        db.execute(
            select(Suggestion.status, func.count())
            .where(Suggestion.site_id == site_id)
            .group_by(Suggestion.status)
        ).all()
    )
    by_method = dict(
        db.execute(
            select(Suggestion.method, func.count())
            .where(Suggestion.site_id == site_id)
            .group_by(Suggestion.method)
        ).all()
    )
    reviewed = by_status.get("approved", 0) + by_status.get("rejected", 0) + by_status.get("applied", 0)
    accepted = by_status.get("approved", 0) + by_status.get("applied", 0)
    return {
        "site_id": site_id,
        "articles": articles,
        "internal_links": links,
        "orphan_articles": orphans,
        "suggestions_by_status": by_status,
        "suggestions_by_method": by_method,
        # the v5 quality signal: how often editors accept what the engine proposes
        "approval_rate": round(accepted / reviewed, 3) if reviewed else None,
    }


@router.get("/{site_id}")
def site_stats(site_id: int, db: Session = Depends(get_db)) -> dict:
    if db.get(Site, site_id) is None:
        raise HTTPException(404, f"site {site_id} not found")
    return _site_stats(db, site_id)


@router.get("/{site_id}/evaluation")
def site_evaluation(site_id: int) -> dict:
    """Last run of scripts/run_evaluation.py for this site (baseline vs GNN, +10pts gate)."""
    import json
    from pathlib import Path

    from app.config import settings

    path = Path(settings.model_dir) / f"eval_site_{site_id}.json"
    if not path.exists():
        raise HTTPException(404, f"no evaluation run for site {site_id}")
    return json.loads(path.read_text())


@router.get("")
def fleet_stats(db: Session = Depends(get_db)) -> list[dict]:
    return [_site_stats(db, sid) for sid in db.scalars(select(Site.id).order_by(Site.id))]
