from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Site, Suggestion
from app.schemas.job import JobAccepted
from app.schemas.publication import PendingPublicationSite
from app.services.job_service import DuplicateJobError, enqueue_job
from app.tasks.publication import publish_approved

router = APIRouter(prefix="/publish", tags=["publish"])


@router.get("/pending", response_model=list[PendingPublicationSite])
def pending_publication_sites(
    db: Session = Depends(get_db),
) -> list[PendingPublicationSite]:
    rows = db.execute(
        select(
            Suggestion.site_id,
            func.count().label("awaiting_publication"),
        )
        .where(Suggestion.status == "approved")
        .group_by(Suggestion.site_id)
        .order_by(Suggestion.site_id)
    ).all()
    return [
        PendingPublicationSite(
            site_id=site_id,
            awaiting_publication=awaiting_publication,
        )
        for site_id, awaiting_publication in rows
    ]


@router.post("/{site_id}", status_code=202, response_model=JobAccepted)
def trigger_publication(site_id: int, db: Session = Depends(get_db)) -> JobAccepted:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, f"site {site_id} not found")
    if site.platform == "pool":
        raise HTTPException(409, "content-pool sources are read-only")
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
