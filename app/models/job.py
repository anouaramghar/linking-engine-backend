from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

JobKind = Enum(
    "ingestion",
    "analysis",
    "publication_preparation",
    "publication",
    name="job_kind",
    native_enum=False,
    length=32,
)
# queued -> running -> succeeded | failed; a failed attempt goes back to running on RQ retry.
# Rows are created at enqueue time, so a job Redis loses is still visible as 'queued'
# and reconciled to 'failed' on the next enqueue attempt (Phase 0, finding 7).
JobRunStatus = Enum(
    "queued", "running", "succeeded", "failed", name="job_run_status", native_enum=False, length=20
)


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_job_runs_site_kind_status", "site_id", "kind", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(JobKind)
    status: Mapped[str] = mapped_column(JobRunStatus, default="queued", server_default="queued")
    queue_job_id: Mapped[str | None] = mapped_column(String(64), index=True)  # RQ job id
    requested_by: Mapped[str | None] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    result: Mapped[dict | None] = mapped_column(JSONB)
    progress: Mapped[dict | None] = mapped_column(JSONB)
    progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
