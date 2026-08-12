from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

JobStatusValue = Literal["queued", "running", "succeeded", "failed"]


class JobAccepted(BaseModel):
    job_id: str
    job_run_id: int | None = None  # durable job_runs row (Phase 0, finding 7)


class JobStatus(BaseModel):
    job_id: str
    # Stable API vocabulary, independent of whether Redis still has the RQ job.
    status: JobStatusValue
    result: dict | None = None
    progress: dict | None = None
    progress_at: datetime | None = None
    error: str | None = None


class JobRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    kind: str
    status: JobStatusValue
    queue_job_id: str | None
    requested_by: str | None
    attempts: int
    result: dict | None
    progress: dict | None
    progress_at: datetime | None
    error: str | None
    enqueued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
