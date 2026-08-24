from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator


ScheduleCadence = Literal["daily", "weekly"]


class SiteScheduleUpdate(BaseModel):
    enabled: bool = False
    cadence: ScheduleCadence = "daily"
    weekday: int | None = Field(default=None, ge=0, le=6)
    local_time: time = time(2, 0)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_calendar(self) -> "SiteScheduleUpdate":
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown timezone: {self.timezone}") from error
        if self.cadence == "weekly" and self.weekday is None:
            raise ValueError("weekday is required for weekly schedules")
        if self.cadence == "daily":
            self.weekday = None
        return self


class SiteScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    enabled: bool
    cadence: ScheduleCadence
    weekday: int | None
    local_time: time
    timezone: str
    next_run_at: datetime | None
    last_attempt_at: datetime | None
    last_attempt_status: Literal["queued", "skipped", "failed"] | None
    last_attempt_error: str | None
    last_pipeline_batch_id: int | None = None
    last_run_status: (
        Literal[
            "queued",
            "running",
            "succeeded",
            "failed",
            "partial_failed",
            "cancelled",
        ]
        | None
    ) = None
    last_run_started_at: datetime | None = None
    last_run_finished_at: datetime | None = None
    last_run_error: str | None = None


class SiteScheduleRunAccepted(BaseModel):
    batch_id: int
    ingestion_job_run_id: int
