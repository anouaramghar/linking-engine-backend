from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_api_key, require_site_access
from app.api.pagination import MAX_PAGE_SIZE
from app.models import JobRun, Site
from app.schemas.job import JobRunOut, JobStatus
from app.services.authorization import Principal, authorize_site, tenant_site_filter
from app.services.job_service import (
    JobCancellationConflict,
    cancel_job_run,
    get_job_status,
    reconcile_active_job_runs,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/active", response_model=list[JobRunOut])
def list_active_job_runs(
    limit: int = Query(MAX_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> list[JobRun]:
    """Return durable work that is still present in its RQ queue."""
    query = (
        select(JobRun)
        .where(JobRun.status.in_(("queued", "running", "cancel_requested")))
        .order_by(JobRun.enqueued_at.desc())
        .limit(limit)
    )
    owned = tenant_site_filter(principal)
    if owned is not None:
        query = query.join(Site, Site.id == JobRun.site_id).where(owned)
    return reconcile_active_job_runs(db, list(db.scalars(query).all()))


@router.get("/site/{site_id}", response_model=list[JobRunOut])
def list_job_runs(
    site: Site = Depends(require_site_access),
    kind: str | None = None,
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[JobRun]:
    query = select(JobRun).where(JobRun.site_id == site.id)
    if kind:
        query = query.where(JobRun.kind == kind)
    return db.scalars(query.order_by(JobRun.enqueued_at.desc()).limit(limit).offset(offset)).all()


@router.post("/runs/{job_run_id}/cancel", response_model=JobRunOut)
def cancel_job(
    job_run_id: int,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> JobRun:
    run = db.get(JobRun, job_run_id)
    if run is None:
        raise HTTPException(404, f"job run {job_run_id} not found")
    authorize_site(db, principal, run.site_id)
    try:
        return cancel_job_run(db, job_run_id)
    except JobCancellationConflict as error:
        raise HTTPException(409, str(error)) from error


@router.get("/{job_id}", response_model=JobStatus)
def job_status(
    job_id: str,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> JobStatus:
    run = db.scalars(select(JobRun).where(JobRun.queue_job_id == job_id)).first()
    if run is None:
        # Do not consult Redis before ownership is known — avoids leaking live
        # RQ state for foreign jobs.
        raise HTTPException(404, f"job {job_id} not found")
    authorize_site(db, principal, run.site_id)
    status = get_job_status(job_id)
    if status is None:
        raise HTTPException(404, f"job {job_id} not found")
    return JobStatus(**status)
