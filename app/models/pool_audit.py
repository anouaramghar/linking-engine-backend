from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PoolSourceAuditEvent(Base):
    __tablename__ = "pool_source_audit_events"
    __table_args__ = (
        Index(
            "ix_pool_source_audit_events_site_created",
            "site_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Deliberately not a foreign key: the immutable audit history survives site deletion.
    site_id: Mapped[int] = mapped_column(Integer)
    site_name: Mapped[str] = mapped_column(String(255))
    site_base_url: Mapped[str] = mapped_column(String(2048))
    action: Mapped[str] = mapped_column(String(50))
    operator_id: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
