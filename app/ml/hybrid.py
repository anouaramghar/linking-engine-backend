"""Global dense + BM25 Hybrid ranking with frozen evaluation parameters.

What this actually does, stated plainly, because the stored components claim it:

* dense cosine retrieves a top-100 pool, structured BM25-512 retrieves another;
* weighted RRF prioritizes the union of the two pools;
* **BM25-512 alone decides the final five.**

The fusion therefore broadens which candidates are considered; it does not
improve the ordering of what is delivered. On both measured corpora, the union
produced no delivered suggestion that dense retrieval contributed alone. See
`docs/design/global-hybrid-ranking.md` for the contract.

Both halves of the union are filtered by one shared SQL predicate
(`app.ml.baseline`), so a lexically-retrieved candidate cannot reach an editor
through a rule that dense retrieval would have applied.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import and_, or_, select, true
from sqlalchemy.orm import Session, aliased

from app.config import settings
from app.ml.baseline import (
    eligible_candidate_scores,
    eligible_top_candidates,
    top_candidates,
)
from app.ml.lexical import BM25Index, rank_scores, tokenize
from app.models import (
    Article,
    ArticleTaxonomy,
    Embedding,
    InternalLink,
    Site,
    Suggestion,
    Taxonomy,
)

DENSE_POOL_SIZE = 100
LEXICAL_POOL_SIZE = 100
CONTENT_TOKEN_LIMIT = 512
TITLE_WEIGHT = 3
TAXONOMY_WEIGHT = 2
LEXICAL_RRF_WEIGHT = 1.0
RRF_RANK_CONSTANT = 10
HYBRID_POOL_SIZE = DENSE_POOL_SIZE + LEXICAL_POOL_SIZE

#: Names carried in the stored score components so a row can be traced back to
#: the exact recipe that produced it.
LEXICAL_RECIPE_NAME = "structured_t3_tax2_c512"


def fusion_name() -> str:
    weight = f"{settings.hybrid_dense_rrf_weight:g}".replace(".", "")
    return f"wrrf_d{weight}_l100_k{RRF_RANK_CONSTANT}"


def max_fusion_score() -> float:
    """The ceiling a weighted-RRF score can reach: first place in both pools.

    Computed rather than frozen because it moves with the configured dense
    weight. A deployment that reweights the fusion must not leave rows already
    normalized against the old ceiling looking stronger than the new ones.
    """
    return (settings.hybrid_dense_rrf_weight + LEXICAL_RRF_WEIGHT) / (RRF_RANK_CONSTANT + 1)


COMPONENTS_VERSION = "hybrid_bm25_v1"

#: Rows ordered by the fusion score carry their own recipe label, so a stored
#: suggestion always says which of the two orderings produced it.
FUSION_COMPONENTS_VERSION = "hybrid_wrrf_v2"


@dataclass(frozen=True)
class CorpusArticle:
    id: int
    title: str
    content_text: str
    content_fingerprint: str | None
    taxonomy_names: tuple[str, ...]


@dataclass(frozen=True)
class RankedCandidate:
    """One suggestion-to-be, with every number that explains its position.

    `semantic_score` is what gets persisted as `Suggestion.score`, so cosine
    similarity keeps the single meaning it has always had wherever that column
    is read. `bm25_score` is reported as it comes out of the ranker rather than
    rescaled into something that looks like a confidence: Okapi BM25 has no
    ceiling, so there is no honest percentage to make of it.

    `normalized_fusion_score` is the exception, and it is what the queue orders
    on. Weighted RRF *is* bounded, so dividing by that bound gives a number
    that means the same thing on every row.
    """

    target_id: int
    semantic_score: float
    bm25_score: float = 0.0
    fusion_rank: int | None = None
    fusion_score: float = 0.0
    dense_rank: int | None = None
    lexical_rank: int | None = None

    @property
    def normalized_fusion_score(self) -> float:
        """This row's fusion score as a 0-1 fraction of the reachable ceiling.

        1.0 means both retrievers put this target first. Callers must only read
        this when the fusion actually decided the order — on a BM25-ordered or
        cosine-ordered row the fusion score is either irrelevant or absent, and
        the fraction would describe a ranking that never happened.
        """
        ceiling = max_fusion_score()
        if ceiling <= 0:
            return 0.0
        return min(1.0, max(0.0, self.fusion_score / ceiling))

    def score_components(self) -> dict:
        return {
            "version": (
                COMPONENTS_VERSION
                if settings.hybrid_final_order == "bm25"
                else FUSION_COMPONENTS_VERSION
            ),
            # Named so a reader never has to infer which number ordered the row.
            "final_order": (
                "wrrf" if settings.hybrid_final_order == "fusion" else "bm25_512"
            ),
            "score_is": "cosine_semantic_similarity",
            "recipe": LEXICAL_RECIPE_NAME,
            "bm25_score": self.bm25_score,
            "fusion": {
                "name": fusion_name(),
                "dense_weight": settings.hybrid_dense_rrf_weight,
                "lexical_weight": LEXICAL_RRF_WEIGHT,
                "rank_constant": RRF_RANK_CONSTANT,
                # The divisor behind `rank_score`, stored so an old row can be
                # re-derived after the weights change rather than re-normalized
                # against whatever the ceiling happens to be at read time.
                "ceiling": max_fusion_score(),
            },
            "fusion_rank": self.fusion_rank,
            "fusion_score": self.fusion_score,
            "normalized_fusion_score": self.normalized_fusion_score,
            # None means the other retriever never proposed this target.
            "dense_rank": self.dense_rank,
            "lexical_rank": self.lexical_rank,
            "semantic": self.semantic_score,
        }


@dataclass(frozen=True)
class HybridRanking:
    candidates: tuple[RankedCandidate, ...]
    baseline_candidates: tuple[RankedCandidate, ...]
    dense_count: int
    lexical_count: int
    union_count: int


def normalized_title(title: str) -> str:
    """Match SQL's `lower(btrim(title))` exactly.

    Deliberately *not* collapsing internal whitespace. Doing so would make this
    in-memory pre-filter stricter than the database rule, and the two must agree
    or the pre-filter silently drops candidates the authoritative predicate
    would have allowed.
    """
    return title.lower().strip()


def structured_terms(article: CorpusArticle) -> list[str]:
    """Reproduce the frozen BM25-512 recipe selected by the offline evaluation."""
    if len(article.content_text) > settings.crawl_max_article_chars:
        raise ValueError(f"article content exceeded {settings.crawl_max_article_chars} characters")
    title_terms = tokenize(article.title)
    taxonomy_terms = [
        term for taxonomy_name in article.taxonomy_names for term in tokenize(taxonomy_name)
    ]
    content_terms = tokenize(article.content_text)[:CONTENT_TOKEN_LIMIT]
    return title_terms * TITLE_WEIGHT + taxonomy_terms * TAXONOMY_WEIGHT + content_terms


def weighted_rrf_scores(
    dense_ranking: Sequence[int],
    lexical_ranking: Sequence[int],
    *,
    limit: int = HYBRID_POOL_SIZE,
) -> list[tuple[int, float]]:
    """Prioritize the candidate union with the frozen lexical-heavy RRF recipe."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    scores: dict[int, float] = defaultdict(float)
    best_rank: dict[int, int] = {}
    for ranking, weight in (
        (dense_ranking, settings.hybrid_dense_rrf_weight),
        (lexical_ranking, LEXICAL_RRF_WEIGHT),
    ):
        for rank, article_id in enumerate(ranking, start=1):
            scores[article_id] += weight / (RRF_RANK_CONSTANT + rank)
            best_rank[article_id] = min(best_rank.get(article_id, rank), rank)
    ordered = sorted(
        scores,
        key=lambda article_id: (
            -scores[article_id],
            best_rank[article_id],
            article_id,
        ),
    )[:limit]
    return [(article_id, scores[article_id]) for article_id in ordered]


