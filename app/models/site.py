from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_types import EncryptedCredential

Platform = Enum("wordpress", "html", "pool", name="platform", native_enum=False, length=20)
RunStatus = Enum("running", "succeeded", "failed", name="run_status", native_enum=False, length=20)


class Site(Base):
    __tablename__ = "sites"

    # Two clients may legitimately own the same URL — an agency and the brand
    # itself, or the same domain moving between tenants. Global uniqueness would
    # also let one tenant probe another's inventory through the 409.
    __table_args__ = (UniqueConstraint("tenant_id", "base_url", name="uq_sites_tenant_base_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255))
    base_url: Mapped[str] = mapped_column(String(2048))
    platform: Mapped[str] = mapped_column(Platform)
    crawl_frequency: Mapped[str] = mapped_column(
        String(50), default="manual", server_default="manual"
    )
    # WordPress Application Passwords (A2) — HTTP Basic Auth
    wp_username: Mapped[str | None] = mapped_column(String(255))
    wp_app_password: Mapped[str | None] = mapped_column(EncryptedCredential())
    pool_source_approved: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    pool_source_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pool_source_approved_by: Mapped[str | None] = mapped_column(String(255))
    pool_source_consecutive_failures: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
    )
    pool_source_quarantined: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    pool_source_quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pool_source_quarantine_reason: Mapped[str | None] = mapped_column(Text)
    pool_source_last_reactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    pool_source_last_reactivated_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    job_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_runs.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(RunStatus, default="running", server_default="running")
    articles_upserted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    links_found: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
