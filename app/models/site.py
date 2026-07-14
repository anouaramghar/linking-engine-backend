from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

Platform = Enum("wordpress", "html", name="platform", native_enum=False, length=20)
RunStatus = Enum("running", "succeeded", "failed", name="run_status", native_enum=False, length=20)


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    base_url: Mapped[str] = mapped_column(String(2048), unique=True)
    platform: Mapped[str] = mapped_column(Platform)
    crawl_frequency: Mapped[str] = mapped_column(String(50), default="manual", server_default="manual")
    # WordPress Application Passwords (A2) — HTTP Basic Auth
    wp_username: Mapped[str | None] = mapped_column(String(255))
    wp_app_password: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(RunStatus, default="running", server_default="running")
    articles_upserted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    links_found: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
