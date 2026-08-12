from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_site_access
from app.api.pagination import MAX_PAGE_SIZE
from app.models import IngestionRun, Site
from app.schemas.job import JobAccepted
from app.schemas.site import IngestionRunOut
from app.services.ingestion_service import latest_run
from app.services.job_service import DuplicateJobError, enqueue_job
from app.services.pool_source_policy import PoolSourcePolicyError, require_approved_pool_source
from app.tasks.ingestion import ingest_pool_site, ingest_site

router = APIRouter(prefix="/sites", tags=["ingestion"])


@router.post("/{site_id}/ingest", status_code=202, response_model=JobAccepted)
def trigger_ingestion(
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> JobAccepted:
    task = ingest_site
    if site.platform == "pool":
        try:
            require_approved_pool_source(site)
        except PoolSourcePolicyError as error:
            raise HTTPException(409, str(error)) from error
        task = ingest_pool_site
    try:
        run = enqueue_job(db, site.id, "ingestion", task, job_timeout=3600)
    except DuplicateJobError as e:
        raise HTTPException(409, str(e)) from e
    return JobAccepted(job_id=run.queue_job_id, job_run_id=run.id)


@router.get("/{site_id}/ingestion-runs/latest", response_model=IngestionRunOut)
def latest_ingestion_run(
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> IngestionRun:
    run = latest_run(db, site.id)
    if run is None:
        raise HTTPException(404, f"no ingestion run for site {site.id}")
    return run


@router.get("/{site_id}/ingestion-runs", response_model=list[IngestionRunOut])
def list_ingestion_runs(
    site: Site = Depends(require_site_access),
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[IngestionRun]:
    return db.scalars(
        select(IngestionRun)
        .where(IngestionRun.site_id == site.id)
        .order_by(IngestionRun.started_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
