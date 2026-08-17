import json
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
from app.schemas.reviewer_labels import LabelReadinessOut, ReviewerLabelDatasetOut
from app.ml.evaluation.reviewer_labels import (
    ReviewerLabelDataset,
    build_reviewer_label_dataset,
    inspect_label_readiness,
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
    if isinstance(value, (dict, list)):
        return csv_escape_formula(json.dumps(value, sort_keys=True, separators=(",", ":")))
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
    "shown_at",
    "last_shown_at",
    "exposure_count",
    "status",
    "created_at",
    "reviewed_at",
    "reviewer_id",
    "rejection_reason",
    "retrieval_version",
    "ranking_version",
    "final_rank",
    "feature_snapshot",
    "placement_generated_at",
    "applied_at",
    "publish_outcome",
    "last_failure_at",
)

REVIEWER_LABEL_EXPORT_HEADER = (
    "review_event_id",
    "suggestion_id",
    "trace_id",
    "site_id",
    "source_article_id",
    "target_article_id",
    "label",
    "reviewed_at",
    "reviewer_id",
    "shown_at",
    "exposure_count",
    "method",
    "score",
    "retrieval_version",
    "ranking_version",
    "final_rank",
    "feature_snapshot",
)


def _validate_cutoff(cutoff_at: datetime) -> None:
    if cutoff_at.tzinfo is None or cutoff_at.utcoffset() is None:
        raise HTTPException(422, "cutoff_at must include a timezone")


def _reviewer_label_dataset(
    db: Session,
    *,
    site_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
    cutoff_at: datetime,
    holdout_site_id: int | None,
) -> ReviewerLabelDataset:
    _validate_filters(db, site_id, date_from, date_to)
    _validate_cutoff(cutoff_at)
    if holdout_site_id is not None and db.get(Site, holdout_site_id) is None:
        raise HTTPException(404, f"site {holdout_site_id} not found")
    try:
        return build_reviewer_label_dataset(
            db,
            cutoff_at=cutoff_at,
            site_ids=(site_id,) if site_id is not None else None,
            date_from=date_from,
            date_to=date_to,
            holdout_site_id=holdout_site_id,
            # This endpoint is an auditable evidence export. It returns the
            # eligible rows and embeds the false readiness state, while the
            # offline freeze script remains fail-closed for training artifacts.
            require_ready=False,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/label-readiness", response_model=LabelReadinessOut)
def get_label_readiness(
    site_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    db: Session = Depends(get_db),
) -> LabelReadinessOut:
    """Report whether reviewer evidence is ready for a frozen benchmark."""
    _validate_filters(db, site_id, date_from, date_to)
    return inspect_label_readiness(
        db,
        site_ids=(site_id,) if site_id is not None else None,
        date_from=date_from,
        date_to=date_to,
    ).to_dict()


@router.get("/reviewer-labels.json", response_model=ReviewerLabelDatasetOut)
def export_reviewer_labels_json(
    cutoff_at: datetime,
    site_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    holdout_site_id: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
) -> dict:
    """Export only exposed individual labels plus frozen split metadata.

    The response is an evidence export, not permission to train or promote a
    model. ``readiness.ready`` must be true before a file is frozen for those
    purposes.
    """
    return _reviewer_label_dataset(
        db,
        site_id=site_id,
        date_from=date_from,
        date_to=date_to,
        cutoff_at=cutoff_at,
        holdout_site_id=holdout_site_id,
    ).to_dict()


@router.get("/reviewer-labels.csv")
def export_reviewer_labels_csv(
    cutoff_at: datetime,
    site_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    holdout_site_id: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream the same eligible labels for spreadsheet/offline inspection."""
    dataset = _reviewer_label_dataset(
        db,
        site_id=site_id,
        date_from=date_from,
        date_to=date_to,
        cutoff_at=cutoff_at,
        holdout_site_id=holdout_site_id,
    )
    return csv_response(
        REVIEWER_LABEL_EXPORT_HEADER,
        (
            [
                _csv_safe(value)
                for value in (
                    row.review_event_id,
                    row.suggestion_id,
                    row.trace_id,
                    row.site_id,
                    row.source_article_id,
                    row.target_article_id,
                    row.label,
                    row.reviewed_at,
                    row.reviewer_id,
                    row.shown_at,
                    row.exposure_count,
                    row.method,
                    row.score,
                    row.retrieval_version,
                    row.ranking_version,
                    row.final_rank,
                    row.feature_snapshot,
                )
            ]
            for row in dataset.labels
        ),
        filename="linkmesh-reviewer-labels.csv",
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
