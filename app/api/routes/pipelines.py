from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_api_key
from app.models import PipelineBatch, PipelineSiteRun, Site
from app.schemas.pipeline import PipelineBatchCreate, PipelineBatchOut, PipelineSiteRunOut
from app.services.authorization import Principal, authorize_site
from app.services.job_service import DuplicateJobError, enqueue_job
from app.services.pipeline_service import (
    pipeline_batch_counts,
    refresh_pipeline_batch_status,
    update_pipeline_site,
)
from app.tasks.pipeline import analyze_pipeline_site, ingest_pipeline_site


router = APIRouter(prefix="/pipelines", tags=["pipelines"])


def _batch_out(db: Session, batch: PipelineBatch) -> PipelineBatchOut:
    items = list(
        db.scalars(
            select(PipelineSiteRun)
            .where(PipelineSiteRun.batch_id == batch.id)
            .order_by(PipelineSiteRun.id)
        )
    )
    return PipelineBatchOut(
        id=batch.id,
        status=batch.status,
        **pipeline_batch_counts(db, batch.id),
        created_at=batch.created_at,
        started_at=batch.started_at,
        finished_at=batch.finished_at,
        sites=[PipelineSiteRunOut.model_validate(item) for item in items],
    )


def _enqueue_pipeline_stage(
    db: Session,
    item: PipelineSiteRun,
    stage: str,
) -> None:
    if stage == "ingestion":
        kind, task, timeout = "ingestion", ingest_pipeline_site, 3600
    else:
        kind, task, timeout = "analysis", analyze_pipeline_site, 7200
    run = enqueue_job(
        db,
        item.site_id,
        kind,
        task,
        job_timeout=timeout,
        task_kwargs={"batch_site_run_id": item.id},
    )
    db.refresh(item)
    if stage == "ingestion":
        item.ingestion_job_run_id = run.id
    else:
        item.analysis_job_run_id = run.id
        if item.status == "queued":
            item.status = "analysis_queued"
    db.commit()


@router.post("/batches", status_code=202, response_model=PipelineBatchOut)
def create_pipeline_batch(
    payload: PipelineBatchCreate,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> PipelineBatchOut:
    sites = list(db.scalars(select(Site).where(Site.id.in_(payload.site_ids))))
    found = {site.id for site in sites}
    missing = sorted(set(payload.site_ids) - found)
    if missing:
        raise HTTPException(404, f"site(s) not found: {', '.join(map(str, missing))}")
    for site in sites:
        authorize_site(db, principal, site.id)
    pool_sites = sorted(site.id for site in sites if site.platform == "pool")
    if pool_sites:
        raise HTTPException(
            409,
            "content-pool sources cannot generate suggestions: "
            + ", ".join(map(str, pool_sites)),
        )

    batch = PipelineBatch()
    db.add(batch)
    db.flush()
    items = [PipelineSiteRun(batch_id=batch.id, site_id=site_id) for site_id in payload.site_ids]
    db.add_all(items)
    db.commit()
    for item in items:
        try:
            _enqueue_pipeline_stage(db, item, "ingestion")
        except DuplicateJobError as error:
            update_pipeline_site(
                db,
                item.id,
                status="failed",
                stage="ingestion",
                error=str(error),
            )
            db.commit()
        except Exception as error:
            update_pipeline_site(
                db,
                item.id,
                status="failed",
                stage="ingestion",
                error=f"could not enqueue ingestion: {error}",
            )
            db.commit()
    refresh_pipeline_batch_status(db, batch.id)
    db.commit()
    db.refresh(batch)
    return _batch_out(db, batch)


@router.get("/batches/{batch_id}", response_model=PipelineBatchOut)
def get_pipeline_batch(
    batch_id: int,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> PipelineBatchOut:
    batch = db.get(PipelineBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"pipeline batch {batch_id} not found")
    items = list(db.scalars(select(PipelineSiteRun).where(PipelineSiteRun.batch_id == batch.id)))
    if not items and not principal.is_admin:
        # Deleting a site cascades its runs away, which can empty a batch that
        # still exists. Hiding it from tenants is right — there is nothing left
        # to prove ownership with — but an admin should still see the record.
        raise HTTPException(404, f"pipeline batch {batch_id} not found")
    for item in items:
        authorize_site(db, principal, item.site_id)
    return _batch_out(db, batch)


@router.post(
    "/batches/{batch_id}/sites/{site_id}/retry",
    status_code=202,
    response_model=PipelineBatchOut,
)
def retry_pipeline_site(
    batch_id: int,
    site_id: int,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> PipelineBatchOut:
    authorize_site(db, principal, site_id)
    batch = db.get(PipelineBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"pipeline batch {batch_id} not found")
    item = db.scalar(
        select(PipelineSiteRun).where(
            PipelineSiteRun.batch_id == batch_id,
            PipelineSiteRun.site_id == site_id,
        )
    )
    if item is None:
        raise HTTPException(404, f"site {site_id} is not in pipeline batch {batch_id}")
    if item.status != "failed":
        raise HTTPException(409, f"site {site_id} pipeline is {item.status}, not failed")
    retry_stage = item.stage
    item.status = "queued"
    item.error = None
    item.finished_at = None
    item.retry_count += 1
    refresh_pipeline_batch_status(db, batch_id)
    db.commit()
    try:
        _enqueue_pipeline_stage(db, item, retry_stage)
    except DuplicateJobError as error:
        update_pipeline_site(
            db,
            item.id,
            status="failed",
            stage=retry_stage,
            error=str(error),
        )
        db.commit()
        raise HTTPException(409, str(error)) from error
    except Exception as error:
        update_pipeline_site(
            db,
            item.id,
            status="failed",
            stage=retry_stage,
            error=f"could not enqueue retry: {error}",
        )
        db.commit()
        raise HTTPException(503, "could not enqueue pipeline retry") from error
    db.refresh(batch)
    return _batch_out(db, batch)
