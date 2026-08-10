from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import PipelineBatch, PipelineSiteRun


ACTIVE_SITE_STATUSES = {
    "queued",
    "ingestion_running",
    "analysis_queued",
    "analysis_running",
}


def refresh_pipeline_batch_status(db: Session, batch_id: int) -> PipelineBatch:
    """Recompute aggregate status while locking the batch against concurrent workers."""
    batch = db.scalar(select(PipelineBatch).where(PipelineBatch.id == batch_id).with_for_update())
    if batch is None:
        raise ValueError(f"pipeline batch {batch_id} not found")
    statuses = list(
        db.scalars(select(PipelineSiteRun.status).where(PipelineSiteRun.batch_id == batch_id))
    )
    now = datetime.now(UTC)
    if any(status in ACTIVE_SITE_STATUSES for status in statuses):
        batch.status = "running"
        batch.started_at = batch.started_at or now
        batch.finished_at = None
    elif statuses and all(status == "succeeded" for status in statuses):
        batch.status = "succeeded"
        batch.started_at = batch.started_at or now
        batch.finished_at = now
    elif statuses and all(status == "failed" for status in statuses):
        batch.status = "failed"
        batch.started_at = batch.started_at or now
        batch.finished_at = now
    else:
        batch.status = "partial_failed"
        batch.started_at = batch.started_at or now
        batch.finished_at = now
    db.flush()
    return batch


def update_pipeline_site(
    db: Session,
    pipeline_site_run_id: int,
    *,
    status: str,
    stage: str,
    error: str | None = None,
) -> PipelineSiteRun:
    item = db.scalar(
        select(PipelineSiteRun).where(PipelineSiteRun.id == pipeline_site_run_id).with_for_update()
    )
    if item is None:
        raise ValueError(f"pipeline site run {pipeline_site_run_id} not found")
    now = datetime.now(UTC)
    item.status = status
    item.stage = stage
    item.error = error[:2000] if error else None
    if status in ("ingestion_running", "analysis_running"):
        item.started_at = item.started_at or now
        item.finished_at = None
    elif status in ("succeeded", "failed"):
        item.started_at = item.started_at or now
        item.finished_at = now
    db.flush()
    refresh_pipeline_batch_status(db, item.batch_id)
    return item


def pipeline_batch_counts(db: Session, batch_id: int) -> dict[str, int]:
    rows = db.execute(
        select(PipelineSiteRun.status, func.count())
        .where(PipelineSiteRun.batch_id == batch_id)
        .group_by(PipelineSiteRun.status)
    ).all()
    counts = {status: count for status, count in rows}
    total = sum(counts.values())
    return {
        "total": total,
        "active": sum(counts.get(status, 0) for status in ACTIVE_SITE_STATUSES),
        "succeeded": counts.get("succeeded", 0),
        "failed": counts.get("failed", 0),
    }
