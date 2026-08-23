from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

JobStatusValue = Literal["queued", "running", "succeeded", "failed"]


class JobAccepted(BaseModel):
    job_id: str
    job_run_id: int | None = None  # durable job_runs row (Phase 0, finding 7)


class JobStartGuard(BaseModel):
    """Optional optimistic guard used by staged dashboard job starts.

    Ordinary dashboard callers may omit the body. A staged agent proposal
    always supplies the exact active same-kind job ids it observed (normally
    an empty list), so a confirmation card cannot start work after the site's
    job state has changed underneath it.
    """

    expected_active_job_run_ids: list[int] = Field(max_length=100)

    @field_validator("expected_active_job_run_ids")
    @classmethod
    def sorted_unique_positive_ids(cls, value: list[int]) -> list[int]:
        if any(run_id <= 0 for run_id in value):
            raise ValueError("expected_active_job_run_ids must contain positive integers")
        if value != sorted(set(value)):
            raise ValueError("expected_active_job_run_ids must be sorted and unique")
        return value


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