def weighted_reciprocal_rank_fusion(
    dense_ranking: Sequence[int],
    lexical_ranking: Sequence[int],
    *,
    limit: int = HYBRID_POOL_SIZE,
) -> list[int]:
    """The fused order alone, for callers that do not need the scores."""
    return [
        article_id
        for article_id, _score in weighted_rrf_scores(dense_ranking, lexical_ranking, limit=limit)
    ]


def rank_hybrid_candidates(
    dense_ids: Sequence[int],
    lexical_ids: Sequence[int],
    bm25_scores: Mapping[int, float],
    semantic_scores: Mapping[int, float],
    *,
    limit: int,
) -> tuple[RankedCandidate, ...]:
    """Return the production Hybrid order with its complete rank evidence.

    The candidate pools may be larger than the frozen retrieval pools. The
    bounded slices and the final BM25-first ordering live here so production
    analysis and offline evaluation cannot silently diverge.
    """

    if limit <= 0:
        raise ValueError("limit must be positive")
    dense_pool = list(dense_ids[:DENSE_POOL_SIZE])
    lexical_pool = list(lexical_ids[:LEXICAL_POOL_SIZE])
    fused = weighted_rrf_scores(dense_pool, lexical_pool)
    fusion_scores = dict(fused)
    fusion_ranks = {target_id: rank for rank, (target_id, _score) in enumerate(fused, start=1)}
    dense_ranks = {target_id: rank for rank, target_id in enumerate(dense_pool, start=1)}
    lexical_ranks = {target_id: rank for rank, target_id in enumerate(lexical_pool, start=1)}

    missing_scores = [target_id for target_id in fusion_ranks if target_id not in semantic_scores]
    if missing_scores:
        raise ValueError(
            f"Hybrid ranking is missing semantic scores for target ids {missing_scores[:5]}"
        )
    # `fusion_ranks` is already the fusion order, so ordering by it delivers the
    # combined signal. The BM25 branch is the previous behavior, kept so the
    # change can be reversed by configuration rather than by a deployment.
    if settings.hybrid_final_order == "fusion":
        final_ids = sorted(
            fusion_ranks,
            key=lambda target_id: (fusion_ranks[target_id], target_id),
        )[:limit]
    else:
        final_ids = sorted(
            fusion_ranks,
            key=lambda target_id: (
                -bm25_scores.get(target_id, 0.0),
                fusion_ranks[target_id],
                target_id,
            ),
        )[:limit]
    return tuple(
        RankedCandidate(
            target_id=target_id,
            semantic_score=min(1.0, max(0.0, semantic_scores[target_id])),
            bm25_score=bm25_scores.get(target_id, 0.0),
            fusion_rank=fusion_ranks[target_id],
            fusion_score=fusion_scores[target_id],
            dense_rank=dense_ranks.get(target_id),
            lexical_rank=lexical_ranks.get(target_id),
        )
        for target_id in final_ids
    )


