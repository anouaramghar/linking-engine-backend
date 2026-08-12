"""Ranking quality metrics for internal-link suggestions.

These functions are deliberately free of database and model code. They take a
ranked list of target article ids and the set of targets a human actually linked,
and return numbers. That keeps them cheap to test and identical for every
suggestion method being compared.

One evaluation query is one source article: rank the candidate targets for it,
then measure where the true targets landed.

    recall@k  did the true target appear in the first k? Position is ignored.
    ndcg@k    are the true targets near the top? Position is discounted.
    mrr       how early is the first true target?

Report all three. High recall with low NDCG means retrieval works and ranking
does not. Low recall means the ranker never saw the right article at all.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from math import log2, sqrt
from statistics import fmean, stdev


# Only meaningful for a mean over bounded per-query scores, which is what these are.
_Z_95 = 1.959963985


def _validate(ranked: Sequence[int], relevant: Collection[int], k: int) -> None:
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    if not relevant:
        raise ValueError("relevant must not be empty; an unscorable query cannot be scored")
    # A repeated candidate would be counted as two hits and inflate every metric.
    if len(set(ranked)) != len(ranked):
        raise ValueError("ranked must not contain duplicate article ids")


def recall_at_k(ranked: Sequence[int], relevant: Collection[int], k: int) -> float:
    """Fraction of the true targets that appear in the first k positions."""
    _validate(ranked, relevant, k)
    relevant = set(relevant)
    found = sum(1 for article_id in ranked[:k] if article_id in relevant)
    return found / len(relevant)


def ndcg_at_k(ranked: Sequence[int], relevant: Collection[int], k: int) -> float:
    """Position-discounted score in [0, 1]; 1.0 means every true target is on top.

    Relevance is binary here: an internal link either exists or it does not, so
    every true target contributes the same gain before the position discount.
    """
    _validate(ranked, relevant, k)
    relevant = set(relevant)
    # Rank 1 is the first position, and log2(1 + 1) == 1, so a top hit scores 1.0.
    gain = sum(
        1 / log2(rank + 1)
        for rank, article_id in enumerate(ranked[:k], start=1)
        if article_id in relevant
    )
    # The best reachable ordering puts as many true targets as will fit at the top.
    ideal = sum(1 / log2(rank + 1) for rank in range(1, min(k, len(relevant)) + 1))
    return gain / ideal


def reciprocal_rank(
    ranked: Sequence[int], relevant: Collection[int], k: int | None = None
) -> float:
    """1 / rank of the first true target, or 0.0 when none is found."""
    _validate(ranked, relevant, k if k is not None else 1)
    relevant = set(relevant)
    considered = ranked if k is None else ranked[:k]
    for rank, article_id in enumerate(considered, start=1):
        if article_id in relevant:
            return 1 / rank
    return 0.0


@dataclass(frozen=True)
class QueryScore:
    """The three metrics for one source article."""

    source_article_id: int
    recall: float
    ndcg: float
    reciprocal_rank: float


@dataclass(frozen=True)
class MetricSummary:
    k: int
    # Number of source articles actually scored. Never quote a metric without it:
    # on a few dozen queries a 5% difference is indistinguishable from noise.
    queries: int
    # Source articles that had no true target, so nothing could be measured.
    skipped_without_relevant: int
    recall_at_k: float
    ndcg_at_k: float
    mrr: float
    # 95% intervals for the two headline means. If two methods' intervals overlap,
    # the difference between them is not established by this test set.
    recall_ci95: tuple[float, float]
    ndcg_ci95: tuple[float, float]

    def to_dict(self) -> dict:
        return {
            "k": self.k,
            "queries": self.queries,
            "skipped_without_relevant": self.skipped_without_relevant,
            "recall_at_k": self.recall_at_k,
            "ndcg_at_k": self.ndcg_at_k,
            "mrr": self.mrr,
            "recall_ci95": list(self.recall_ci95),
            "ndcg_ci95": list(self.ndcg_ci95),
        }


def _mean_ci95(values: Sequence[float]) -> tuple[float, float]:
    """Normal-approximation interval for the mean. Deterministic, so runs compare."""
    if len(values) < 2:
        return (0.0, 1.0)
    margin = _Z_95 * stdev(values) / sqrt(len(values))
    mean = fmean(values)
    # The per-query scores are bounded, so the interval is too.
    return (max(0.0, mean - margin), min(1.0, mean + margin))


def summarize(
    scores: Sequence[QueryScore], *, k: int, skipped_without_relevant: int = 0
) -> MetricSummary:
    """Average per-query scores into one reportable result."""
    if not scores:
        return MetricSummary(
            k=k,
            queries=0,
            skipped_without_relevant=skipped_without_relevant,
            recall_at_k=0.0,
            ndcg_at_k=0.0,
            mrr=0.0,
            recall_ci95=(0.0, 1.0),
            ndcg_ci95=(0.0, 1.0),
        )
    recalls = [score.recall for score in scores]
    ndcgs = [score.ndcg for score in scores]
    return MetricSummary(
        k=k,
        queries=len(scores),
        skipped_without_relevant=skipped_without_relevant,
        recall_at_k=fmean(recalls),
        ndcg_at_k=fmean(ndcgs),
        mrr=fmean([score.reciprocal_rank for score in scores]),
        recall_ci95=_mean_ci95(recalls),
        ndcg_ci95=_mean_ci95(ndcgs),
    )


def evaluate_rankings(
    rankings: Mapping[int, Sequence[int]],
    relevant_by_source: Mapping[int, Collection[int]],
    *,
    k: int,
) -> MetricSummary:
    """Score every source article that has at least one true target.

    ``rankings`` maps a source article id to the target article ids a suggestion
    method proposed, best first. ``relevant_by_source`` maps it to the targets a
    human linked, which the temporal split supplies. A source article with no true
    target is not a failure, it is unmeasurable, so it is counted and left out.

    A source article that is missing from ``rankings`` scores zero rather than
    being skipped: the method was asked for suggestions and returned none.
    """
    scores: list[QueryScore] = []
    skipped = 0
    for source_article_id, relevant in relevant_by_source.items():
        if not relevant:
            skipped += 1
            continue
        ranked = rankings.get(source_article_id, ())
        scores.append(
            QueryScore(
                source_article_id=source_article_id,
                recall=recall_at_k(ranked, relevant, k),
                ndcg=ndcg_at_k(ranked, relevant, k),
                reciprocal_rank=reciprocal_rank(ranked, relevant),
            )
        )
    scores.sort(key=lambda score: score.source_article_id)
    return summarize(scores, k=k, skipped_without_relevant=skipped)
