import csv
import io
import json
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import exists, func, insert, literal, or_, select, text, tuple_, update
from sqlalchemy.orm import Session, aliased, joinedload

from app.api.deps import get_audit_actor, get_db
from app.api.pagination import MAX_PAGE_SIZE
from app.ml.llm import openrouter
from app.ml.llm.openrouter import OpenRouterError, OpenRouterNotConfigured
from app.models import (
    Article,
    BulkReviewOperation,
    BulkReviewOperationItem,
    Site,
    Suggestion,
    SuggestionEvent,
)
from app.schemas.job import JobAccepted
from app.schemas.site import ArticleBrief
from app.schemas.suggestion import (
    MAX_BULK_REVIEW,
    MAX_SEARCH_TERM,
    BulkReview,
    BulkReviewFilter,
    BulkReviewFilterResult,
    BulkReviewUndoResult,
    PlacementOut,
    SuggestionCounts,
    SuggestionCursor,
    SuggestionEventOut,
    TraceEventOut,
    TraceEventPage,
    SuggestionOut,
    SuggestionPage,
    SuggestionReview,
    TargetOrigin,
)
from app.services import placement_service
from app.services.job_service import DuplicateJobError, enqueue_job
from app.tasks.analysis import analyze_site, compare_site

logger = logging.getLogger(__name__)

router = APIRouter(tags=["suggestions"])


UNREVIEWABLE = ("applying", "applied", "expired")

#: Statuses whose anchor phrase is spoken for. The row is on its way to becoming
#: a link on the source article, or already is one. A rejected, expired or
#: quarantined row will never publish, so the words it picked are free again.
CLAIMS_AN_ANCHOR = ("pending", "approved", "applying", "applied")


def _suggestion_outputs(db: Session, suggestions: Sequence[Suggestion]) -> list[SuggestionOut]:
    """Serialize suggestions with each target's ownership made explicit.

    A normal customer suggestion targets an article from the same site. Pool
    articles are deliberately allowed as targets, so the target article's
    owning site is the source of truth for the dashboard label.
    """

    target_site_ids = {suggestion.target_article.site_id for suggestion in suggestions}
    target_sites = {
        site_id: (name, platform)
        for site_id, name, platform in db.execute(
            select(Site.id, Site.name, Site.platform).where(Site.id.in_(target_site_ids))
        )
    }
    outputs = []
    for suggestion in suggestions:
        target_site_name, target_platform = target_sites[suggestion.target_article.site_id]
        outputs.append(
            SuggestionOut(
                id=suggestion.id,
                trace_id=suggestion.trace_id,
                site_id=suggestion.site_id,
                source_article=ArticleBrief.model_validate(suggestion.source_article),
                target_article=ArticleBrief.model_validate(suggestion.target_article),
                target_origin=(
                    "content_pool" if target_platform == "pool" else "internal"
                ),
                target_site_name=target_site_name,
                method=suggestion.method,
                score=suggestion.score,
                score_components=suggestion.score_components,
                status=suggestion.status,
                anchor_text=suggestion.anchor_text,
                publish_outcome=suggestion.publish_outcome,
                publish_attempts=suggestion.publish_attempts,
                publish_error=suggestion.publish_error,
                created_at=suggestion.created_at,
            )
        )
    return outputs


def _review_values(status: str) -> dict:
    """What a review writes, whichever path issued it.

    Undoing a decision returns the suggestion to the unreviewed state, so the
    review timestamp is cleared rather than advanced.

    The publication failure history is cleared too. A quarantined row is only
    ever revived by an editor deciding on it again, and leaving the count at the
    limit re-quarantines it on its very next attempt — which makes re-approval
    look like it worked and change nothing.
    """
    return {
        "status": status,
        "reviewed_at": None if status == "pending" else datetime.now(timezone.utc),
        "publish_attempts": 0,
        "publish_error": None,
    }


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
    return set(
        db.scalars(
            update(Suggestion)
            .where(*conditions, Suggestion.status.notin_(UNREVIEWABLE))
            .values(**_review_values(status))
            .returning(Suggestion.id)
            .execution_options(synchronize_session=False)
        )
    )


