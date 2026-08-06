from datetime import datetime

from sqlalchemy import (
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
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    source_article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    target_article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source_article: Mapped[Article] = relationship(foreign_keys=[source_article_id])
    target_article: Mapped[Article] = relationship(foreign_keys=[target_article_id])
