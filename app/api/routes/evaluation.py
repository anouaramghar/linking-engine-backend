from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.csv_stream import csv_escape_formula, csv_response
from app.api.deps import get_db, require_admin
from app.models import Site
from app.schemas.evaluation import (
    EvaluationMetric,
    EvaluationMetricsOut,
    EvaluationSuggestionPage,
)
from app.services.evaluation_service import (
    evaluation_export_rows,
    evaluation_metrics,
    evaluation_suggestions,
)

# Admin-only, for the whole surface. Every route here answers fleet-wide
# questions — `site_id` is a filter, not a scope, and omitting it reports on
# every site at once. Scoped keys exist to bound blast radius, so a tenant key
# that could read another site's titles and export them as CSV would defeat the
# only containment the product model keeps. Narrowing this to "your sites only"
# was rejected: the aggregate itself is the answer, and an aggregate filtered
# per caller is a different, less useful metric that still leaks fleet totals
# through comparison.
router = APIRouter(
    prefix="/evaluation",
    tags=["evaluation"],
    dependencies=[Depends(require_admin)],
)


def _validate_filters(
    db: Session,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> None:
    if site_id is not None and db.get(Site, site_id) is None:
        raise HTTPException(404, f"site {site_id} not found")
    for label, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(422, f"{label} must include a timezone")
    if date_from is not None and date_to is not None and date_from >= date_to:
        raise HTTPException(422, "date_from must be before date_to")


@router.get("/metrics", response_model=EvaluationMetricsOut)
def get_evaluation_metrics(
    site_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    db: Session = Depends(get_db),
) -> EvaluationMetricsOut:
    _validate_filters(db, site_id, date_from, date_to)
    return evaluation_metrics(db, site_id, date_from, date_to)


@router.get("/suggestions", response_model=EvaluationSuggestionPage)
def get_evaluation_suggestions(
    metric: EvaluationMetric,
    site_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> EvaluationSuggestionPage:
    _validate_filters(db, site_id, date_from, date_to)
    return evaluation_suggestions(
        db,
        metric,
        site_id,
        date_from,
        date_to,
        limit,
        offset,
    )


def _csv_safe(value: object) -> str:
    if value is None:
        return ""
    rendered = value.isoformat() if isinstance(value, datetime) else str(value)
    return csv_escape_formula(rendered)


EXPORT_HEADER = (
    "suggestion_id",
    "trace_id",
    "site",
    "source_title",
    "target_title",
    "method",
    "semantic_score",
    "status",
    "created_at",
    "reviewed_at",
    "placement_generated_at",
    "applied_at",
    "publish_outcome",
    "last_failure_at",
)


@router.get("/export.csv")
def export_evaluation_csv(
    site_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream the cohort as CSV.

    Unfiltered, this is every suggestion in the database. It streams rather than
    being assembled first, so the response is bounded by one batch of rows
    instead of by the size of the table.
    """
    _validate_filters(db, site_id, date_from, date_to)
    return csv_response(
        EXPORT_HEADER,
        (
            [_csv_safe(value) for value in row]
            for row in evaluation_export_rows(db, site_id, date_from, date_to)
        ),
        filename="linkmesh-evaluation.csv",
    )
