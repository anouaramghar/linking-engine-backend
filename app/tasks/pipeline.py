from datetime import UTC, datetime

from rq import get_current_job

from app.db import SessionLocal
from app.models import PipelineSiteRun
from app.services.ingestion_service import run_ingestion
from app.services.job_service import DuplicateJobError, enqueue_job, run_durably
from app.services.pipeline_service import update_pipeline_site
from app.services.suggestion_service import generate_suggestions


def _is_final_attempt() -> bool:
    job = get_current_job()
    retries_left = getattr(job, "retries_left", None)
    return job is None or retries_left is None or retries_left <= 0


def _mark_stage_failure(pipeline_site_run_id: int, stage: str, error: Exception) -> None:
    if not _is_final_attempt():
        return
    with SessionLocal() as db:
        update_pipeline_site(
            db,
            pipeline_site_run_id,
            status="failed",
            stage=stage,
            error=str(error),
        )
        db.commit()


def _require_pipeline_site(pipeline_site_run_id: int, site_id: int) -> None:
    with SessionLocal() as db:
        item = db.get(PipelineSiteRun, pipeline_site_run_id)
        if item is None:
            raise ValueError(f"pipeline site run {pipeline_site_run_id} not found")
        if item.site_id != site_id:
            raise ValueError(
                f"pipeline site run {pipeline_site_run_id} belongs to site "
                f"{item.site_id}, not site {site_id}"
            )


def ingest_pipeline_site(
    site_id: int,
    job_run_id: int | None = None,
    batch_site_run_id: int | None = None,
) -> dict:
    if batch_site_run_id is None:
        raise ValueError("batch_site_run_id is required")
    _require_pipeline_site(batch_site_run_id, site_id)
    with SessionLocal() as db:
        update_pipeline_site(
            db,
            batch_site_run_id,
            status="ingestion_running",
            stage="ingestion",
        )
        db.commit()
    try:
        result = run_durably(job_run_id, run_ingestion, site_id)
    except Exception as error:
        _mark_stage_failure(batch_site_run_id, "ingestion", error)
        raise

    try:
        with SessionLocal() as db:
            analysis_run = enqueue_job(
                db,
                site_id,
                "analysis",
                analyze_pipeline_site,
                job_timeout=7200,
                task_kwargs={"batch_site_run_id": batch_site_run_id},
            )
            item = db.get(PipelineSiteRun, batch_site_run_id, with_for_update=True)
            if item is None:
                raise ValueError(f"pipeline site run {batch_site_run_id} not found")
            item.analysis_job_run_id = analysis_run.id
            # A fast analysis worker may already have advanced this row.
            if item.status == "ingestion_running":
                item.status = "analysis_queued"
                item.stage = "analysis"
                item.error = None
            db.commit()
    except DuplicateJobError as error:
        with SessionLocal() as db:
            update_pipeline_site(
                db,
                batch_site_run_id,
                status="failed",
                stage="analysis",
                error=str(error),
            )
            db.commit()
        return {**result, "analysis_enqueue_error": str(error)}
    except Exception as error:
        _mark_stage_failure(batch_site_run_id, "analysis", error)
        raise
    return {**result, "analysis_job_run_id": analysis_run.id}


def analyze_pipeline_site(
    site_id: int,
    job_run_id: int | None = None,
    batch_site_run_id: int | None = None,
) -> dict:
    if batch_site_run_id is None:
        raise ValueError("batch_site_run_id is required")
    _require_pipeline_site(batch_site_run_id, site_id)
    with SessionLocal() as db:
        update_pipeline_site(
            db,
            batch_site_run_id,
            status="analysis_running",
            stage="analysis",
        )
        db.commit()
    try:
        result = run_durably(job_run_id, generate_suggestions, site_id)
    except Exception as error:
        _mark_stage_failure(batch_site_run_id, "analysis", error)
        raise
    with SessionLocal() as db:
        item = update_pipeline_site(
            db,
            batch_site_run_id,
            status="succeeded",
            stage="completed",
        )
        item.finished_at = datetime.now(UTC)
        db.commit()
    return result
