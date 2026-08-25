"""Deterministic graph intelligence behind a small, reusable interface.

The module has two seams:

* ``ensure_graph_snapshot`` materializes the accepted active graph into an
  immutable, versioned observation;
* ``deterministic_rerank`` and ``simulate_graph`` are pure computations over
  that observation.

The ranking implementation is deliberately bounded. Structural opportunity can
move a sufficiently relevant candidate, but its adjustment is capped and cannot
make a weak candidate outrank a materially more relevant one merely because it
is an orphan.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Sequence, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.config import settings
from app.models import (
    Article,
    GraphFeature as GraphFeatureRow,
    GraphSnapshot,
    IngestionRun,
    InternalLink,
)

_GRAPH_LOCK_NAMESPACE = 0x4C47  # "LG"
CandidateT = TypeVar("CandidateT")


@dataclass(frozen=True)
class GraphThresholds:
    underlinked_max_in_degree: int
    hub_min_out_degree: int
    saturation_min_in_degree: int


@dataclass(frozen=True)
class GraphNode:
    article_id: int
    url: str
    title: str


@dataclass(frozen=True)
class GraphFeatureData:
    article_id: int
    article_url: str
    article_title: str
    in_degree: int
    out_degree: int
    orphan: bool
    underlinked: bool
    hub: bool
    saturated: bool
    hub_score: float
    saturation_score: float


@dataclass(frozen=True)
class GraphComputation:
    features: tuple[GraphFeatureData, ...]
    edges: tuple[tuple[int, int], ...]

    @property
    def article_count(self) -> int:
        return len(self.features)

    @property
    def edge_count(self) -> int:
        return len(self.edges)


@dataclass(frozen=True)
class GraphRankMetadata:
    target_in_degree: int | None
    source_out_degree: int | None
    target_orphan: bool
    target_underlinked: bool
    source_hub: bool
    source_saturated: bool
    opportunity: float
    adjustment: float
    baseline_rank: int
    final_rank: int
    applied: bool


@dataclass(frozen=True)
class GraphCounts:
    active_articles: int
    active_links: int
    orphan_count: int
    underlinked_count: int
    hub_count: int
    saturated_count: int
    max_in_degree: int
    max_out_degree: int


@dataclass(frozen=True)
class GraphSimulation:
    before: GraphCounts
    after: GraphCounts
    applied_edges: tuple[tuple[int, int], ...]
    duplicate_edges: tuple[tuple[int, int], ...]
    newly_connected_article_ids: tuple[int, ...]
    newly_saturated_article_ids: tuple[int, ...]
    target_concentration: float
    warnings: tuple[str, ...]


def graph_thresholds() -> GraphThresholds:
    return GraphThresholds(
        underlinked_max_in_degree=settings.graph_underlinked_max_in_degree,
        hub_min_out_degree=settings.graph_hub_min_out_degree,
        saturation_min_in_degree=settings.graph_saturation_min_in_degree,
    )


def compute_graph(
    nodes: Sequence[GraphNode],
    edges: Sequence[tuple[int, int]],
    *,
    thresholds: GraphThresholds | None = None,
) -> GraphComputation:
    """Compute structural features from an ordered node set and directed edges.

    Unknown endpoints and self-links are ignored. Duplicate edges are counted
    once, matching the database's unique source/target constraint and making a
    simulation safe to call with repeated suggestions.
    """

    thresholds = thresholds or graph_thresholds()
    node_ids = {node.article_id for node in nodes}
    unique_edges = sorted(
        {
            (source_id, target_id)
            for source_id, target_id in edges
            if source_id in node_ids and target_id in node_ids and source_id != target_id
        }
    )
    in_degree: Counter[int] = Counter()
    out_degree: Counter[int] = Counter()
    for source_id, target_id in unique_edges:
        out_degree[source_id] += 1
        in_degree[target_id] += 1

    denominator = max(1, len(node_ids) - 1)
    by_id = {node.article_id: node for node in nodes}
    features = []
    for node in sorted(nodes, key=lambda item: item.article_id):
        incoming = in_degree[node.article_id]
        outgoing = out_degree[node.article_id]
        features.append(
            GraphFeatureData(
                article_id=node.article_id,
                article_url=node.url,
                article_title=node.title,
                in_degree=incoming,
                out_degree=outgoing,
                orphan=incoming == 0,
                underlinked=incoming <= thresholds.underlinked_max_in_degree,
                hub=outgoing >= thresholds.hub_min_out_degree,
                saturated=incoming >= thresholds.saturation_min_in_degree,
                hub_score=round(outgoing / denominator, 6),
                saturation_score=round(incoming / denominator, 6),
            )
        )

    # Keep this check close to the computation: a feature row without a
    # corresponding node would make simulation and review disagree silently.
    # It is a raise rather than an assertion because `python -O` strips
    # assertions, which would remove the one guard that catches that.
    if set(by_id) != {feature.article_id for feature in features}:
        raise RuntimeError("graph features and nodes disagree on article ids")
    return GraphComputation(tuple(features), tuple(unique_edges))


def _graph_digest(
    nodes: Sequence[GraphNode],
    edges: Sequence[tuple[int, int]],
    *,
    source_ingestion_run_id: int | None,
) -> str:
    payload = {
        "algorithm_version": settings.graph_algorithm_version,
        "source_ingestion_run_id": source_ingestion_run_id,
        "nodes": [
            {"id": node.article_id, "url": node.url, "title": node.title}
            for node in sorted(nodes, key=lambda item: item.article_id)
        ],
        "edges": sorted(set(edges)),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _accepted_run_id(db: Session, site_id: int) -> int | None:
    return db.scalar(
        select(IngestionRun.id)
        .where(IngestionRun.site_id == site_id, IngestionRun.status == "succeeded")
        .order_by(IngestionRun.id.desc())
        .limit(1)
    )


def _graph_inputs(
    db: Session, site_id: int
) -> tuple[list[GraphNode], list[tuple[int, int]], int | None]:
    nodes = [
        GraphNode(article_id, url, title)
        for article_id, url, title in db.execute(
            select(Article.id, Article.url, Article.title)
            .where(Article.site_id == site_id, Article.is_active.is_(True))
            .order_by(Article.id)
        )
    ]
    source = aliased(Article)
    target = aliased(Article)
    edges = list(
        db.execute(
            select(InternalLink.source_article_id, InternalLink.target_article_id)
            .join(source, source.id == InternalLink.source_article_id)
            .join(target, target.id == InternalLink.target_article_id)
            .where(
                source.site_id == site_id,
                target.site_id == site_id,
                source.is_active.is_(True),
                target.is_active.is_(True),
                InternalLink.is_active.is_(True),
            )
            .order_by(InternalLink.source_article_id, InternalLink.target_article_id)
        )
    )
    return (
        nodes,
        [(source_id, target_id) for source_id, target_id in edges],
        _accepted_run_id(db, site_id),
    )


def latest_snapshot(db: Session, site_id: int) -> GraphSnapshot | None:
    return db.scalars(
        select(GraphSnapshot)
        .where(GraphSnapshot.site_id == site_id)
        .order_by(GraphSnapshot.computed_at.desc(), GraphSnapshot.id.desc())
        .limit(1)
    ).first()


def ensure_graph_snapshot(db: Session, site_id: int) -> tuple[GraphSnapshot, bool]:
    """Return the current graph snapshot, creating it only when its version changed."""

    # The lock protects the unique (site_id, graph_version) creation path when
    # two review/analysis requests observe the same new crawl concurrently.
    db.execute(select(func.pg_advisory_xact_lock(_GRAPH_LOCK_NAMESPACE, site_id)))
    nodes, edges, source_run_id = _graph_inputs(db, site_id)
    digest = _graph_digest(nodes, edges, source_ingestion_run_id=source_run_id)
    existing = db.scalar(
        select(GraphSnapshot).where(
            GraphSnapshot.site_id == site_id,
            GraphSnapshot.graph_version == digest,
        )
    )
    if existing is not None:
        return existing, False

    computation = compute_graph(nodes, edges)
    snapshot = GraphSnapshot(
        site_id=site_id,
        source_ingestion_run_id=source_run_id,
        algorithm_version=settings.graph_algorithm_version,
        graph_version=digest,
        article_count=computation.article_count,
        edge_count=computation.edge_count,
        orphan_count=sum(feature.orphan for feature in computation.features),
        underlinked_count=sum(feature.underlinked for feature in computation.features),
        hub_count=sum(feature.hub for feature in computation.features),
        saturated_count=sum(feature.saturated for feature in computation.features),
    )
    db.add(snapshot)
    db.flush()
    db.add_all(
        [
            GraphFeatureRow(
                snapshot_id=snapshot.id,
                article_id=feature.article_id,
                article_url=feature.article_url,
                article_title=feature.article_title,
                in_degree=feature.in_degree,
                out_degree=feature.out_degree,
                orphan_flag=feature.orphan,
                underlinked_flag=feature.underlinked,
                hub_flag=feature.hub,
                saturated_flag=feature.saturated,
                hub_score=feature.hub_score,
                saturation_score=feature.saturation_score,
            )
            for feature in computation.features
        ]
    )
    db.flush()
    return snapshot, True


def snapshot_features(
    db: Session, snapshot_id: int, article_ids: Sequence[int] | None = None
) -> dict[int, GraphFeatureData]:
    """Feature rows for a snapshot, optionally only the articles asked for.

    The bound belongs in SQL rather than in the caller. A snapshot holds one row
    per article, so a site at the analysis ceiling has 10 000 of them; the queue
    page that reads this wants the two dozen on the page. Filtering afterwards
    read the whole table on every request. `uq_graph_features_snapshot_article`
    covers the narrowed predicate.
    """
    query = select(GraphFeatureRow).where(GraphFeatureRow.snapshot_id == snapshot_id)
    if article_ids is not None:
        wanted = set(article_ids)
        if not wanted:
            return {}
        query = query.where(GraphFeatureRow.article_id.in_(wanted))
    rows = db.scalars(query.order_by(GraphFeatureRow.article_id)).all()
    return {
        row.article_id: GraphFeatureData(
            article_id=row.article_id,
            article_url=row.article_url,
            article_title=row.article_title,
            in_degree=row.in_degree,
            out_degree=row.out_degree,
            orphan=row.orphan_flag,
            underlinked=row.underlinked_flag,
            hub=row.hub_flag,
            saturated=row.saturated_flag,
            hub_score=row.hub_score,
            saturation_score=row.saturation_score,
        )
        for row in rows
    }


def current_feature_map(
    db: Session, site_id: int, article_ids: Sequence[int] | None = None
) -> tuple[GraphSnapshot | None, dict[int, GraphFeatureData]]:
    snapshot = latest_snapshot(db, site_id)
    if snapshot is None:
        return None, {}
    return snapshot, snapshot_features(db, snapshot.id, article_ids)


def deterministic_rerank(
    candidates: Sequence[CandidateT],
    features: Mapping[int, GraphFeatureData],
    *,
    source_article_id: int,
    mode: str,
    max_relevance_boost: float | None = None,
    minimum_relevance: float = 0.0,
) -> tuple[list[CandidateT], dict[int, GraphRankMetadata]]:
    """Return a relevance-first order and explain the structural adjustment.

    ``CandidateT`` only needs ``target_id`` and ``semantic_score`` attributes;
    the original objects are returned unchanged. The capped adjustment is
    applied only above the relevance floor. Disabled and shadow runs leave the
    incoming baseline order intact while still returning the proposed metadata.
    """

    max_relevance_boost = (
        settings.graph_max_relevance_boost if max_relevance_boost is None else max_relevance_boost
    )
    source = features.get(source_article_id)
    metadata: dict[int, GraphRankMetadata] = {}
    scored: list[tuple[float, int, int, CandidateT]] = []
    for baseline_index, candidate in enumerate(candidates, start=1):
        target = features.get(candidate.target_id)
        target_in_degree = target.in_degree if target is not None else None
        target_orphan = target.orphan if target is not None else False
        target_underlinked = target.underlinked if target is not None else False
        opportunity = 0.0
        if target is not None:
            if target.orphan:
                opportunity = 1.0
            elif target.underlinked:
                opportunity = 0.65
            # A saturated target is never an opportunity, even if an older
            # configuration accidentally marks both flags true.
            if target.saturated:
                opportunity = 0.0
            if source is not None and source.saturated:
                opportunity *= 0.75
        adjustment = (
            min(max_relevance_boost, max_relevance_boost * opportunity)
            if mode == "active" and candidate.semantic_score >= minimum_relevance
            else 0.0
        )
        metadata[candidate.target_id] = GraphRankMetadata(
            target_in_degree=target_in_degree,
            source_out_degree=source.out_degree if source is not None else None,
            target_orphan=target_orphan,
            target_underlinked=target_underlinked,
            source_hub=source.hub if source is not None else False,
            source_saturated=source.saturated if source is not None else False,
            opportunity=round(opportunity, 6),
            adjustment=round(adjustment, 6),
            baseline_rank=baseline_index,
            final_rank=baseline_index,
            applied=mode == "active" and adjustment > 0,
        )
        if mode == "active":
            adjusted = candidate.semantic_score + adjustment
            scored.append((-adjusted, baseline_index, candidate.target_id, candidate))

    if mode != "active":
        return list(candidates), metadata

    ordered = [candidate for _, _, _, candidate in sorted(scored)]
    for final_index, candidate in enumerate(ordered, start=1):
        old = metadata[candidate.target_id]
        metadata[candidate.target_id] = GraphRankMetadata(
            **{**old.__dict__, "final_rank": final_index}
        )
    return ordered, metadata


def graph_counts(features: Mapping[int, GraphFeatureData], edge_count: int) -> GraphCounts:
    return GraphCounts(
        active_articles=len(features),
        active_links=edge_count,
        orphan_count=sum(feature.orphan for feature in features.values()),
        underlinked_count=sum(feature.underlinked for feature in features.values()),
        hub_count=sum(feature.hub for feature in features.values()),
        saturated_count=sum(feature.saturated for feature in features.values()),
        max_in_degree=max((feature.in_degree for feature in features.values()), default=0),
        max_out_degree=max((feature.out_degree for feature in features.values()), default=0),
    )


def simulate_graph(
    computation: GraphComputation,
    proposed_edges: Sequence[tuple[int, int]],
    *,
    target_share_warning: float | None = None,
) -> GraphSimulation:
    """Simulate proposed internal edges without mutating the database."""

    current_edges = set(computation.edges)
    node_ids = {feature.article_id for feature in computation.features}
    duplicate_edges: list[tuple[int, int]] = []
    applied_edges: list[tuple[int, int]] = []
    seen_proposed: set[tuple[int, int]] = set()
    for edge in proposed_edges:
        if edge in current_edges or edge in seen_proposed:
            duplicate_edges.append(edge)
            continue
        if edge[0] not in node_ids or edge[1] not in node_ids or edge[0] == edge[1]:
            continue
        seen_proposed.add(edge)
        applied_edges.append(edge)

    nodes = [
        GraphNode(feature.article_id, feature.article_url, feature.article_title)
        for feature in computation.features
    ]
    after_computation = compute_graph(nodes, [*computation.edges, *applied_edges])
    before_by_id = {feature.article_id: feature for feature in computation.features}
    after_by_id = {feature.article_id: feature for feature in after_computation.features}
    newly_connected = tuple(
        article_id
        for article_id in sorted(before_by_id)
        if before_by_id[article_id].orphan and not after_by_id[article_id].orphan
    )
    newly_saturated = tuple(
        article_id
        for article_id in sorted(before_by_id)
        if not before_by_id[article_id].saturated and after_by_id[article_id].saturated
    )

    target_counts = Counter(target_id for _, target_id in applied_edges)
    target_concentration = (
        max(target_counts.values()) / len(applied_edges) if applied_edges else 0.0
    )
    warning_limit = (
        settings.graph_simulation_target_share_warning
        if target_share_warning is None
        else target_share_warning
    )
    warnings = []
    if not applied_edges:
        warnings.append("No new internal links would change the graph.")
    if target_concentration >= warning_limit and applied_edges:
        target_id, target_count = target_counts.most_common(1)[0]
        warnings.append(
            f"Target article {target_id} receives {target_count} of {len(applied_edges)} "
            f"new links ({target_concentration:.0%}); review concentration before publishing."
        )
    if newly_saturated:
        warnings.append(
            f"{len(newly_saturated)} target article(s) become saturated under the current "
            "graph thresholds."
        )

    return GraphSimulation(
        before=graph_counts(before_by_id, computation.edge_count),
        after=graph_counts(
            after_by_id,
            after_computation.edge_count,
        ),
        applied_edges=tuple(applied_edges),
        duplicate_edges=tuple(duplicate_edges),
        newly_connected_article_ids=newly_connected,
        newly_saturated_article_ids=newly_saturated,
        target_concentration=round(target_concentration, 6),
        warnings=tuple(warnings),
    )


def graph_computation_from_snapshot(db: Session, snapshot: GraphSnapshot) -> GraphComputation:
    features = snapshot_features(db, snapshot.id)
    source = aliased(Article)
    target = aliased(Article)
    edges = list(
        db.execute(
            select(InternalLink.source_article_id, InternalLink.target_article_id)
            .join(source, source.id == InternalLink.source_article_id)
            .join(target, target.id == InternalLink.target_article_id)
            .where(
                source.site_id == snapshot.site_id,
                target.site_id == snapshot.site_id,
                source.is_active.is_(True),
                target.is_active.is_(True),
                InternalLink.is_active.is_(True),
            )
            .order_by(InternalLink.source_article_id, InternalLink.target_article_id)
        )
    )
    return GraphComputation(
        tuple(features.values()),
        tuple((source_id, target_id) for source_id, target_id in edges),
    )
