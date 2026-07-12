from datetime import datetime, timezone

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
    suggestion.reviewed_at = datetime.now(timezone.utc)


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
def trigger_analysis(site_id: int, db: Session = Depends(get_db)) -> JobAccepted:
    if db.get(Site, site_id) is None:
        raise HTTPException(404, f"site {site_id} not found")
    job = default_queue.enqueue(analyze_site, site_id, job_timeout=7200)
    return JobAccepted(job_id=job.id)


@router.get("/suggestions/{site_id}", response_model=list[SuggestionOut])
def list_suggestions(
    site_id: int,
    status: str | None = None,
    method: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[Suggestion]:
    query = (
        select(Suggestion)
        .where(Suggestion.site_id == site_id)
        .options(joinedload(Suggestion.source_article), joinedload(Suggestion.target_article))
        .order_by(Suggestion.score.desc())
    )
    if status:
        query = query.where(Suggestion.status == status)
    if method:
        query = query.where(Suggestion.method == method)
    return db.scalars(query.limit(limit).offset(offset)).all()


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
