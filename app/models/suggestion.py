from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.article import Article

SuggestionMethod = Enum(
    "baseline_cosine",
    "gnn_graphsage",
    "external_search",
    name="suggestion_method",
    native_enum=False,
    length=30,
)
# pending -> approved | rejected -> applied ('applied' set only by the publication worker)
SuggestionStatus = Enum(
    "pending", "approved", "rejected", "applied", name="suggestion_status", native_enum=False, length=20
)


class Suggestion(Base):
    __tablename__ = "suggestions"
    __table_args__ = (Index("ix_suggestions_status_created_at", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    source_article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    # NULL for external suggestions (v3) — the target lives in external_url instead
    target_article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    method: Mapped[str] = mapped_column(SuggestionMethod)
    score: Mapped[float] = mapped_column(Float)
    external_url: Mapped[str | None] = mapped_column(String(2048))  # v3
    external_title: Mapped[str | None] = mapped_column(Text)  # v3
    trust_score: Mapped[float | None] = mapped_column(Float)  # v3
    status: Mapped[str] = mapped_column(SuggestionStatus, default="pending", server_default="pending")
    anchor_text: Mapped[str | None] = mapped_column(Text)  # v4
    llm_model: Mapped[str | None] = mapped_column(String(100))  # v4 traceability
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source_article: Mapped[Article] = relationship(foreign_keys=[source_article_id])
    target_article: Mapped[Article | None] = relationship(foreign_keys=[target_article_id])

    @property
    def link_url(self) -> str:
        return self.external_url or self.target_article.url

    @property
    def link_title(self) -> str:
        return self.external_title or self.target_article.title

    # Text around the anchor occurrence, for the dashboard's in-context preview.
    def _context(self) -> tuple[str, str]:
        text = self.source_article.content_text
        if self.anchor_text:
            idx = text.lower().find(self.anchor_text.lower())
            if idx >= 0:
                return text[max(0, idx - 180) : idx], text[idx + len(self.anchor_text) : idx + 180]
        return text[:240], ""

    @property
    def context_before(self) -> str:
        return self._context()[0]

    @property
    def context_after(self) -> str:
        return self._context()[1]
