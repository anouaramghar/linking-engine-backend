from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

EMBEDDING_DIM = 1024  # BAAI/bge-m3

TaxonomyKind = Enum("category", "tag", name="taxonomy_kind", native_enum=False, length=20)


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        # Idempotent ingestion: re-crawl updates, never duplicates
        UniqueConstraint("site_id", "url"),
        UniqueConstraint("site_id", "external_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(255))  # WP post id
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(Text)
    content_text: Mapped[str] = mapped_column(Text)
    content_html: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(10))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Embedding(Base):
    __tablename__ = "embeddings"
    __table_args__ = (UniqueConstraint("article_id", "model"),)  # encode-once cache

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(100))
    vector: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Taxonomy(Base):
    __tablename__ = "taxonomies"
    __table_args__ = (UniqueConstraint("site_id", "kind", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(TaxonomyKind)
    name: Mapped[str] = mapped_column(String(255))
    external_id: Mapped[str | None] = mapped_column(String(255))


class ArticleTaxonomy(Base):
    __tablename__ = "article_taxonomies"

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    taxonomy_id: Mapped[int] = mapped_column(
        ForeignKey("taxonomies.id", ondelete="CASCADE"), primary_key=True
    )