class HybridRanker:
    """One immutable site snapshot reused for every source in an analysis run."""

    def __init__(
        self,
        *,
        articles: dict[int, CorpusArticle],
        blocked_targets: dict[int, set[int]],
        allowed_target_ids: set[int] | None = None,
    ) -> None:
        if not articles:
            raise ValueError("hybrid ranking requires at least one active embedded article")
        self.articles = articles
        self.blocked_targets = blocked_targets
        self.allowed_target_ids = (
            tuple(sorted(allowed_target_ids)) if allowed_target_ids is not None else None
        )
        self.terms_by_article = {
            article_id: structured_terms(article) for article_id, article in articles.items()
        }
        self.index = BM25Index(self.terms_by_article)

        title_groups: dict[str, set[int]] = defaultdict(set)
        fingerprint_groups: dict[str, set[int]] = defaultdict(set)
        for article in articles.values():
            title_groups[normalized_title(article.title)].add(article.id)
            if article.content_fingerprint:
                fingerprint_groups[article.content_fingerprint].add(article.id)
        self.title_groups = dict(title_groups)
        self.fingerprint_groups = dict(fingerprint_groups)

    @classmethod
    def load(
        cls,
        db: Session,
        *,
        site_id: int,
        model: str,
        allowed_target_ids: set[int] | None = None,
    ) -> "HybridRanker":
        article_rows = list(
            db.execute(
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
                .join(Site, Site.id == Article.site_id)
                .where(
                    or_(
                        Article.site_id == site_id,
                        and_(
                            Site.platform == "pool",
                            Site.pool_source_approved.is_(True),
                            Site.pool_source_quarantined.is_(False),
                        ),
                    ),
                    Article.is_active.is_(True),
                    Article.id.in_(allowed_target_ids)
                    if allowed_target_ids is not None
                    else true(),
                )
                .order_by(Article.id)
                .limit(settings.analysis_max_corpus_articles + 1)
            )
        )
        if len(article_rows) > settings.analysis_max_corpus_articles:
            raise ValueError(
                f"analysis corpus exceeded {settings.analysis_max_corpus_articles} articles"
            )
        article_ids = {row.id for row in article_rows}

        taxonomy_by_article: dict[int, list[str]] = defaultdict(list)
        for article_id, taxonomy_name in db.execute(
            select(ArticleTaxonomy.article_id, Taxonomy.name)
            .join(Taxonomy, Taxonomy.id == ArticleTaxonomy.taxonomy_id)
            .join(Article, Article.id == ArticleTaxonomy.article_id)
            .join(Site, Site.id == Article.site_id)
            .where(
                or_(Article.site_id == site_id, Site.platform == "pool"),
                Article.is_active.is_(True),
                Article.id.in_(article_ids),
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
            for row in article_rows
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
        return cls(
            articles=articles,
            blocked_targets=dict(blocked_targets),
            allowed_target_ids=allowed_target_ids,
        )

    def _duplicate_ids(self, source_id: int) -> set[int]:
        article = self.articles[source_id]
        duplicates = set(self.title_groups.get(normalized_title(article.title), set()))
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
        duplicate_similarity_threshold: float,
        include_baseline: bool = False,
    ) -> HybridRanking:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if source_id not in self.articles:
            return HybridRanking((), (), 0, 0, 0)

        # Dense half: the SQL predicate has already applied every pilot rule.
        dense_rows = eligible_top_candidates(
            db,
            source_id,
            model,
            DENSE_POOL_SIZE,
            duplicate_similarity_threshold,
            **(
                {"allowed_target_ids": self.allowed_target_ids}
                if self.allowed_target_ids is not None
                else {}
            ),
        )
        dense_ids = [target_id for target_id, _score in dense_rows]
        semantic_scores = dict(dense_rows)

        # Lexical half: the in-memory exclusions are only a pre-filter, so the
        # pool is not spent on candidates already known to be ineligible. The
        # rules that BM25 cannot see are enforced by the re-check below.
        excluded_ids = (
            self.blocked_targets.get(source_id, set())
            | self._duplicate_ids(source_id)
            | {source_id}
        )
        query_terms = self.terms_by_article[source_id]
        bm25_scores = self.index.score_documents(query_terms, excluded_ids=excluded_ids)
        ranked_lexical_ids = [target_id for target_id, _score in rank_scores(bm25_scores)]

        # Build the lexical top-100 *after* authoritative eligibility. Checking
        # only the raw first 100 would let rejected candidates consume the pool:
        # an eligible BM25 rank 101 could disappear even when it should be the
        # highest-ranked eligible lexical result. Walk the ranking in bounded
        # pages until the pool is full or the scored corpus is exhausted.
        lexical_ids: list[int] = []
        for page_start in range(0, len(ranked_lexical_ids), LEXICAL_POOL_SIZE):
            if len(lexical_ids) >= LEXICAL_POOL_SIZE:
                break
            page_ids = ranked_lexical_ids[page_start : page_start + LEXICAL_POOL_SIZE]
            lexical_only_ids = [
                target_id for target_id in page_ids if target_id not in semantic_scores
            ]
            semantic_scores.update(
                eligible_candidate_scores(
                    db,
                    source_id,
                    model,
                    lexical_only_ids,
                    duplicate_similarity_threshold,
                    **(
                        {"allowed_target_ids": self.allowed_target_ids}
                        if self.allowed_target_ids is not None
                        else {}
                    ),
                )
            )
            lexical_ids.extend(target_id for target_id in page_ids if target_id in semantic_scores)
        lexical_ids = lexical_ids[:LEXICAL_POOL_SIZE]

        candidates = rank_hybrid_candidates(
            dense_ids,
            lexical_ids,
            bm25_scores,
            semantic_scores,
            limit=limit,
        )

        # The shadow comparison must be against what the baseline path would
        # really have written, which is the untouched baseline query — not the
        # Hybrid's stricter dense pool.
        baseline_candidates: tuple[RankedCandidate, ...] = ()
        if include_baseline:
            baseline_candidates = tuple(
                RankedCandidate(
                    target_id=target_id,
                    semantic_score=min(1.0, max(0.0, semantic_score)),
                )
                for target_id, semantic_score in top_candidates(
                    db,
                    source_id,
                    model,
                    DENSE_POOL_SIZE,
                    **(
                        {"allowed_target_ids": self.allowed_target_ids}
                        if self.allowed_target_ids is not None
                        else {}
                    ),
                )
            )
        return HybridRanking(
            candidates=candidates,
            baseline_candidates=baseline_candidates,
            dense_count=len(dense_ids),
            lexical_count=len(lexical_ids),
            union_count=len(set(dense_ids) | set(lexical_ids)),
        )
