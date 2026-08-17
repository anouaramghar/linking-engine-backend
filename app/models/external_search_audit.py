"""Durable audit trail for paid external-search fallback decisions."""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ExternalSearchAuditEvent(Base):
    """One bounded request outcome or candidate decision.

    Blocked provider results never become suggestions, so their filter decision
    cannot live in ``suggestion_events``. This table keeps that missing half of
    the trace without storing credentials or full provider responses.
    """

    __tablename__ = "external_search_audit_events"
    __table_args__ = (
        Index(
            "ix_external_search_audit_site_created",
            "site_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_external_search_audit_source_created",
            "source_article_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    source_article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    suggestion_id: Mapped[int | None] = mapped_column(
        ForeignKey("suggestions.id", ondelete="SET NULL"), index=True
    )
    job_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_runs.id", ondelete="SET NULL"), index=True
    )
    provider: Mapped[str] = mapped_column(String(50))
    provider_request_id: Mapped[str | None] = mapped_column(String(255), index=True)
    search_query: Mapped[str] = mapped_column(Text)
    candidate_url: Mapped[str | None] = mapped_column(String(2048))
    provider_score: Mapped[float | None] = mapped_column(Float)
    decision: Mapped[str] = mapped_column(String(30))
    details: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
