"""Temporal train/test split (v0 protocol): train on links first seen before the cutoff,
test on links that appeared after — the model never sees the future."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Article, InternalLink

Pair = tuple[int, int]  # (source_article_id, target_article_id)


def link_pairs(db: Session, site_id: int) -> list[tuple[int, int, datetime]]:
    rows = db.execute(
        select(
            InternalLink.source_article_id,
            InternalLink.target_article_id,
            InternalLink.first_seen_at,
        )
        .join(Article, Article.id == InternalLink.source_article_id)
        .where(Article.site_id == site_id)
    ).all()
    return [(r[0], r[1], r[2]) for r in rows]


def temporal_split(
    pairs: list[tuple[int, int, datetime]], test_fraction: float = 0.2
) -> tuple[list[Pair], list[Pair]]:
    """Newest test_fraction of links (by first_seen_at) become the test set.

    ponytail: on a single-crawl corpus every first_seen_at is identical, so the split
    degenerates to arbitrary order — a second crawl over time makes it meaningful.
    """
    if len(pairs) < 2:
        return [(s, t) for s, t, _ in pairs], []
    ordered = sorted(pairs, key=lambda p: p[2])
    n_test = max(1, int(len(ordered) * test_fraction))
    train = [(s, t) for s, t, _ in ordered[:-n_test]]
    test = [(s, t) for s, t, _ in ordered[-n_test:]]
    return train, test


def truth_by_source(test_pairs: list[Pair]) -> dict[int, set[int]]:
    truth: dict[int, set[int]] = {}
    for s, t in test_pairs:
        truth.setdefault(s, set()).add(t)
    return truth