def _review_ids(db: Session, suggestion_ids: Sequence[int], status: str) -> set[int]:
    return _review_matching(db, [Suggestion.id.in_(suggestion_ids)], status)


def _set_audit_actor(db: Session, actor: str) -> None:
    """Label trigger-written lifecycle events for this transaction only."""
    db.execute(
        text("SELECT set_config('linkmesh.audit_actor', :actor, true)"),
        {"actor": actor[:255]},
    )


def _review_matching_counts(
    db: Session,
    conditions: Sequence,
    status: str,
    operation_id: str | None = None,
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
        .values(**_review_values(status))
        .returning(Suggestion.id)
        .execution_options(synchronize_session=False)
        .cte("reviewed_rows")
    )
    reviewed_source = reviewed_rows
    reviewed_id = reviewed_rows.c.id
    if operation_id is not None:
        saved_rows = (
            insert(BulkReviewOperationItem)
            .from_select(
                ["operation_id", "suggestion_id"],
                select(literal(operation_id), reviewed_rows.c.id),
            )
            .returning(BulkReviewOperationItem.suggestion_id)
            .cte("saved_reviewed_rows")
        )
        reviewed_source = saved_rows
        reviewed_id = saved_rows.c.suggestion_id
    bounded_reviewed_ids = select(reviewed_id.label("id")).limit(MAX_BULK_REVIEW).subquery()
    result = db.execute(
        select(
            select(func.count()).select_from(candidates).scalar_subquery().label("matched"),
            select(func.count()).select_from(reviewed_source).scalar_subquery().label("reviewed"),
            select(func.array_agg(bounded_reviewed_ids.c.id))
            .select_from(bounded_reviewed_ids)
            .scalar_subquery()
            .label("reviewed_ids"),
        )
    ).one()
    reviewed_ids = sorted(result.reviewed_ids or []) if result.reviewed <= MAX_BULK_REVIEW else None
    return result.matched, result.reviewed, reviewed_ids


_LIKE_ESCAPE = "\\"


