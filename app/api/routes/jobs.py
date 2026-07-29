from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.pagination import MAX_PAGE_SIZE
from app.models import JobRun, Site
from app.schemas.job import JobRunOut, JobStatus
from app.services.job_service import get_job_status, reconcile_active_job_runs

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/active", response_model=list[JobRunOut])
def list_active_job_runs(
    limit: int = Query(MAX_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
) -> list[JobRun]:
    """Return durable work that is still present in its RQ queue."""
    query = (
        select(JobRun)
        .where(JobRun.status.in_(("queued", "running")))
        .order_by(JobRun.enqueued_at.desc())
        .limit(limit)
    )
    return reconcile_active_job_runs(db, list(db.scalars(query).all()))


@router.get("/site/{site_id}", response_model=list[JobRunOut])
def list_job_runs(
    site_id: int,
    kind: str | None = None,
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[JobRun]:
    if db.get(Site, site_id) is None:
        raise HTTPException(404, f"site {site_id} not found")
    query = select(JobRun).where(JobRun.site_id == site_id)
    if kind:
        query = query.where(JobRun.kind == kind)
    return db.scalars(query.order_by(JobRun.enqueued_at.desc()).limit(limit).offset(offset)).all()


@router.get("/{job_id}", response_model=JobStatus)
def job_status(job_id: str) -> JobStatus:
    status = get_job_status(job_id)
    if status is None:
        raise HTTPException(404, f"job {job_id} not found")
    return JobStatus(**status)
