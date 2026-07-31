from collections.abc import Sequence
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, literal, select, tuple_, update
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_db
from app.api.pagination import MAX_PAGE_SIZE
from app.models import Site, Suggestion
from app.schemas.job import JobAccepted
from app.schemas.suggestion import (
    MAX_BULK_REVIEW,
    BulkReview,
    BulkReviewFilter,
    BulkReviewFilterResult,
    SuggestionCounts,
    SuggestionCursor,
    SuggestionOut,
    SuggestionPage,
    SuggestionReview,
)
from app.services.job_service import DuplicateJobError, enqueue_job
from app.tasks.analysis import analyze_site, compare_site

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


def _review_matching_counts(
    db: Session, conditions: Sequence, status: str
) -> tuple[int, int, list[int] | None]:
    """Summarize one stable candidate cohort and update its reviewable rows.

    The explicit-id path always returns identities because the caller supplied
    them. A filtered review does not have that bound, so this path returns at most
    MAX_BULK_REVIEW ids and otherwise keeps only counts. MATERIALIZED gives
    `matched` a precise meaning: rows in the statement's initial snapshot, not a
    count from an earlier snapshot.
    """
    if not conditions:
        raise ValueError("filtered review requires at least one match condition")

    reviewed_at = None if status == "pending" else datetime.now(timezone.utc)
    candidates = (
        select(Suggestion.id)
        .where(*conditions)
        .cte("review_candidates")
        .prefix_with("MATERIALIZED")
    )
    reviewed_rows = (
        update(Suggestion)
        .where(
            Suggestion.id == candidates.c.id,
            *conditions,
        )
        .values(status=status, reviewed_at=reviewed_at)
        .returning(Suggestion.id)
        .execution_options(synchronize_session=False)
        .cte("reviewed_rows")
    )
    bounded_reviewed_ids = select(reviewed_rows.c.id).limit(MAX_BULK_REVIEW).subquery()
    result = db.execute(
        select(
            select(func.count()).select_from(candidates).scalar_subquery().label("matched"),
            select(func.count()).select_from(reviewed_rows).scalar_subquery().label("reviewed"),
            select(func.array_agg(bounded_reviewed_ids.c.id))
            .select_from(bounded_reviewed_ids)
            .scalar_subquery()
            .label("reviewed_ids"),
        )
    ).one()
    reviewed_ids = sorted(result.reviewed_ids or []) if result.reviewed <= MAX_BULK_REVIEW else None
    return result.matched, result.reviewed, reviewed_ids


def _percent_boundary(percent: int) -> float:
    """Raw-score boundary equivalent to JavaScript's Math.round(score * 100).

    Scores are non-negative, so Math.round(score * 100) >= P is exactly
    score >= (P - 0.5) / 100. Comparing the indexed column with that constant
    preserves index scans and avoids PostgreSQL's different half-rounding rule.
    """

    return (percent - 0.5) / 100


def _queue_conditions(
    *,
    site_id: int | None = None,
    status: str | None = None,
    method: str | None = None,
    min_percent: int | None = None,
    max_percent: int | None = None,
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
    if min_percent is not None:
        conditions.append(Suggestion.score >= _percent_boundary(min_percent))
    if max_percent is not None:
        conditions.append(Suggestion.score < _percent_boundary(max_percent))
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
        min_percent=payload.threshold_percent if payload.status == "approved" else None,
        max_percent=payload.threshold_percent if payload.status == "rejected" else None,
    )
    matched, reviewed, reviewed_ids = _review_matching_counts(db, conditions, payload.status)
    db.commit()
    return BulkReviewFilterResult(
        reviewed=reviewed,
        skipped=matched - reviewed,
        reviewed_ids=reviewed_ids,
        status=payload.status,
    )


@router.get("/suggestions/counts", response_model=SuggestionCounts)
def count_suggestions(
    site_id: int | None = None,
    method: str | None = None,
    min_percent: int | None = Query(None, ge=0, le=100),
    max_percent: int | None = Query(None, ge=0, le=100),
    db: Session = Depends(get_db),
) -> SuggestionCounts:
    conditions = _queue_conditions(
        site_id=site_id, method=method, min_percent=min_percent, max_percent=max_percent
    )
    rows = db.execute(
        select(Suggestion.status, func.count()).where(*conditions).group_by(Suggestion.status)
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
    min_percent: int | None = Query(None, ge=0, le=100),
    max_percent: int | None = Query(None, ge=0, le=100),
    after_score: float | None = Query(None, ge=0, le=1),
    after_id: int | None = Query(None, ge=1),
    include_total: bool = False,
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int | None = Query(None, ge=0, deprecated=True),
    db: Session = Depends(get_db),
) -> SuggestionPage:
    """The queue across every site, or one of them, a cursor page at a time."""
    conditions = _queue_conditions(
        site_id=site_id,
        status=status,
        method=method,
        min_percent=min_percent,
        max_percent=max_percent,
    )
    if status is None:
        # Render the fixed predicate literally so PostgreSQL can prove that the
        # query implies the partial-index predicate even for a generic prepared
        # plan; a bound `$1` could be any status at plan time.
        conditions.append(Suggestion.status != literal("expired", literal_execute=True))
    if offset is not None:
        raise HTTPException(
            422,
            "offset pagination is not supported; continue with after_score and after_id",
        )
    if (after_score is None) != (after_id is None):
        raise HTTPException(422, "after_score and after_id must be provided together")

    page_conditions = list(conditions)
    if after_score is not None and after_id is not None:
        # Both sort keys descend, so the next page is strictly below the last
        # tuple returned. Removing or inserting rows above it cannot shift this
        # boundary as it can with OFFSET.
        page_conditions.append(
            tuple_(Suggestion.score, Suggestion.id) < tuple_(after_score, after_id)
        )
    items = db.scalars(
        select(Suggestion)
        .where(*page_conditions)
        .options(joinedload(Suggestion.source_article), joinedload(Suggestion.target_article))
        .order_by(Suggestion.score.desc(), Suggestion.id.desc())
        # One look-ahead row tells the client whether another request is useful
        # without paying for COUNT(*) on every page.
        .limit(limit + 1)
    ).all()
    has_more = len(items) > limit
    items = items[:limit]
    next_cursor = None
    if has_more and items:
        next_cursor = SuggestionCursor(score=items[-1].score, id=items[-1].id)
    total = None
    if include_total:
        total = db.scalar(select(func.count()).select_from(Suggestion).where(*conditions)) or 0
    return SuggestionPage(
        items=items,
        total=total,
        limit=limit,
        next_cursor=next_cursor,
    )


@router.post("/suggestions/{site_id}", status_code=202, response_model=JobAccepted)
def trigger_analysis(site_id: int, db: Session = Depends(get_db)) -> JobAccepted:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, f"site {site_id} not found")
    if site.platform == "pool":
        raise HTTPException(409, "content-pool sources cannot generate suggestions")
    try:
        run = enqueue_job(db, site_id, "analysis", analyze_site, job_timeout=7200)
    except DuplicateJobError as e:
        raise HTTPException(409, str(e)) from e
    return JobAccepted(job_id=run.queue_job_id, job_run_id=run.id)


@router.post("/suggestions/{site_id}/compare", status_code=202, response_model=JobAccepted)
def trigger_analysis_comparison(site_id: int, db: Session = Depends(get_db)) -> JobAccepted:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, f"site {site_id} not found")
    if site.platform == "pool":
        raise HTTPException(409, "content-pool sources cannot generate suggestions")
    try:
        run = enqueue_job(db, site_id, "analysis", compare_site, job_timeout=7200)
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
