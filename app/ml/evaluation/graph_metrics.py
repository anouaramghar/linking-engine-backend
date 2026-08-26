"""Graph-aware evaluation over a frozen, as-of-cutoff article graph.

The public seam is intentionally small: build one graph from the split's training
edges, then evaluate ranked target lists by simulating their top-K edges. The
implementation reuses the production graph computation and simulation rules, so
offline structural outcomes cannot quietly disagree with the review preview.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.services.graph_service import (
    GraphComputation,
    GraphCounts,
    GraphNode,
    compute_graph,
    simulate_graph,
)
from app.models import Article


@dataclass(frozen=True)
class StructuralSummary:
    """Structural result of simulating one method's top-K recommendations."""

    k: int
    queries: int
    proposed_edges: int
    applied_edges: int
    duplicate_edges: int
    before: GraphCounts
    after: GraphCounts
    newly_connected_count: int
    newly_saturated_count: int
    target_concentration: float
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "k": self.k,
            "queries": self.queries,
            "proposed_edges": self.proposed_edges,
            "applied_edges": self.applied_edges,
            "duplicate_edges": self.duplicate_edges,
            "before": _counts_to_dict(self.before),
            "after": _counts_to_dict(self.after),
            "delta": {
                "orphan_count": self.after.orphan_count - self.before.orphan_count,
                "underlinked_count": self.after.underlinked_count - self.before.underlinked_count,
                "saturated_count": self.after.saturated_count - self.before.saturated_count,
                "active_links": self.after.active_links - self.before.active_links,
            },
            "newly_connected_count": self.newly_connected_count,
            "newly_connected_rate": (
                self.newly_connected_count / self.before.orphan_count
                if self.before.orphan_count
                else 0.0
            ),
            "newly_saturated_count": self.newly_saturated_count,
            "target_concentration": self.target_concentration,
            "warnings": list(self.warnings),
        }


def build_as_of_graph(
    db: Session,
    *,
    site_id: int,
    cutoff_at: datetime,
    source_ids: Collection[int],
    train_edges: Collection[tuple[int, int]],
) -> GraphComputation:
    """Build the evaluation graph without using links from the frozen test set.

    Nodes published before the cutoff form the candidate pool. Measured post-cutoff
    source articles are added so proposed edges can be simulated. Only the caller's
    frozen training edges enter the graph; current database links are deliberately
    ignored to prevent future-state leakage.
    """

    if cutoff_at.tzinfo is None or cutoff_at.utcoffset() is None:
        raise ValueError("cutoff_at must include a timezone")

    source_ids = set(source_ids)
    node_filter = Article.published_at < cutoff_at
    if source_ids:
        node_filter = or_(node_filter, Article.id.in_(source_ids))
    rows = db.execute(
        select(Article.id, Article.url, Article.title)
        .where(
            Article.site_id == site_id,
            Article.is_active.is_(True),
            node_filter,
        )
        .order_by(Article.id)
    ).all()
    nodes = [GraphNode(article_id, url, title) for article_id, url, title in rows]
    return compute_graph(nodes, sorted(set(train_edges)))


def evaluate_structural_outcomes(
    computation: GraphComputation,
    rankings: Mapping[int, Sequence[int]],
    *,
    k: int,
) -> StructuralSummary:
    """Simulate each ranking's top-K source-to-target edges as one batch."""

    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")

    proposed_edges = [
        (source_id, target_id)
        for source_id in sorted(rankings)
        for target_id in rankings[source_id][:k]
    ]
    simulation = simulate_graph(computation, proposed_edges)
    return StructuralSummary(
        k=k,
        queries=len(rankings),
        proposed_edges=len(proposed_edges),
        applied_edges=len(simulation.applied_edges),
        duplicate_edges=len(simulation.duplicate_edges),
        before=simulation.before,
        after=simulation.after,
        newly_connected_count=len(simulation.newly_connected_article_ids),
        newly_saturated_count=len(simulation.newly_saturated_article_ids),
        target_concentration=simulation.target_concentration,
        warnings=simulation.warnings,
    )


def aggregate_structural_outcomes(
    summaries: Sequence[StructuralSummary],
) -> StructuralSummary:
    """Combine independent site summaries for a fleet-level report.

    Counts are additive across sites. Target concentration is the maximum site
    concentration, which preserves the safety meaning rather than mixing unrelated
    article IDs from different sites.
    """

    if not summaries:
        raise ValueError("summaries must not be empty")
    first = summaries[0]
    if any(summary.k != first.k for summary in summaries):
        raise ValueError("all structural summaries must use the same k")

    warnings = tuple(
        dict.fromkeys(warning for summary in summaries for warning in summary.warnings)
    )
    return StructuralSummary(
        k=first.k,
        queries=sum(summary.queries for summary in summaries),
        proposed_edges=sum(summary.proposed_edges for summary in summaries),
        applied_edges=sum(summary.applied_edges for summary in summaries),
        duplicate_edges=sum(summary.duplicate_edges for summary in summaries),
        before=_sum_counts(summary.before for summary in summaries),
        after=_sum_counts(summary.after for summary in summaries),
        newly_connected_count=sum(summary.newly_connected_count for summary in summaries),
        newly_saturated_count=sum(summary.newly_saturated_count for summary in summaries),
        target_concentration=max(summary.target_concentration for summary in summaries),
        warnings=warnings,
    )


def _counts_to_dict(counts: GraphCounts) -> dict:
    return {
        "active_articles": counts.active_articles,
        "active_links": counts.active_links,
        "orphan_count": counts.orphan_count,
        "underlinked_count": counts.underlinked_count,
        "hub_count": counts.hub_count,
        "saturated_count": counts.saturated_count,
        "max_in_degree": counts.max_in_degree,
        "max_out_degree": counts.max_out_degree,
    }


def _sum_counts(counts: Sequence[GraphCounts]) -> GraphCounts:
    counts = tuple(counts)
    return GraphCounts(
        active_articles=sum(item.active_articles for item in counts),
        active_links=sum(item.active_links for item in counts),
        orphan_count=sum(item.orphan_count for item in counts),
        underlinked_count=sum(item.underlinked_count for item in counts),
        hub_count=sum(item.hub_count for item in counts),
        saturated_count=sum(item.saturated_count for item in counts),
        max_in_degree=max(item.max_in_degree for item in counts),
        max_out_degree=max(item.max_out_degree for item in counts),
    )
