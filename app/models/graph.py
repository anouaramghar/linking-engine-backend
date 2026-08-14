from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class GraphSnapshot(Base):
    """One reproducible view of a site's accepted active-link graph.

    The graph is derived from the accepted article/link state, but it is kept as
    its own immutable observation so a later crawl cannot rewrite the structural
    explanation an editor saw. ``graph_version`` is the SHA-256 of the ordered
    node/edge input plus the algorithm version and source ingestion run.
    """

    __tablename__ = "graph_snapshots"
    __table_args__ = (
        UniqueConstraint("site_id", "graph_version", name="uq_graph_snapshots_site_version"),
        Index("ix_graph_snapshots_site_computed", "site_id", "computed_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    source_ingestion_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="SET NULL"), index=True
    )
    algorithm_version: Mapped[str] = mapped_column(String(40))
    graph_version: Mapped[str] = mapped_column(String(64))
    article_count: Mapped[int] = mapped_column(Integer)
    edge_count: Mapped[int] = mapped_column(Integer)
    orphan_count: Mapped[int] = mapped_column(Integer)
    underlinked_count: Mapped[int] = mapped_column(Integer)
    hub_count: Mapped[int] = mapped_column(Integer)
    saturated_count: Mapped[int] = mapped_column(Integer)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    features: Mapped[list["GraphFeature"]] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
        order_by="GraphFeature.article_id",
    )


class GraphFeature(Base):
    """A historical structural feature row for one article in one snapshot.

    Article title and URL are copied deliberately. A later deletion or URL
    change must not make an old graph snapshot unreadable.
    """

    __tablename__ = "graph_features"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "article_id", name="uq_graph_features_snapshot_article"),
        Index("ix_graph_features_snapshot_orphan", "snapshot_id", "orphan_flag"),
        Index("ix_graph_features_snapshot_underlinked", "snapshot_id", "underlinked_flag"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("graph_snapshots.id", ondelete="CASCADE"), index=True
    )
    # This is an historical identity, not a foreign key: preserving a snapshot
    # matters even when a later accepted crawl removes the article.
    article_id: Mapped[int] = mapped_column(Integer, index=True)
    article_url: Mapped[str] = mapped_column(Text)
    article_title: Mapped[str] = mapped_column(Text)
    in_degree: Mapped[int] = mapped_column(Integer)
    out_degree: Mapped[int] = mapped_column(Integer)
    orphan_flag: Mapped[bool] = mapped_column(Boolean)
    underlinked_flag: Mapped[bool] = mapped_column(Boolean)
    hub_flag: Mapped[bool] = mapped_column(Boolean)
    saturated_flag: Mapped[bool] = mapped_column(Boolean)
    hub_score: Mapped[float] = mapped_column()
    saturation_score: Mapped[float] = mapped_column()

    snapshot: Mapped[GraphSnapshot] = relationship(back_populates="features")
