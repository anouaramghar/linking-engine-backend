from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator


ScheduleCadence = Literal["daily", "weekly"]


class SiteScheduleExpected(BaseModel):
    """The exact schedule configuration a guarded update was previewed against."""

    model_config = ConfigDict(extra="forbid")

    exists: bool
    enabled: bool | None = None
    cadence: ScheduleCadence | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    local_time: time | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "SiteScheduleExpected":
        required = (self.enabled, self.cadence, self.local_time, self.timezone)
        if self.exists:
            if any(value is None for value in required):
                raise ValueError(
                    "an existing schedule snapshot must include its full configuration"
                )
            if self.cadence == "daily" and self.weekday is not None:
                raise ValueError("daily schedule snapshots cannot include a weekday")
            if self.cadence == "weekly" and self.weekday is None:
                raise ValueError("weekly schedule snapshots require a weekday")
        elif any(
            value is not None
            for value in (self.enabled, self.cadence, self.weekday, self.local_time, self.timezone)
        ):
            raise ValueError("an absent schedule snapshot cannot include configuration")
        return self

    def wire_state(self) -> dict[str, object]:
        """Return the compact JSON state used by previews and guarded writes."""
        if not self.exists:
            return {"exists": False}
        return self.model_dump(mode="json")


class SiteScheduleUpdate(BaseModel):
    enabled: bool = False
    cadence: ScheduleCadence = "daily"
    weekday: int | None = Field(default=None, ge=0, le=6)
    local_time: time = time(2, 0)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    expected: SiteScheduleExpected | None = None

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

    def configuration(self) -> dict[str, object]:
        """Return only the normalized schedule values accepted by the update route."""
        return self.model_dump(
            mode="json",
            include={"enabled", "cadence", "weekday", "local_time", "timezone"},
        )


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
    created_by: str | None = None
    updated_by: str | None = None


class SiteScheduleRunAccepted(BaseModel):
    batch_id: int
    ingestion_job_run_id: int
