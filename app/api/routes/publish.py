from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Site, Suggestion
from app.schemas.job import JobAccepted
from app.services.job_service import DuplicateJobError, enqueue_job
from app.tasks.publication import publish_approved

router = APIRouter(prefix="/publish", tags=["publish"])


@router.post("/{site_id}", status_code=202, response_model=JobAccepted)
def trigger_publication(site_id: int, db: Session = Depends(get_db)) -> JobAccepted:
    if db.get(Site, site_id) is None:
        raise HTTPException(404, f"site {site_id} not found")
    try:
        run = enqueue_job(db, site_id, "publication", publish_approved, job_timeout=3600)
    except DuplicateJobError as e:
        raise HTTPException(409, str(e)) from e
    return JobAccepted(job_id=run.queue_job_id, job_run_id=run.id)


@router.get("/{site_id}/status")
def publication_status(site_id: int, db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(Suggestion.status, func.count())
        .where(Suggestion.site_id == site_id, Suggestion.status.in_(["approved", "applied"]))
        .group_by(Suggestion.status)
    ).all()
    counts = dict(rows)
    return {"applied": counts.get("applied", 0), "awaiting_publication": counts.get("approved", 0)}
