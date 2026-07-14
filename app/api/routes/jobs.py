from fastapi import APIRouter, HTTPException

from app.schemas.job import JobStatus
from app.services.job_service import get_job_status

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobStatus)
def job_status(job_id: str) -> JobStatus:
    status = get_job_status(job_id)
    if status is None:
        raise HTTPException(404, f"job {job_id} not found")
    return JobStatus(**status)
