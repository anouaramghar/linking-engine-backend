from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.article import Article

SuggestionMethod = Enum(
    "baseline_cosine", "gnn_graphsage", name="suggestion_method", native_enum=False, length=30
)
# pending -> approved | rejected; approved -> applying -> applied.
# 'applying' is the publication worker's claim: written only inside the publish
# transaction (never visible committed), so a crash rolls it back to 'approved'.
# 'applied' is set only by the publication worker.
SuggestionStatus = Enum(
    "pending",
    "approved",
    "rejected",
    "applying",
    "applied",
    name="suggestion_status",
    native_enum=False,
    length=20,
)


class Suggestion(Base):
    __tablename__ = "suggestions"
    __table_args__ = (Index("ix_suggestions_status_created_at", "status", "created_at"),)

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
    status: Mapped[str] = mapped_column(SuggestionStatus, default="pending", server_default="pending")
    anchor_text: Mapped[str | None] = mapped_column(Text)  # v4
    llm_model: Mapped[str | None] = mapped_column(String(100))  # v4 traceability
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source_article: Mapped[Article] = relationship(foreign_keys=[source_article_id])
    target_article: Mapped[Article] = relationship(foreign_keys=[target_article_id])
