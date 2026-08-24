from datetime import datetime, time

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


ScheduleCadence = Enum(
    "daily",
    "weekly",
    name="site_schedule_cadence",
    native_enum=False,
    length=16,
)


class SiteSchedule(Base):
    """One durable crawl-then-analysis schedule for a managed site.

    The schedule is configuration, not a queue job. ``next_run_at`` is the
    coordinator's durable cursor; every execution creates a normal pipeline
    batch, which keeps the existing crawl, analysis, cancellation, and retry
    contracts in one place.
    """

    __tablename__ = "site_schedules"
    __table_args__ = (
        UniqueConstraint("site_id"),
        Index("ix_site_schedules_due", "enabled", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    cadence: Mapped[str] = mapped_column(ScheduleCadence, default="daily", server_default="daily")
    weekday: Mapped[int | None] = mapped_column(Integer)
    local_time: Mapped[time] = mapped_column(Time, default=time(2), server_default="02:00:00")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_status: Mapped[str | None] = mapped_column(String(16))
    last_attempt_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
