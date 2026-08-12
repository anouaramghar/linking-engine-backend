"""Rank targets for one source article under offline evaluation conditions.

The production ranker cannot be used unchanged here, and the reason matters.
`_PILOT_ELIGIBILITY_SQL` drops any target the source already links to, because
proposing an existing link wastes an editor's time. The observed ground truth is
made of exactly those existing links, so the production predicate would hide
every correct answer and every metric would read zero.

This module therefore keeps the rules that describe a *bad target* and drops the
three that describe *editorial history*:

    kept     minimum score, near-duplicate ceiling, identical title,
             identical fingerprint, low-value titles and urls, same site, active
    dropped  already linked, an existing suggestion, the reverse direction

The ordering recipe itself is imported from `app.ml.hybrid`, not reimplemented,
so a change to how suggestions are ranked changes this measurement too.

The candidate pool is every article published before the cutoff. An article that
did not exist yet cannot be proposed, so a method cannot be rewarded for
choosing one.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.ml.baseline import low_value_target_params, semantic_scores_for_targets
from app.ml.hybrid import (
    DENSE_POOL_SIZE,
    HYBRID_POOL_SIZE,
    LEXICAL_POOL_SIZE,
    CorpusArticle,
    normalized_title,
    structured_terms,
    weighted_rrf_scores,
)
from app.ml.lexical import BM25Index, rank_scores
from app.models import Article, ArticleTaxonomy, Embedding, Taxonomy


#: The three orderings a comparison table holds, weakest first.
#:
#:  lexical  BM25 over titles, taxonomies and body text. Baseline 2 on the board.
#:  dense    cosine over the article embeddings. The V1 ranking.
#:  hybrid   what production ships: BM25 order with the fused rank breaking ties.
#:
#: A GNN becomes a fourth name here and nothing else in the measurement changes.
RankingMethod = Literal["lexical", "dense", "hybrid"]
RANKING_METHODS: tuple[RankingMethod, ...] = ("lexical", "dense", "hybrid")


@dataclass(frozen=True)
class PoolStats:
    """What the pool contains, so a metric can be read against its own setup."""

    site_id: int
    pool_articles: int
    source_articles: int
    excluded_low_value: int


def _low_value_filter():
    params = low_value_target_params()
    titles = set(params["low_value_titles"])
    pattern = str(params["low_value_url_pattern"])

    def is_low_value(title: str, url: str) -> bool:
        if normalized_title(title) in titles:
            return True
        return bool(pattern) and re.search(pattern, url.lower()) is not None

    return is_low_value


def _load_articles(
    db: Session,
    *,
    site_id: int,
    model: str,
    published_before: datetime | None,
    ids: set[int] | None,
) -> tuple[dict[int, CorpusArticle], dict[int, str]]:
    query = (
        select(
            Article.id,
            Article.title,
            Article.url,
            Article.content_text,
            Embedding.content_fingerprint,
        )
        .join(Embedding, (Embedding.article_id == Article.id) & (Embedding.model == model))
        .where(Article.site_id == site_id, Article.is_active.is_(True))
    )
    if published_before is not None:
        query = query.where(Article.published_at < published_before)
    if ids is not None:
        query = query.where(Article.id.in_(ids))
    rows = db.execute(query.order_by(Article.id)).all()

    taxonomy_names: dict[int, list[str]] = defaultdict(list)
    if rows:
        for article_id, name in db.execute(
            select(ArticleTaxonomy.article_id, Taxonomy.name)
            .join(Taxonomy, Taxonomy.id == ArticleTaxonomy.taxonomy_id)
            .where(ArticleTaxonomy.article_id.in_([row.id for row in rows]))
            .order_by(ArticleTaxonomy.article_id, Taxonomy.name)
        ):
            taxonomy_names[article_id].append(name)

    articles = {
        row.id: CorpusArticle(
            id=row.id,
            title=row.title,
            content_text=row.content_text,
            content_fingerprint=row.content_fingerprint,
            taxonomy_names=tuple(taxonomy_names[row.id]),
        )
        for row in rows
    }
    return articles, {row.id: row.url for row in rows}


class EvaluationRanker:
    """One site's pool, frozen at the cutoff, reused for every source article."""

    def __init__(
        self,
        *,
        site_id: int,
        model: str,
        pool: dict[int, CorpusArticle],
        sources: dict[int, CorpusArticle],
        excluded_low_value: int = 0,
    ) -> None:
        self.site_id = site_id
        self.model = model
        self.pool = pool
        self.sources = sources
        self.pool_ids = sorted(pool)
        self.duplicate_threshold = settings.suggestion_duplicate_similarity_threshold
        self.terms_by_pool_article = {
            article_id: structured_terms(article) for article_id, article in pool.items()
        }
        self.index = BM25Index(self.terms_by_pool_article) if pool else None
        self.terms_by_source = {
            article_id: structured_terms(article) for article_id, article in sources.items()
        }
        self.stats = PoolStats(
            site_id=site_id,
            pool_articles=len(pool),
            source_articles=len(sources),
            excluded_low_value=excluded_low_value,
        )

    @classmethod
    def load(
        cls,
        db: Session,
        *,
        site_id: int,
        model: str,
        cutoff_at: datetime,
        source_ids: set[int],
    ) -> "EvaluationRanker":
        pool, pool_urls = _load_articles(
            db, site_id=site_id, model=model, published_before=cutoff_at, ids=None
        )
        is_low_value = _low_value_filter()
        low_value_ids = {
            article_id
            for article_id, article in pool.items()
            if is_low_value(article.title, pool_urls[article_id])
        }
        for article_id in low_value_ids:
            del pool[article_id]

        sources, _ = _load_articles(
            db, site_id=site_id, model=model, published_before=None, ids=source_ids
        )
        return cls(
            site_id=site_id,
            model=model,
            pool=pool,
            sources=sources,
            excluded_low_value=len(low_value_ids),
        )

    def _eligible_scores(self, db: Session, source_id: int) -> dict[int, float]:
        """Cosine score for every pool target this source may be shown."""
        source = self.sources[source_id]
        cosine = semantic_scores_for_targets(
            db, article_id=source_id, model=self.model, target_ids=self.pool_ids
        )
        source_title = normalized_title(source.title)
        eligible: dict[int, float] = {}
        for target_id, score in cosine.items():
            if target_id == source_id:
                continue
            if score < settings.suggestion_min_score:
                continue
            # A target this close is the same page, not something worth linking to.
            if score >= self.duplicate_threshold:
                continue
            target = self.pool[target_id]
            if normalized_title(target.title) == source_title:
                continue
            if (
                source.content_fingerprint is not None
                and target.content_fingerprint == source.content_fingerprint
            ):
                continue
            eligible[target_id] = score
        return eligible

    def rank(
        self,
        db: Session,
        *,
        source_id: int,
        limit: int = HYBRID_POOL_SIZE,
        method: RankingMethod = "hybrid",
    ) -> list[int]:
        """Target article ids for one source under one method, best first."""
        return self.rank_all(db, source_id=source_id, limit=limit, methods=(method,))[method]

    def rank_all(
        self,
        db: Session,
        *,
        source_id: int,
        limit: int = HYBRID_POOL_SIZE,
        methods: Sequence[RankingMethod] = RANKING_METHODS,
    ) -> dict[RankingMethod, list[int]]:
        """Rank one source under several methods, reading the embeddings once.

        Every method sees the same eligible targets. That is the point of the
        comparison: a difference between two rows of the table is then a
        difference in ordering, not in which candidates each method was offered.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        unknown = [method for method in methods if method not in RANKING_METHODS]
        if unknown:
            raise ValueError(f"unsupported ranking methods: {unknown}")

        empty: dict[RankingMethod, list[int]] = {method: [] for method in methods}
        if source_id not in self.sources or self.index is None:
            return empty
        eligible = self._eligible_scores(db, source_id)
        if not eligible:
            return empty

        dense_order = [
            target_id
            for target_id, _score in sorted(eligible.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        bm25_scores = self.index.score_documents(
            self.terms_by_source[source_id],
            excluded_ids=set(self.pool) - set(eligible),
        )
        lexical_order = [target_id for target_id, _score in rank_scores(bm25_scores)]

        ranked: dict[RankingMethod, list[int]] = {}
        for method in methods:
            if method == "dense":
                ranked[method] = dense_order[:limit]
            elif method == "lexical":
                ranked[method] = lexical_order[:limit]
            else:
                ranked[method] = self._hybrid_order(
                    dense_order, lexical_order, bm25_scores, limit=limit
                )
        return ranked

    def _hybrid_order(
        self,
        dense_order: Sequence[int],
        lexical_order: Sequence[int],
        bm25_scores: dict[int, float],
        *,
        limit: int,
    ) -> list[int]:
        fused = weighted_rrf_scores(
            dense_order[:DENSE_POOL_SIZE], lexical_order[:LEXICAL_POOL_SIZE]
        )
        fusion_ranks = {target_id: rank for rank, (target_id, _score) in enumerate(fused, start=1)}
        # BM25-512 decides the order and the fused rank breaks ties, exactly as
        # `HybridRanker.rank` does. See the module docstring in app/ml/hybrid.py.
        return sorted(
            fusion_ranks,
            key=lambda target_id: (
                -bm25_scores.get(target_id, 0.0),
                fusion_ranks[target_id],
                target_id,
            ),
        )[:limit]
