"""Site-scoped dense + BM25 pilot ranking with frozen evaluation parameters."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.ml.baseline import semantic_scores_for_targets, top_candidates
from app.ml.lexical import BM25Index, tokenize
from app.models import (
    Article,
    ArticleTaxonomy,
    Embedding,
    InternalLink,
    Suggestion,
    Taxonomy,
)

DENSE_POOL_SIZE = 100
LEXICAL_POOL_SIZE = 100
CONTENT_TOKEN_LIMIT = 512
TITLE_WEIGHT = 3
TAXONOMY_WEIGHT = 2
DENSE_RRF_WEIGHT = 0.25
LEXICAL_RRF_WEIGHT = 1.0
RRF_RANK_CONSTANT = 10
HYBRID_POOL_SIZE = DENSE_POOL_SIZE + LEXICAL_POOL_SIZE


@dataclass(frozen=True)
class CorpusArticle:
    id: int
    title: str
    content_text: str
    content_fingerprint: str | None
    taxonomy_names: tuple[str, ...]


@dataclass(frozen=True)
class RankedCandidate:
    target_id: int
    semantic_score: float


@dataclass(frozen=True)
class HybridRanking:
    candidates: tuple[RankedCandidate, ...]
    baseline_candidates: tuple[RankedCandidate, ...]
    dense_count: int
    lexical_count: int
    union_count: int


def structured_terms(article: CorpusArticle) -> list[str]:
    """Reproduce the frozen BM25-512 recipe selected by the offline evaluation."""
    title_terms = tokenize(article.title)
    taxonomy_terms = [
        term for taxonomy_name in article.taxonomy_names for term in tokenize(taxonomy_name)
    ]
    content_terms = tokenize(article.content_text)[:CONTENT_TOKEN_LIMIT]
    return title_terms * TITLE_WEIGHT + taxonomy_terms * TAXONOMY_WEIGHT + content_terms


def weighted_reciprocal_rank_fusion(
    dense_ranking: Sequence[int],
    lexical_ranking: Sequence[int],
    *,
    limit: int = HYBRID_POOL_SIZE,
) -> list[int]:
    """Prioritize the candidate union with the frozen lexical-heavy RRF recipe."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    scores: dict[int, float] = defaultdict(float)
    best_rank: dict[int, int] = {}
    for ranking, weight in (
        (dense_ranking, DENSE_RRF_WEIGHT),
        (lexical_ranking, LEXICAL_RRF_WEIGHT),
    ):
        for rank, article_id in enumerate(ranking, start=1):
            scores[article_id] += weight / (RRF_RANK_CONSTANT + rank)
            best_rank[article_id] = min(best_rank.get(article_id, rank), rank)
    return sorted(
        scores,
        key=lambda article_id: (
            -scores[article_id],
            best_rank[article_id],
            article_id,
        ),
    )[:limit]


