"""The immutable artifact a human approves before anything is written back.

A suggestion whose status is 'approved' has only been *selected*: it says an
editor wants this link, not that anyone has seen the edit it produces. Every
decision that turns a selection into bytes — which anchor, in-text or appended
block, which reciprocal direction wins, what the post looked like at the time —
used to happen inside the publication worker, after the last human had left. The
operator approved an intention and the worker invented the change.

A PublicationPlan is that change, frozen. Preparation reads the live post,
generates any missing placement, renders the exact resulting HTML, and stores
both sides. Approval binds a named operator to a SHA-256 hash over the whole
artifact. Publication sends `updated_html` verbatim, and only while the live post
still equals `original_html`.

One plan per source article, because one WordPress post is one edit and one
revision: retries, staleness, audit history, and partial failures then stay
independent per post rather than per approval batch.

Artifact fields are never updated in place. A different edit is a different row
with a different hash, and needs its own approval.
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

#: prepared -> approved -> applied.
#: prepared -> superseded (a newer preparation replaced it; never applied to an
#: approved plan, whose artifact a human is already bound to).
#: approved -> stale (the live post changed after approval: no write happens, the
#: suggestions go back to selected, and a new plan must be prepared and approved).
#: approved -> failed (integrity mismatch, or repeated hard publication failure).
PublicationPlanStatus = Enum(
    "prepared",
    "approved",
    "applied",
    "stale",
    "superseded",
    "failed",
    name="publication_plan_status",
    native_enum=False,
    length=20,
)

#: Statuses in which a plan still describes a change that may reach WordPress.
#: At most one such plan may exist per source article — two live snapshots of the
#: same post would both look publishable while only one can be correct.
ACTIVE_PLAN_STATUSES = ("prepared", "approved")

#: Bumped whenever the hashed artifact's shape changes. It is hashed too, so an
#: artifact serialized under an older shape can never collide with a newer one.
PLAN_SCHEMA_VERSION = 1


class PublicationPlan(Base):
    __tablename__ = "publication_plans"
    __table_args__ = (
        # The worker's queue read: every approved plan for one site.
        Index("ix_publication_plans_site_status", "site_id", "status"),
        Index(
            "ux_publication_plans_active_source",
            "source_article_id",
            unique=True,
            postgresql_where=text("status IN ('prepared', 'approved')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    source_article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    #: The source URL as it was when the operator looked at it, not as it is now.
    source_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        PublicationPlanStatus, default="prepared", server_default="prepared"
    )
    #: Exact editable WordPress content read during preparation. Publication
    #: refuses to write unless the live post still equals this byte for byte.
    original_html: Mapped[str] = mapped_column(Text)
    #: Exact content that may be sent after approval. Nothing renders it again.
    updated_html: Mapped[str] = mapped_column(Text)
    #: Ordered, immutable snapshots: position, suggestion_id, target_url,
    #: anchor_text, outcome. Stored as JSONB rather than a child table because no
    #: query searches or updates one item — they are read and hashed as a whole.
    items: Mapped[list] = mapped_column(JSONB)
    plan_hash: Mapped[str] = mapped_column(String(64), index=True)
    #: Set equal to `plan_hash` at approval. What the operator actually agreed to,
    #: kept separately so a recomputation can be compared against it rather than
    #: against a column that would have been rewritten by the same mutation.
    approved_hash: Mapped[str | None] = mapped_column(String(64))
    #: From `require_operator_identity`: a person, not a shared service key.
    approved_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Why a stale or failed plan is terminal, bounded, so the row explains itself
    #: without a log search.
    failure_reason: Mapped[str | None] = mapped_column(Text)
