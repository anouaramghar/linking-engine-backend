from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import IngestionRun, Site
from app.schemas.job import JobAccepted
from app.schemas.site import IngestionRunOut
from app.services.ingestion_service import latest_run
from app.tasks.ingestion import ingest_site
from app.tasks.queues import default_queue

router = APIRouter(prefix="/sites", tags=["ingestion"])


@router.post("/{site_id}/ingest", status_code=202, response_model=JobAccepted)
def trigger_ingestion(site_id: int, db: Session = Depends(get_db)) -> JobAccepted:
    if db.get(Site, site_id) is None:
        raise HTTPException(404, f"site {site_id} not found")
    job = default_queue.enqueue(ingest_site, site_id, job_timeout=3600)
    return JobAccepted(job_id=job.id)


@router.get("/{site_id}/ingestion-runs/latest", response_model=IngestionRunOut)
def latest_ingestion_run(site_id: int, db: Session = Depends(get_db)) -> IngestionRun:
    run = latest_run(db, site_id)
    if run is None:
        raise HTTPException(404, f"no ingestion run for site {site_id}")
    return run


@router.get("/{site_id}/ingestion-runs", response_model=list[IngestionRunOut])
def list_ingestion_runs(
    site_id: int, limit: int = 50, offset: int = 0, db: Session = Depends(get_db)
) -> list[IngestionRun]:
    return db.scalars(
        select(IngestionRun)
        .where(IngestionRun.site_id == site_id)
        .order_by(IngestionRun.started_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
