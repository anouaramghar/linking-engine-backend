from collections.abc import Sequence
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_db
from app.api.pagination import MAX_PAGE_SIZE
from app.models import Site, Suggestion
from app.schemas.job import JobAccepted
from app.schemas.suggestion import (
    BulkReview,
    BulkReviewFilter,
    BulkReviewFilterResult,
    SuggestionCounts,
    SuggestionOut,
    SuggestionPage,
    SuggestionReview,
)
from app.services.job_service import DuplicateJobError, enqueue_job
from app.tasks.analysis import analyze_site

router = APIRouter(tags=["suggestions"])


UNREVIEWABLE = ("applying", "applied", "expired")


def _review_matching(db: Session, conditions: Sequence, status: str) -> set[int]:
    """Move every reviewable row matching ``conditions`` to ``status``; returns the
    ids that actually moved.

    Guarded transition (Phase 0, finding 5): a suggestion being published holds a
    row lock on its claim, so this update blocks until the publish commits and
    then matches zero rows — a reject can never land on top of a publish.

    One statement for the whole batch, so the round trips do not scale with the
    size of the review. `synchronize_session=False` keeps it that way: the ORM
    would otherwise re-fetch the touched rows to update its identity map, and
    nothing here reads them back through it.
    """
    # Undoing a decision returns the suggestion to the unreviewed state, so the
    # review timestamp is cleared rather than advanced.
    reviewed_at = None if status == "pending" else datetime.now(timezone.utc)
    return set(
        db.scalars(
            update(Suggestion)
            .where(*conditions, Suggestion.status.notin_(UNREVIEWABLE))
            .values(status=status, reviewed_at=reviewed_at)
            .returning(Suggestion.id)
            .execution_options(synchronize_session=False)
        )
    )


def _review_ids(db: Session, suggestion_ids: Sequence[int], status: str) -> set[int]:
    return _review_matching(db, [Suggestion.id.in_(suggestion_ids)], status)


def _queue_conditions(
    *,
    site_id: int | None = None,
    status: str | None = None,
    method: str | None = None,
    min_score: float | None = None,
    max_score: float | None = None,
) -> list:
    """Only the bounds the caller gave. Whether an absent status means "every
    status" or "everything but expired" differs per route, so it is decided there.

    The score bounds are asymmetric on purpose: the dashboard rule approves at or
    above a threshold and rejects below the same number, so a shared threshold
    must not put one row in both halves.
    """
    conditions = []
    if site_id is not None:
        conditions.append(Suggestion.site_id == site_id)
    if status is not None:
        conditions.append(Suggestion.status == status)
    if method is not None:
        conditions.append(Suggestion.method == method)
    if min_score is not None:
        conditions.append(Suggestion.score >= min_score)
    if max_score is not None:
        conditions.append(Suggestion.score < max_score)
    return conditions


# declared before /suggestions/{site_id} so "bulk-review" isn't parsed as a site id
@router.post("/suggestions/bulk-review")
def bulk_review(payload: BulkReview, db: Session = Depends(get_db)) -> dict:
    # Partial success, not all-or-nothing: a batch is a set of independent
    # decisions, and one row the publication worker has already claimed must not
    # discard the rest. Undo hits this routinely — the worker can pick up an
    # approval while the undo affordance is still on screen, and failing the
    # whole batch would leave the editor with no way to walk back the others.
    #
    # Read the ids that exist first, so "skipped" means the worker got there
    # first rather than lumping in ids that were never rows at all.
    existing = set(
        db.scalars(select(Suggestion.id).where(Suggestion.id.in_(payload.suggestion_ids)))
    )
    reviewed = _review_ids(db, payload.suggestion_ids, payload.status)
    db.commit()
    return {
        "reviewed": sorted(reviewed),
        "skipped": sorted(existing - reviewed),
        "status": payload.status,
    }


# also declared before /suggestions/{site_id}, for the same reason as bulk-review
@router.post("/suggestions/bulk-review-by-filter", response_model=BulkReviewFilterResult)
def bulk_review_by_filter(
    payload: BulkReviewFilter, db: Session = Depends(get_db)
) -> BulkReviewFilterResult:
    """Apply a bulk rule to every row it matches, however many that is.

    The id-list endpoint above stays the path for an explicit selection. This one
    exists because the queue is read a page at a time: the client knows the rule
    but can no longer name its targets, and a fleet-wide rule matches far more
    rows than PostgreSQL will accept as bound parameters.
    """
    conditions = _queue_conditions(
        site_id=payload.site_id,
        status=payload.match_status,
        method=payload.method,
        min_score=payload.min_score,
        max_score=payload.max_score,
    )
    # Counted before the update, so a row the publication worker claims in between
    # is reported as skipped rather than silently dropped from both numbers.
    matched = db.scalar(select(func.count()).select_from(Suggestion).where(*conditions)) or 0
    reviewed = _review_matching(db, conditions, payload.status)
    db.commit()
    return BulkReviewFilterResult(
        reviewed=len(reviewed),
        skipped=max(matched - len(reviewed), 0),
        status=payload.status,
    )


@router.get("/suggestions/counts", response_model=SuggestionCounts)
def count_suggestions(
    site_id: int | None = None,
    method: str | None = None,
    min_score: float | None = Query(None, ge=0, le=1),
    max_score: float | None = Query(None, ge=0, le=1),
    db: Session = Depends(get_db),
) -> SuggestionCounts:
    conditions = _queue_conditions(
        site_id=site_id, method=method, min_score=min_score, max_score=max_score
    )
    rows = db.execute(
        select(Suggestion.status, func.count())
        .where(*conditions)
        .group_by(Suggestion.status)
    ).all()
    counts = {status: count for status, count in rows}
    return SuggestionCounts(
        **counts,
        total=sum(count for status, count in counts.items() if status != "expired"),
    )


@router.get("/suggestions", response_model=SuggestionPage)
def list_suggestion_page(
    site_id: int | None = None,
    status: str | None = None,
    method: str | None = None,
    min_score: float | None = Query(None, ge=0, le=1),
    max_score: float | None = Query(None, ge=0, le=1),
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> SuggestionPage:
    """The queue across every site, or one of them, a page at a time."""
    conditions = _queue_conditions(
        site_id=site_id,
        status=status,
        method=method,
        min_score=min_score,
        max_score=max_score,
    )
    if status is None:
        conditions.append(Suggestion.status != "expired")
    items = db.scalars(
        select(Suggestion)
        .where(*conditions)
        .options(joinedload(Suggestion.source_article), joinedload(Suggestion.target_article))
        # Score alone is not unique, and equal scores are common. Without the id
        # tiebreaker PostgreSQL may order tied rows differently per statement, so
        # paging through would repeat some rows and never show others.
        .order_by(Suggestion.score.desc(), Suggestion.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    total = db.scalar(select(func.count()).select_from(Suggestion).where(*conditions)) or 0
    return SuggestionPage(items=items, total=total, limit=limit, offset=offset)


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
    if not _review_ids(db, [suggestion_id], payload.status):
        raise HTTPException(409, f"suggestion {suggestion_id} is no longer reviewable")
    db.commit()
    db.refresh(suggestion)
    return suggestion
