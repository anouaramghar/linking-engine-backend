from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_db
from app.api.pagination import MAX_PAGE_SIZE
from app.models import Site, Suggestion
from app.schemas.job import JobAccepted
from app.schemas.suggestion import BulkReview, SuggestionOut, SuggestionReview
from app.services.job_service import DuplicateJobError, enqueue_job
from app.tasks.analysis import analyze_site

router = APIRouter(tags=["suggestions"])


def _review(db: Session, suggestion: Suggestion, status: str) -> None:
    # Undoing a decision returns the suggestion to the unreviewed state, so the
    # review timestamp is cleared rather than advanced.
    reviewed_at = None if status == "pending" else datetime.now(timezone.utc)
    # Guarded transition (Phase 0, finding 5): a suggestion being published holds a
    # row lock on its claim, so this update blocks until the publish commits and
    # then matches zero rows — a reject can never land on top of a publish.
    updated = db.execute(
        update(Suggestion)
        .where(
            Suggestion.id == suggestion.id,
            Suggestion.status.notin_(["applying", "applied", "expired"]),
        )
        .values(status=status, reviewed_at=reviewed_at)
    ).rowcount
    if updated == 0:
        raise HTTPException(409, f"suggestion {suggestion.id} is no longer reviewable")
    db.expire(suggestion)


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
    try:
        run = enqueue_job(db, site_id, "analysis", analyze_site, job_timeout=7200)
    except DuplicateJobError as e:
        raise HTTPException(409, str(e)) from e
    return JobAccepted(job_id=run.queue_job_id, job_run_id=run.id)


@router.get("/suggestions/{site_id}", response_model=list[SuggestionOut])
def list_suggestions(
    site_id: int,
    status: str | None = None,
    method: str | None = None,
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
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
    else:
        query = query.where(Suggestion.status != "expired")
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