class HybridRanker:
    """One immutable site snapshot reused for every source in an analysis run."""

    def __init__(
        self,
        *,
        articles: dict[int, CorpusArticle],
        blocked_targets: dict[int, set[int]],
    ) -> None:
        if not articles:
            raise ValueError("hybrid ranking requires at least one active embedded article")
        self.articles = articles
        self.blocked_targets = blocked_targets
        self.terms_by_article = {
            article_id: structured_terms(article) for article_id, article in articles.items()
        }
        self.index = BM25Index(self.terms_by_article)

        title_groups: dict[str, set[int]] = defaultdict(set)
        fingerprint_groups: dict[str, set[int]] = defaultdict(set)
        for article in articles.values():
            title_groups[" ".join(article.title.lower().split())].add(article.id)
            if article.content_fingerprint:
                fingerprint_groups[article.content_fingerprint].add(article.id)
        self.title_groups = dict(title_groups)
        self.fingerprint_groups = dict(fingerprint_groups)

    @classmethod
    def load(cls, db: Session, *, site_id: int, model: str) -> "HybridRanker":
        taxonomy_by_article: dict[int, list[str]] = defaultdict(list)
        for article_id, taxonomy_name in db.execute(
            select(ArticleTaxonomy.article_id, Taxonomy.name)
            .join(Taxonomy, Taxonomy.id == ArticleTaxonomy.taxonomy_id)
            .join(Article, Article.id == ArticleTaxonomy.article_id)
            .where(
                Article.site_id == site_id,
                Article.is_active.is_(True),
            )
            .order_by(ArticleTaxonomy.article_id, Taxonomy.name)
        ):
            taxonomy_by_article[article_id].append(taxonomy_name)

        articles = {
            row.id: CorpusArticle(
                id=row.id,
                title=row.title,
                content_text=row.content_text,
                content_fingerprint=row.content_fingerprint,
                taxonomy_names=tuple(taxonomy_by_article[row.id]),
            )
            for row in db.execute(
                select(
                    Article.id,
                    Article.title,
                    Article.content_text,
                    Embedding.content_fingerprint,
                )
                .join(
                    Embedding,
                    (Embedding.article_id == Article.id) & (Embedding.model == model),
                )
                .where(
                    Article.site_id == site_id,
                    Article.is_active.is_(True),
                )
                .order_by(Article.id)
            )
        }

        blocked_targets: dict[int, set[int]] = defaultdict(set)
        source_article = aliased(Article)
        for source_id, target_id in db.execute(
            select(InternalLink.source_article_id, InternalLink.target_article_id)
            .join(source_article, source_article.id == InternalLink.source_article_id)
            .where(
                source_article.site_id == site_id,
                InternalLink.is_active.is_(True),
            )
        ):
            blocked_targets[source_id].add(target_id)
        for source_id, target_id in db.execute(
            select(Suggestion.source_article_id, Suggestion.target_article_id).where(
                Suggestion.site_id == site_id,
                Suggestion.status != "expired",
            )
        ):
            blocked_targets[source_id].add(target_id)
        return cls(articles=articles, blocked_targets=dict(blocked_targets))

    def _duplicate_ids(self, source_id: int) -> set[int]:
        article = self.articles[source_id]
        duplicates = set(self.title_groups.get(" ".join(article.title.lower().split()), set()))
        if article.content_fingerprint:
            duplicates.update(self.fingerprint_groups.get(article.content_fingerprint, set()))
        duplicates.discard(source_id)
        return duplicates

    def rank(
        self,
        db: Session,
        *,
        source_id: int,
        model: str,
        limit: int,
    ) -> HybridRanking:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if source_id not in self.articles:
            return HybridRanking((), (), 0, 0, 0)

        baseline_rows = top_candidates(
            db,
            source_id,
            model,
            DENSE_POOL_SIZE,
        )
        duplicate_ids = self._duplicate_ids(source_id)
        dense_rows = [row for row in baseline_rows if row[0] not in duplicate_ids]
        dense_ids = [target_id for target_id, _score in dense_rows]
        excluded_ids = self.blocked_targets.get(source_id, set()) | duplicate_ids | {source_id}
        lexical_rows = self.index.rank(
            self.terms_by_article[source_id],
            limit=LEXICAL_POOL_SIZE,
            excluded_ids=excluded_ids,
        )
        lexical_ids = [target_id for target_id, _score in lexical_rows]
        lexical_scores = dict(lexical_rows)

        prioritized_ids = weighted_reciprocal_rank_fusion(
            dense_ids,
            lexical_ids,
        )
        fusion_rank = {target_id: rank for rank, target_id in enumerate(prioritized_ids)}
        final_ids = sorted(
            prioritized_ids,
            key=lambda target_id: (
                -lexical_scores.get(target_id, 0.0),
                fusion_rank[target_id],
                target_id,
            ),
        )[:limit]
        semantic_scores = semantic_scores_for_targets(
            db,
            article_id=source_id,
            model=model,
            target_ids=final_ids,
        )
        candidates = tuple(
            RankedCandidate(
                target_id=target_id,
                semantic_score=min(1.0, max(0.0, semantic_scores[target_id])),
            )
            for target_id in final_ids
        )
        baseline_candidates = tuple(
            RankedCandidate(
                target_id=target_id,
                semantic_score=min(1.0, max(0.0, semantic_score)),
            )
            for target_id, semantic_score in baseline_rows
        )
        return HybridRanking(
            candidates=candidates,
            baseline_candidates=baseline_candidates,
            dense_count=len(dense_ids),
            lexical_count=len(lexical_ids),
            union_count=len(set(dense_ids) | set(lexical_ids)),
        )
