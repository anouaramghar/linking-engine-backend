from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.article import Article

SuggestionMethod = Enum(
    "baseline_cosine",
    "hybrid_bm25",
    "gnn_graphsage",
    "external_search",
    name="suggestion_method",
    native_enum=False,
    length=30,
)
# pending -> approved | rejected; pending/approved -> expired; approved -> applying -> applied.
# 'applying' is the publication worker's claim: written only inside the publish
# transaction (never visible committed), so a crash rolls it back to 'approved'.
# 'applied' is set only by the publication worker.
# 'failed' is where a suggestion goes after publish_max_suggestion_attempts: a
# post locked by a plugin or a revoked password is not transient, and without a
# terminal state it is retried by every publication run for ever. Set only by
# the publication worker; an editor re-approving the row resets the count.
SuggestionStatus = Enum(
    "pending",
    "approved",
    "rejected",
    "expired",
    "applying",
    "applied",
    "failed",
    name="suggestion_status",
    native_enum=False,
    length=20,
)


class Suggestion(Base):
    __tablename__ = "suggestions"
    # Exact-status and default active-queue reads need different leading columns:
    # status-first serves a selected chip, while the partial indexes preserve
    # global score order across every non-expired status. All are ascending because
    # PostgreSQL can walk them backwards for the uniformly descending queue.
    __table_args__ = (
        Index("ix_suggestions_status_created_at", "status", "created_at"),
        Index("ix_suggestions_queue", "status", "score", "id"),
        Index("ix_suggestions_site_queue", "site_id", "status", "score", "id"),
        Index(
            "ix_suggestions_active_queue",
            "score",
            "id",
            postgresql_where=text("status <> 'expired'"),
        ),
        Index(
            "ix_suggestions_site_active_queue",
            "site_id",
            "score",
            "id",
            postgresql_where=text("status <> 'expired'"),
        ),
        Index(
            "uq_suggestions_active_source_external_url",
            "source_article_id",
            "external_url",
            unique=True,
            postgresql_where=text("external_url IS NOT NULL AND status <> 'expired'"),
        ),
        CheckConstraint(
            "(target_article_id IS NOT NULL) <> (external_url IS NOT NULL)",
            name="ck_suggestions_exactly_one_target",
        ),
        Index(
            "ix_suggestions_publication_pending",
            "site_id",
            "source_article_id",
            "score",
            "id",
            postgresql_where=text("status = 'approved' AND publication_plan_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable correlation key for logs, API responses and lifecycle events.
    # Unlike the database id it can safely be copied between environments.
    trace_id: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        index=True,
        default=lambda: str(uuid4()),
        server_default=text("gen_random_uuid()::text"),
    )
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    source_article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    target_article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    # Dynamic web-search targets are intentionally not imported as articles.
    # Content-pool articles are reusable corpus documents; a Tavily result is a
    # per-query candidate whose provider provenance belongs on this suggestion.
    external_url: Mapped[str | None] = mapped_column(String(2048))
    external_title: Mapped[str | None] = mapped_column(Text)
    external_snippet: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(50))
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    provider_score: Mapped[float | None] = mapped_column(Float)
    search_query: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str] = mapped_column(SuggestionMethod)
    # Cosine semantic similarity, for every method. The dashboard percentage, its
    # thresholds, and the global queue order all read this one column, so it has
    # to keep one meaning across methods — a pilot row and a baseline row at 0.82
    # are equally similar. What the Hybrid ranker used to *choose* the pair goes
    # in score_components instead of being rescaled into this column.
    score: Mapped[float] = mapped_column(Float)
    # Truthful, method-specific explanation of the row: for hybrid_bm25, the BM25
    # score that ordered it plus its fusion and per-retriever ranks. Null for rows
    # written before the column existed.
    score_components: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        SuggestionStatus, default="pending", server_default="pending"
    )
    # -- placement context (v4) -------------------------------------------
    # The phrase the link would be written on, copied verbatim out of the source
    # article rather than composed, so it can later be found again in the post.
    anchor_text: Mapped[str | None] = mapped_column(Text)  # v4
    llm_model: Mapped[str | None] = mapped_column(String(100))  # v4 traceability
    # The passage `anchor_text` sits in, also verbatim — what the dashboard shows
    # an editor so the decision is about a real position in the article.
    placement_context: Mapped[str | None] = mapped_column(Text)
    # Set once the model has been asked, whatever it answered. It is what
    # separates "not generated yet" from "generated, and no passage fit" — the
    # two look identical in the columns above and must not cost the same row a
    # second call on every preview.
    placement_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # -- publication accounting -------------------------------------------
    # What publication actually did. "applied" alone cannot answer the question
    # in-text placement exists to answer — how often a link lands in the prose
    # rather than in an appended block — and it reports a link an editor had
    # already added by hand identically to one we wrote.
    publish_outcome: Mapped[str | None] = mapped_column(String(20))
    # Consecutive failed publication attempts, reset on success and on
    # re-approval. Counts attempts, not retries, so the first failure is 1.
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Why the last attempt failed, so a quarantined row explains itself without
    # a log search.
    publish_error: Mapped[str | None] = mapped_column(Text)
    # The approved artifact this row is part of. Set only at final plan approval,
    # never at preparation: until a human approves the exact edit, a selected
    # suggestion belongs to nothing. Retained after success for audit, cleared
    # when a plan goes stale or a failed row is explicitly selected again.
    # ON DELETE SET NULL rather than CASCADE — deleting a plan must never delete
    # the editorial decisions that fed it.
    publication_plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("publication_plans.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source_article: Mapped[Article] = relationship(foreign_keys=[source_article_id])
    target_article: Mapped[Article | None] = relationship(foreign_keys=[target_article_id])
    events: Mapped[list["SuggestionEvent"]] = relationship(
        back_populates="suggestion",
        cascade="all, delete-orphan",
        order_by="SuggestionEvent.created_at",
    )

    @property
    def resolved_target_url(self) -> str:
        if self.target_article is not None:
            return self.target_article.url
        if self.external_url is not None:
            return self.external_url
        raise ValueError(f"suggestion {self.id} has no target URL")

    @property
    def resolved_target_title(self) -> str:
        if self.target_article is not None:
            return self.target_article.title
        if self.external_title is not None:
            return self.external_title
        return self.resolved_target_url

    @property
    def resolved_target_text(self) -> str:
        if self.target_article is not None:
            return self.target_article.content_text
        return self.external_snippet or self.external_title or ""


class SuggestionEvent(Base):
    """Immutable explanation of one suggestion lifecycle transition."""

    __tablename__ = "suggestion_events"
    __table_args__ = (
        Index(
            "ix_suggestion_events_suggestion_created",
            "suggestion_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    suggestion_id: Mapped[int] = mapped_column(
        ForeignKey("suggestions.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(30))
    actor: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    suggestion: Mapped[Suggestion] = relationship(back_populates="events")


class BulkReviewOperation(Base):
    """Durable identity for one server-side bulk rule and its exact undo cohort."""

    __tablename__ = "bulk_review_operations"
    __table_args__ = (
        Index("ix_bulk_review_operations_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor: Mapped[str] = mapped_column(String(255))
    from_status: Mapped[str] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    reviewed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    undone_count: Mapped[int | None] = mapped_column(Integer)
    skipped_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BulkReviewOperationItem(Base):
    """One suggestion changed by a bulk rule, stored without sending huge ID lists."""

    __tablename__ = "bulk_review_operation_items"
    __table_args__ = (
        Index("ix_bulk_review_operation_items_suggestion_id", "suggestion_id"),
    )

    operation_id: Mapped[str] = mapped_column(
        ForeignKey("bulk_review_operations.id", ondelete="CASCADE"), primary_key=True
    )
    suggestion_id: Mapped[int] = mapped_column(
        ForeignKey("suggestions.id", ondelete="CASCADE"), primary_key=True
    )
