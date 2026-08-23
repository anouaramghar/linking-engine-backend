from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PipelineBatchCreate(BaseModel):
    site_ids: list[int] = Field(min_length=1, max_length=100)
    expected_active_job_run_ids: list[int] | None = Field(None, max_length=200)

    @field_validator("site_ids")
    @classmethod
    def unique_positive_site_ids(cls, value: list[int]) -> list[int]:
        if any(site_id <= 0 for site_id in value):
            raise ValueError("site_ids must contain only positive integers")
        if len(value) != len(set(value)):
            raise ValueError("site_ids must not contain duplicates")
        return value

    @field_validator("expected_active_job_run_ids")
    @classmethod
    def sorted_unique_positive_job_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(run_id <= 0 for run_id in value):
            raise ValueError("expected_active_job_run_ids must contain positive integers")
        if value != sorted(set(value)):
            raise ValueError("expected_active_job_run_ids must be sorted and unique")
        return value


class PipelineRetryGuard(BaseModel):
    expected_batch_status: str
    expected_site_status: Literal["failed"]
    expected_stage: Literal["ingestion", "analysis"]
    expected_retry_count: int = Field(ge=0)


class PipelineCancelSiteGuard(BaseModel):
    site_id: int = Field(ge=1)
    status: Literal["queued", "ingestion_running", "analysis_queued", "analysis_running"]
    stage: Literal["ingestion", "analysis"]
    ingestion_job_run_id: int | None = Field(None, ge=1)
    analysis_job_run_id: int | None = Field(None, ge=1)


class PipelineCancelGuard(BaseModel):
    expected_batch_status: Literal["queued", "running"]
    expected_sites: list[PipelineCancelSiteGuard] = Field(min_length=1, max_length=100)

    @field_validator("expected_sites")
    @classmethod
    def sorted_unique_sites(
        cls, value: list[PipelineCancelSiteGuard]
    ) -> list[PipelineCancelSiteGuard]:
        site_ids = [item.site_id for item in value]
        if site_ids != sorted(set(site_ids)):
            raise ValueError("expected_sites must be sorted by unique site_id")
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
