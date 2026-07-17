from datetime import datetime

from pydantic import BaseModel, ConfigDict


class JobAccepted(BaseModel):
    job_id: str
    job_run_id: int | None = None  # durable job_runs row (Phase 0, finding 7)


class JobStatus(BaseModel):
    job_id: str
    # RQ vocabulary while the job lives in Redis (queued | started | finished | failed),
    # job_runs vocabulary once evicted (queued | running | succeeded | failed)
    status: str
    result: dict | None = None
    error: str | None = None


class JobRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    kind: str
    status: str
    queue_job_id: str | None
    attempts: int
    result: dict | None
    error: str | None
    enqueued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
