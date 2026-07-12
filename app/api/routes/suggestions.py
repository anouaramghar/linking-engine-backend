from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_db
from app.models import Site, Suggestion
from app.schemas.job import JobAccepted
from app.schemas.suggestion import BulkReview, SuggestionOut, SuggestionReview
from app.tasks.analysis import analyze_site
from app.tasks.queues import default_queue

router = APIRouter(tags=["suggestions"])


def _review(db: Session, suggestion: Suggestion, status: str) -> None:
    if suggestion.status == "applied":
        raise HTTPException(409, f"suggestion {suggestion.id} is already applied")
    suggestion.status = status
    suggestion.reviewed_at = None if status == "pending" else datetime.now(timezone.utc)


# declared before /suggestions/{site_id} so "bulk-review" isn't parsed as a site id
@router.post("/suggestions/bulk-review")
def bulk_review(payload: BulkReview, db: Session = Depends(get_db)) -> dict:
    suggestions = db.scalars(
        select(Suggestion).where(Suggestion.id.in_(payload.suggestion_ids))
    ).all()
    for suggestion in suggestions:
        _review(db, suggestion, payload.status)
    db.commit()
    return {"reviewed": len(suggestions), "status": payload.status}


@router.post("/suggestions/{site_id}", status_code=202, response_model=JobAccepted)
def trigger_analysis(
    site_id: int,
    method: Literal["baseline_cosine", "gnn_graphsage"] = "baseline_cosine",
    db: Session = Depends(get_db),
) -> JobAccepted:
    if db.get(Site, site_id) is None:
        raise HTTPException(404, f"site {site_id} not found")
    job = default_queue.enqueue(analyze_site, site_id, method, job_timeout=7200)
    return JobAccepted(job_id=job.id)


@router.post("/suggestions/{site_id}/external", status_code=202, response_model=JobAccepted)
def trigger_external(site_id: int, db: Session = Depends(get_db)) -> JobAccepted:
    if db.get(Site, site_id) is None:
        raise HTTPException(404, f"site {site_id} not found")
    from app.tasks.external import external_site

    job = default_queue.enqueue(external_site, site_id, job_timeout=7200)
    return JobAccepted(job_id=job.id)


@router.post("/suggestions/{site_id}/anchors", status_code=202, response_model=JobAccepted)
def trigger_anchor_generation(site_id: int, db: Session = Depends(get_db)) -> JobAccepted:
    if db.get(Site, site_id) is None:
        raise HTTPException(404, f"site {site_id} not found")
    from app.tasks.anchors import anchor_site

    job = default_queue.enqueue(anchor_site, site_id, job_timeout=7200)
    return JobAccepted(job_id=job.id)


@router.get("/suggestions", response_model=list[SuggestionOut])
def list_all_suggestions(  # cross-site view for the validation dashboard (v5)
    site_id: int | None = None,
    status: str | None = None,
    method: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[Suggestion]:
    query = (
        select(Suggestion)
        .options(joinedload(Suggestion.source_article), joinedload(Suggestion.target_article))
        .order_by(Suggestion.score.desc())
    )
    if site_id:
        query = query.where(Suggestion.site_id == site_id)
    if status:
        query = query.where(Suggestion.status == status)
    if method:
        query = query.where(Suggestion.method == method)
    return db.scalars(query.limit(limit).offset(offset)).all()


@router.get("/suggestions/{site_id}", response_model=list[SuggestionOut])
def list_suggestions(
    site_id: int,
    status: str | None = None,
    method: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[Suggestion]:
    return list_all_suggestions(site_id, status, method, limit, offset, db)


@router.put("/suggestions/{suggestion_id}", response_model=SuggestionOut)
def review_suggestion(
    suggestion_id: int, payload: SuggestionReview, db: Session = Depends(get_db)
) -> Suggestion:
    suggestion = db.get(
        Suggestion,
        suggestion_id,
        options=[joinedload(Suggestion.source_article), joinedload(Suggestion.target_article)],
    )
    if suggestion is None:
        raise HTTPException(404, f"suggestion {suggestion_id} not found")
    _review(db, suggestion, payload.status)
    db.commit()
    db.refresh(suggestion)
    return suggestion