def _like_pattern(term: str) -> str:
    """Match `term` anywhere in a title, with its LIKE metacharacters defanged.

    A search box is free text. An editor pasting a URL fragment or a title that
    contains `_` must get those characters searched for, not interpreted as
    wildcards — otherwise the narrower the query looks, the wider it matches.
    """
    escaped = (
        term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


def _title_matches(term: str):
    """Rows whose source or target article title contains `term`.

    Correlated EXISTS rather than a join onto `articles`: the same condition list
    is reused by the filtered bulk review, which applies it inside an UPDATE
    where a join is not available. Keeping the two sides as separate subqueries
    also lets PostgreSQL choose the trigram index for each independently.
    """
    pattern = _like_pattern(term)
    return or_(
        exists(
            select(1).where(
                Article.id == Suggestion.source_article_id,
                Article.title.ilike(pattern, escape=_LIKE_ESCAPE),
            )
        ),
        exists(
            select(1).where(
                Article.id == Suggestion.target_article_id,
                Article.title.ilike(pattern, escape=_LIKE_ESCAPE),
            )
        ),
    )


def _target_is_pool():
    """Whether this row's target article belongs to a content-pool site.

    Ownership is read from the target's site, which is exactly how
    `_suggestion_outputs` derives the `target_origin` the dashboard renders. One
    source of truth, so the filter and the badge on the card cannot disagree.
    """
    return exists(
        select(1)
        .select_from(Article)
        .join(Site, Site.id == Article.site_id)
        .where(Article.id == Suggestion.target_article_id, Site.platform == "pool")
    )


def _has_stronger_reverse_pair():
    """Whether the same pair is also suggested the other way round, and better.

    Near-duplicate template pages propose A->B and B->A together, and a
    score-ordered queue puts both at the top: on the dev corpus 256 rows are one
    half of such a pair. Excluding *every* reciprocal row would discard the link
    entirely, so this matches only the weaker direction — the stronger one
    survives and the pair costs one review decision instead of two.

    Ordered by (score, id) to match the queue's own ordering, which makes the
    surviving direction the one the editor would have reached first anyway.
    """
    reverse = aliased(Suggestion)
    return exists(
        select(1).where(
            reverse.source_article_id == Suggestion.target_article_id,
            reverse.target_article_id == Suggestion.source_article_id,
            reverse.status != "expired",
            tuple_(reverse.score, reverse.id) > tuple_(Suggestion.score, Suggestion.id),
        )
    )


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
    q: str | None = None,
    target_origin: str | None = None,
    exclude_reciprocal: bool = False,
) -> list:
    """Only the bounds the caller gave. Whether an absent status means "every
    status" or "everything but expired" differs per route, so it is decided there.

    The score bounds are asymmetric on purpose: the dashboard rule approves at or
    above a threshold and rejects below the same number, so a shared threshold
    must not put one row in both halves.

    Every condition here is expressible inside an UPDATE ... WHERE, so the list,
    the counts, and the filtered bulk review can all be built from one function.
    That is what lets the dashboard promise that a bulk rule acts on exactly the
    rows it is showing.
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
    # A search of only whitespace is the same as no search; it must not collapse
    # to '%%' and quietly match the whole queue.
    if q is not None and q.strip():
        conditions.append(_title_matches(q.strip()))
    if target_origin is not None:
        is_pool = _target_is_pool()
        conditions.append(is_pool if target_origin == "content_pool" else ~is_pool)
    if exclude_reciprocal:
        conditions.append(~_has_stronger_reverse_pair())
    return conditions


def _trace_event_query(
    *,
    trace_id: str | None = None,
    actor: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    site_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    source = aliased(Article)
    target = aliased(Article)
    statement = (
        select(
            SuggestionEvent,
            Suggestion.trace_id.label("trace_id"),
            Suggestion.site_id.label("site_id"),
            Site.name.label("site_name"),
            source.title.label("source_title"),
            target.title.label("target_title"),
            Suggestion.status.label("suggestion_status"),
            Suggestion.publish_error.label("publish_error"),
        )
        .join(Suggestion, Suggestion.id == SuggestionEvent.suggestion_id)
        .join(Site, Site.id == Suggestion.site_id)
        .join(source, source.id == Suggestion.source_article_id)
        .join(target, target.id == Suggestion.target_article_id)
    )
    if trace_id and trace_id.strip():
        statement = statement.where(
            Suggestion.trace_id.ilike(_like_pattern(trace_id.strip()), escape=_LIKE_ESCAPE)
        )
    if actor and actor.strip():
        statement = statement.where(
            SuggestionEvent.actor.ilike(_like_pattern(actor.strip()), escape=_LIKE_ESCAPE)
        )
    if event_type and event_type.strip():
        statement = statement.where(SuggestionEvent.event_type == event_type.strip())
    if status and status.strip():
        statement = statement.where(Suggestion.status == status.strip())
    if site_id is not None:
        statement = statement.where(Suggestion.site_id == site_id)
    if date_from is not None:
        statement = statement.where(SuggestionEvent.created_at >= date_from)
    if date_to is not None:
        statement = statement.where(SuggestionEvent.created_at <= date_to)
    return statement


def _trace_event_out(row) -> TraceEventOut:
    event = row[0]
    return TraceEventOut(
        id=event.id,
        suggestion_id=event.suggestion_id,
        event_type=event.event_type,
        actor=event.actor,
        details=event.details or {},
        created_at=event.created_at,
        trace_id=row.trace_id,
        site_id=row.site_id,
        site_name=row.site_name,
        source_title=row.source_title,
        target_title=row.target_title,
        suggestion_status=row.suggestion_status,
        publish_error=row.publish_error,
    )


@router.get("/suggestion-events", response_model=TraceEventPage)
def list_trace_events(
    trace_id: str | None = Query(None, max_length=MAX_SEARCH_TERM),
    actor: str | None = Query(None, max_length=255),
    event_type: str | None = Query(None, max_length=50),
    status: str | None = Query(None, max_length=30),
    site_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> TraceEventPage:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be before date_to")
    statement = _trace_event_query(
        trace_id=trace_id,
        actor=actor,
        event_type=event_type,
        status=status,
        site_id=site_id,
        date_from=date_from,
        date_to=date_to,
    )
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.execute(
        statement.order_by(SuggestionEvent.created_at.desc(), SuggestionEvent.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return TraceEventPage(
        items=[_trace_event_out(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def _csv_safe(value: object) -> str:
    rendered = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value or "")
    return f"'{rendered}" if rendered.startswith(("=", "+", "-", "@")) else rendered


@router.get("/suggestion-events/export.csv")
def export_trace_events_csv(
    trace_id: str | None = Query(None, max_length=MAX_SEARCH_TERM),
    actor: str | None = Query(None, max_length=255),
    event_type: str | None = Query(None, max_length=50),
    status: str | None = Query(None, max_length=30),
    site_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    db: Session = Depends(get_db),
) -> Response:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be before date_to")
    rows = db.execute(
        _trace_event_query(
            trace_id=trace_id,
            actor=actor,
            event_type=event_type,
            status=status,
            site_id=site_id,
            date_from=date_from,
            date_to=date_to,
        ).order_by(SuggestionEvent.created_at.desc(), SuggestionEvent.id.desc())
    ).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "event_id",
            "trace_id",
            "suggestion_id",
            "site",
            "source_title",
            "target_title",
            "event_type",
            "actor",
            "current_status",
            "created_at",
            "publish_error",
            "details",
        ]
    )
    for row in rows:
        item = _trace_event_out(row)
        writer.writerow(
            [
                _csv_safe(item.id),
                _csv_safe(item.trace_id),
                _csv_safe(item.suggestion_id),
                _csv_safe(item.site_name),
                _csv_safe(item.source_title),
                _csv_safe(item.target_title),
                _csv_safe(item.event_type),
                _csv_safe(item.actor),
                _csv_safe(item.suggestion_status),
                _csv_safe(item.created_at.isoformat()),
                _csv_safe(item.publish_error),
                _csv_safe(item.details),
            ]
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="linkmesh-traceability.csv"'},
    )


# declared before /suggestions/{site_id} so "bulk-review" isn't parsed as a site id
@router.post("/suggestions/bulk-review")
def bulk_review(
    payload: BulkReview,
    db: Session = Depends(get_db),
    actor: str = Depends(get_audit_actor),
) -> dict:
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
    _set_audit_actor(db, actor)
    reviewed = _review_ids(db, payload.suggestion_ids, payload.status)
    db.commit()
    logger.info(
        "suggestion review actor=%s mode=explicit status=%s reviewed=%s skipped=%s",
        actor,
        payload.status,
        len(reviewed),
        len(existing - reviewed),
    )
    return {
        "reviewed": sorted(reviewed),
        "skipped": sorted(existing - reviewed),
        "status": payload.status,
    }


# also declared before /suggestions/{site_id}, for the same reason as bulk-review
@router.post("/suggestions/bulk-review-by-filter", response_model=BulkReviewFilterResult)
def bulk_review_by_filter(
    payload: BulkReviewFilter,
    db: Session = Depends(get_db),
    actor: str = Depends(get_audit_actor),
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
        q=payload.q,
        target_origin=payload.target_origin,
        exclude_reciprocal=payload.exclude_reciprocal,
    )
    operation = BulkReviewOperation(
        id=str(uuid4()),
        actor=actor[:255],
        from_status=payload.match_status,
        to_status=payload.status,
    )
    db.add(operation)
    db.flush()
    _set_audit_actor(db, actor)
    matched, reviewed, reviewed_ids = _review_matching_counts(
        db,
        conditions,
        payload.status,
        operation_id=operation.id,
    )
    undo_operation_id: str | None = operation.id
    if reviewed:
        operation.reviewed_count = reviewed
    else:
        db.delete(operation)
        undo_operation_id = None
    db.commit()
    logger.info(
        "suggestion review actor=%s mode=filter status=%s reviewed=%s skipped=%s",
        actor,
        payload.status,
        reviewed,
        matched - reviewed,
    )
    return BulkReviewFilterResult(
        reviewed=reviewed,
        skipped=matched - reviewed,
        reviewed_ids=reviewed_ids,
        undo_operation_id=undo_operation_id,
        status=payload.status,
    )


@router.post(
    "/suggestions/bulk-review-operations/{operation_id}/undo",
    response_model=BulkReviewUndoResult,
)
def undo_filtered_bulk_review(
    operation_id: str,
    db: Session = Depends(get_db),
    actor: str = Depends(get_audit_actor),
) -> BulkReviewUndoResult:
    """Restore exactly the rows changed by one filtered bulk operation.

    Rows that have since entered publication or received another decision are
    deliberately skipped. Repeating the same request is idempotent and returns
    the counts recorded by the first undo.
    """
    operation = db.scalar(
        select(BulkReviewOperation)
        .where(BulkReviewOperation.id == operation_id)
        .with_for_update()
    )
    if operation is None:
        raise HTTPException(status_code=404, detail="bulk review operation not found")
    if operation.undone_at is not None:
        return BulkReviewUndoResult(
            restored=operation.undone_count or 0,
            skipped=operation.skipped_count or 0,
            status=operation.from_status,
            already_undone=True,
        )

    cohort = select(BulkReviewOperationItem.suggestion_id).where(
        BulkReviewOperationItem.operation_id == operation.id
    )
    _set_audit_actor(db, actor)
    restored = db.execute(
        update(Suggestion)
        .where(
            Suggestion.id.in_(cohort),
            Suggestion.status == operation.to_status,
        )
        .values(**_review_values(operation.from_status))
        .execution_options(synchronize_session=False)
    ).rowcount
    skipped = operation.reviewed_count - restored
    operation.undone_at = datetime.now(timezone.utc)
    operation.undone_count = restored
    operation.skipped_count = skipped
    db.commit()
    logger.info(
        "suggestion review actor=%s mode=filtered-undo operation=%s restored=%s skipped=%s",
        actor,
        operation.id,
        restored,
        skipped,
    )
    return BulkReviewUndoResult(
        restored=restored,
        skipped=skipped,
        status=operation.from_status,
    )


@router.get("/suggestions/counts", response_model=SuggestionCounts)
def count_suggestions(
    site_id: int | None = None,
    method: str | None = None,
    min_percent: int | None = Query(None, ge=0, le=100),
    max_percent: int | None = Query(None, ge=0, le=100),
    q: str | None = Query(None, max_length=MAX_SEARCH_TERM),
    target_origin: TargetOrigin | None = None,
    exclude_reciprocal: bool = False,
    db: Session = Depends(get_db),
) -> SuggestionCounts:
    conditions = _queue_conditions(
        site_id=site_id,
        method=method,
        min_percent=min_percent,
        max_percent=max_percent,
        q=q,
        target_origin=target_origin,
        exclude_reciprocal=exclude_reciprocal,
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
    q: str | None = Query(None, max_length=MAX_SEARCH_TERM),
    target_origin: TargetOrigin | None = None,
    exclude_reciprocal: bool = False,
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
        q=q,
        target_origin=target_origin,
        exclude_reciprocal=exclude_reciprocal,
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
    serialized_items = _suggestion_outputs(db, items)
    next_cursor = None
    if has_more and items:
        next_cursor = SuggestionCursor(score=items[-1].score, id=items[-1].id)
    total = None
    if include_total:
        total = db.scalar(select(func.count()).select_from(Suggestion).where(*conditions)) or 0
    return SuggestionPage(
        items=serialized_items,
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
) -> list[SuggestionOut]:
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
    return _suggestion_outputs(db, db.scalars(query.limit(limit).offset(offset)).all())


@router.get("/suggestions/{suggestion_id}/placement", response_model=PlacementOut)
def get_suggestion_placement(suggestion_id: int, db: Session = Depends(get_db)) -> PlacementOut:
    """Where in the source article this link belongs, generating it on first ask.

    Lazy rather than part of analysis: a run produces far more suggestions than
    an editor opens, and this costs a model call per row. The result is written
    back, so the price is paid once per suggestion no matter how often the
    drawer is reopened — including when the answer was "nothing fits".

    Two editors opening the same row at once will both generate. The second
    write simply replaces the first with an equivalent answer, which is cheaper
    than a lock held across an external request.

    The phrases sibling suggestions on this source article have already claimed
    are passed along, exactly as the publication preflight passes them. Without
    that, two rows on one article routinely pick the same words: publication
    gives the phrase to whichever writes first and sends the other to the
    appended block, so the second editor is shown a placement that will not be
    the one they get.
    """
    suggestion = db.get(
        Suggestion,
        suggestion_id,
        options=[joinedload(Suggestion.source_article), joinedload(Suggestion.target_article)],
    )
    if suggestion is None:
        raise HTTPException(404, f"suggestion {suggestion_id} not found")

    placement = placement_service.stored(suggestion)
    if placement is None:
        if not openrouter.is_configured():
            raise HTTPException(
                503,
                "placement generation is not configured; set OPENROUTER_API_KEY",
            )
        taken_anchors = list(
            db.scalars(
                select(Suggestion.anchor_text).where(
                    Suggestion.source_article_id == suggestion.source_article_id,
                    Suggestion.id != suggestion_id,
                    Suggestion.anchor_text.is_not(None),
                    Suggestion.status.in_(CLAIMS_AN_ANCHOR),
                )
            )
        )
        # Hand the connection back before the model call. Everything needed is
        # already loaded, and an open read transaction held across a
        # multi-second external request sits in front of the publication
        # worker's writes to this same table. Detaching first so the rollback
        # does not expire the instance out from under `generate`.
        db.expunge_all()
        db.rollback()
        try:
            placement = placement_service.generate(suggestion, taken_anchors)
        except OpenRouterNotConfigured as e:  # key removed between the check and here
            raise HTTPException(503, str(e)) from e
        except OpenRouterError as e:
            # Upstream, and temporary. Nothing is written, so the next open
            # retries rather than inheriting a failure as a permanent verdict.
            logger.warning("placement generation failed for suggestion %s: %s", suggestion_id, e)
            raise HTTPException(502, "the placement model is unavailable; try again") from e
        placement_service.store(db, suggestion_id, placement)
        db.commit()

    return PlacementOut(
        suggestion_id=suggestion_id,
        found=placement.found,
        placement_context=placement.placement_context,
        anchor_text=placement.anchor_text,
        llm_model=placement.llm_model,
        generated_at=placement.generated_at,
    )


@router.get(
    "/suggestions/{suggestion_id}/events",
    response_model=list[SuggestionEventOut],
)
def list_suggestion_events(
    suggestion_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[SuggestionEvent]:
    if db.get(Suggestion, suggestion_id) is None:
        raise HTTPException(404, f"suggestion {suggestion_id} not found")
    return list(
        db.scalars(
            select(SuggestionEvent)
            .where(SuggestionEvent.suggestion_id == suggestion_id)
            .order_by(SuggestionEvent.created_at.desc(), SuggestionEvent.id.desc())
            .limit(limit)
        )
    )


@router.put("/suggestions/{suggestion_id}", response_model=SuggestionOut)
def review_suggestion(
    suggestion_id: int,
    payload: SuggestionReview,
    db: Session = Depends(get_db),
    actor: str = Depends(get_audit_actor),
) -> SuggestionOut:
    suggestion = db.get(
        Suggestion,
        suggestion_id,
        options=[joinedload(Suggestion.source_article), joinedload(Suggestion.target_article)],
    )
    if suggestion is None:
        raise HTTPException(404, f"suggestion {suggestion_id} not found")
    _set_audit_actor(db, actor)
    if not _review_ids(db, [suggestion_id], payload.status):
        raise HTTPException(409, f"suggestion {suggestion_id} is no longer reviewable")
    db.commit()
    logger.info(
        "suggestion review trace_id=%s actor=%s mode=single status=%s",
        suggestion.trace_id,
        actor,
        payload.status,
    )
    db.refresh(suggestion)
    return _suggestion_outputs(db, [suggestion])[0]
