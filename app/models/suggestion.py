from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Index, String, Text, func, text
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
SuggestionStatus = Enum(
    "pending",
    "approved",
    "rejected",
    "expired",
    "applying",
    "applied",
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
    score: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(
        SuggestionStatus, default="pending", server_default="pending"
    )
    anchor_text: Mapped[str | None] = mapped_column(Text)  # v4
    llm_model: Mapped[str | None] = mapped_column(String(100))  # v4 traceability
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source_article: Mapped[Article] = relationship(foreign_keys=[source_article_id])
    target_article: Mapped[Article] = relationship(foreign_keys=[target_article_id])
