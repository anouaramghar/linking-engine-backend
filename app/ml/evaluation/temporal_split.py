from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.models import Article, InternalLink, Suggestion


GroundTruth = Literal["editor", "observed"]


@dataclass(frozen=True)
class TemporalLinkExample:
    site_id: int
    source_article_id: int
    target_article_id: int
    event_at: datetime
    source_created_at: datetime
    target_created_at: datetime
    source_is_new: bool
    target_is_new: bool

    def to_dict(self) -> dict:
        payload = asdict(self)
        for field in ("event_at", "source_created_at", "target_created_at"):
            payload[field] = payload[field].isoformat()
        return payload


@dataclass(frozen=True)
class TemporalEvaluationSplit:
    schema_version: int
    ground_truth: GroundTruth
    cutoff_at: datetime
    train: tuple[TemporalLinkExample, ...]
    test: tuple[TemporalLinkExample, ...]

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "ground_truth": self.ground_truth,
            "cutoff_at": self.cutoff_at.isoformat(),
            "train": [row.to_dict() for row in self.train],
            "test": [row.to_dict() for row in self.test],
        }


def _require_aware_cutoff(cutoff_at: datetime) -> None:
    if cutoff_at.tzinfo is None or cutoff_at.utcoffset() is None:
        raise ValueError("cutoff_at must include a timezone")


def _editor_rows(db: Session, site_ids: tuple[int, ...] | None):
    # A pair may have been suggested by more than one method. Its first successful
    # publication is the editorial event; later duplicate rows must not leak into test.
    applied = (
        select(
            Suggestion.site_id.label("site_id"),
            Suggestion.source_article_id.label("source_article_id"),
            Suggestion.target_article_id.label("target_article_id"),
            func.min(Suggestion.applied_at).label("event_at"),
        )
        .where(Suggestion.status == "applied", Suggestion.applied_at.is_not(None))
        .group_by(
            Suggestion.site_id,
            Suggestion.source_article_id,
            Suggestion.target_article_id,
        )
        .subquery()
    )
    source = aliased(Article)
    target = aliased(Article)
    query = (
        select(
            applied.c.site_id,
            applied.c.source_article_id,
            applied.c.target_article_id,
            applied.c.event_at,
            source.created_at.label("source_created_at"),
            target.created_at.label("target_created_at"),
        )
        .join(source, source.id == applied.c.source_article_id)
        .join(target, target.id == applied.c.target_article_id)
    )
    if site_ids:
        query = query.where(applied.c.site_id.in_(site_ids))
    return db.execute(query.order_by(applied.c.event_at, applied.c.source_article_id)).all()


def _observed_rows(db: Session, site_ids: tuple[int, ...] | None):
    source = aliased(Article)
    target = aliased(Article)
    query = (
        select(
            source.site_id.label("site_id"),
            InternalLink.source_article_id,
            InternalLink.target_article_id,
            InternalLink.first_seen_at.label("event_at"),
            source.created_at.label("source_created_at"),
            target.created_at.label("target_created_at"),
        )
        .join(source, source.id == InternalLink.source_article_id)
        .join(target, target.id == InternalLink.target_article_id)
        .where(source.site_id == target.site_id)
    )
    if site_ids:
        query = query.where(source.site_id.in_(site_ids))
    return db.execute(query.order_by(InternalLink.first_seen_at, InternalLink.id)).all()


def build_temporal_evaluation_split(
    db: Session,
    *,
    cutoff_at: datetime,
    ground_truth: GroundTruth = "editor",
    site_ids: tuple[int, ...] | None = None,
) -> TemporalEvaluationSplit:
    """Build a deterministic split where no event at/after cutoff enters training."""
    _require_aware_cutoff(cutoff_at)
    if ground_truth == "editor":
        rows = _editor_rows(db, site_ids)
    elif ground_truth == "observed":
        rows = _observed_rows(db, site_ids)
    else:
        raise ValueError(f"unsupported ground_truth: {ground_truth!r}")

    examples = tuple(
        TemporalLinkExample(
            site_id=row.site_id,
            source_article_id=row.source_article_id,
            target_article_id=row.target_article_id,
            event_at=row.event_at,
            source_created_at=row.source_created_at,
            target_created_at=row.target_created_at,
            source_is_new=row.source_created_at >= cutoff_at,
            target_is_new=row.target_created_at >= cutoff_at,
        )
        for row in rows
    )
    train = tuple(row for row in examples if row.event_at < cutoff_at)
    test = tuple(row for row in examples if row.event_at >= cutoff_at)
    return TemporalEvaluationSplit(
        schema_version=1,
        ground_truth=ground_truth,
        cutoff_at=cutoff_at,
        train=train,
        test=test,
    )
