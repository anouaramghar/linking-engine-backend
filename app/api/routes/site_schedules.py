from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_audit_actor, get_db, require_site_access, require_site_read
from app.models import Site
from app.schemas.site_schedule import SiteScheduleOut, SiteScheduleRunAccepted, SiteScheduleUpdate
from app.services.job_service import DuplicateJobError
from app.services.site_schedule_service import (
    ScheduledPipelineBusyError,
    load_schedule,
    save_schedule,
    schedule_output,
    start_site_pipeline,
)

router = APIRouter(prefix="/sites", tags=["site-schedules"])


def _managed_site(site: Site) -> Site:
    if site.platform == "pool":
        raise HTTPException(409, "content-pool sources use their own daily ingestion schedule")
    return site


@router.get("/{site_id}/schedule", response_model=SiteScheduleOut | None)
def get_site_schedule(
    site: Site = Depends(require_site_read),
    db: Session = Depends(get_db),
) -> SiteScheduleOut | None:
    _managed_site(site)
    schedule = load_schedule(db, site.id)
    return schedule_output(db, schedule) if schedule is not None else None


@router.put("/{site_id}/schedule", response_model=SiteScheduleOut)
def update_site_schedule(
    payload: SiteScheduleUpdate,
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> SiteScheduleOut:
    _managed_site(site)
    try:
        schedule = save_schedule(db, site, payload)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return schedule_output(db, schedule)


@router.post("/{site_id}/schedule/run-now", response_model=SiteScheduleRunAccepted, status_code=202)
def run_site_schedule_now(
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
    actor: str = Depends(get_audit_actor),
) -> SiteScheduleRunAccepted:
    _managed_site(site)
    schedule = load_schedule(db, site.id)
    try:
        batch, item = start_site_pipeline(
            db,
            site.id,
            schedule_id=schedule.id if schedule is not None else None,
            requested_by=actor,
        )
    except ScheduledPipelineBusyError as error:
        raise HTTPException(409, str(error)) from error
    except DuplicateJobError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return SiteScheduleRunAccepted(
        batch_id=batch.id, ingestion_job_run_id=item.ingestion_job_run_id
    )
