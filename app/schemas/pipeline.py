from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PipelineBatchCreate(BaseModel):
    site_ids: list[int] = Field(min_length=1, max_length=100)

    @field_validator("site_ids")
    @classmethod
    def unique_positive_site_ids(cls, value: list[int]) -> list[int]:
        if any(site_id <= 0 for site_id in value):
            raise ValueError("site_ids must contain only positive integers")
        if len(value) != len(set(value)):
            raise ValueError("site_ids must not contain duplicates")
        return value


class PipelineSiteRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    status: Literal[
        "queued",
        "ingestion_running",
        "analysis_queued",
        "analysis_running",
        "succeeded",
        "failed",
        "cancelled",
    ]
    stage: Literal["ingestion", "analysis", "completed"]
    ingestion_job_run_id: int | None
    analysis_job_run_id: int | None
    retry_count: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class PipelineBatchOut(BaseModel):
    id: int
    status: Literal["queued", "running", "succeeded", "failed", "partial_failed", "cancelled"]
    total: int
    active: int
    succeeded: int
    failed: int
    cancelled: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    sites: list[PipelineSiteRunOut]
