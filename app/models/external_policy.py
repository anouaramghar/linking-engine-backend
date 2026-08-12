from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ExternalLinkPolicy(Base):
    """One outgoing external-link policy for one managed site."""

    __tablename__ = "external_link_policies"

    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True
    )
    external_links_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    require_https: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    min_trust_score: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    min_domain_age_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    trusted_tlds: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    allowlist_domains: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    blocklist_domains: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    competitor_domains: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    updated_by: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
