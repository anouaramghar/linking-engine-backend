from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class EvaluationSnapshot(Base):
    """One durable daily orphan-page observation for a managed site."""

    __tablename__ = "evaluation_snapshots"
    __table_args__ = (UniqueConstraint("snapshot_date", "site_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"),
        index=True,
    )
    active_articles: Mapped[int] = mapped_column(Integer)
    orphan_pages: Mapped[int] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
