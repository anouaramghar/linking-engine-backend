from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
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

    # Scoped rather than global so a 409 cannot be used to probe what a key
    # cannot read: under global uniqueness, "this URL already exists" is an
    # answer about sites outside the caller's scope. There are no clients here
    # (see app/services/authorization.py), so this is containment, not a claim
    # that two owners may hold one URL — in practice the scope is always the
    # same one, and the constraint then behaves exactly like a global one.
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
    # Operator-supplied registration date used by the deterministic trust score.
    # Unknown stays NULL instead of pretending that the source connection date
    # is the age of a domain on the public internet.
    domain_registered_at: Mapped[date | None] = mapped_column(Date)
    # Off by default, deliberately. Editorial feedback reorders production
    # results from approve/reject history, and that history is not yet evidence:
    # it activates after ten mixed rows, counts bulk decisions the same as
    # considered ones, and has never been measured against a held-out set. The
    # evidence plan asks for three representative sites, 100 individual labels
    # each and a versioned result before a ranking default may move. Until that
    # exists an operator may switch it on per site and own the outcome; nothing
    # switches it on for them.
    editorial_feedback_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    editorial_min_score_percent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    editorial_feedback_weight: Mapped[float] = mapped_column(
        Float, default=0.20, server_default="0.20"
    )
    editorial_feedback_min_samples: Mapped[int] = mapped_column(
        Integer, default=10, server_default="10"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def has_wordpress_credentials(self) -> bool:
        """Whether an account exists that could edit this site's posts.

        Publication reads each post with `context=edit` before it writes, and
        WordPress refuses that anonymously. Tested on the username so that
        listing sites does not decrypt a password per row; the two are only ever
        written as a pair.
        """
        return self.platform == "wordpress" and bool(self.wp_username)


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
