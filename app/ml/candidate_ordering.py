"""Final candidate ordering and immutable ranking evidence.

Candidate retrieval produces a useful starting order, but graph opportunity and
editorial feedback may change that order before a suggestion is persisted. This
module owns that final decision and returns the evidence beside each ordered
candidate so callers do not have to reconstruct it themselves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.ml.hybrid import RankedCandidate
from app.models import GraphSnapshot
from app.services.editorial_feedback import (
    EditorialFeedbackProfile,
    rerank_with_editorial_feedback,
    score_percent,
)
from app.services.external_link_policy import TrustEvaluation
from app.services.graph_service import (
    GraphFeatureData,
    GraphRankMetadata,
    deterministic_rerank,
)


@dataclass(frozen=True)
class OrderedCandidate:
    """One final candidate with the evidence persisted with its suggestion."""

    candidate: RankedCandidate
    final_rank: int
    score_components: dict | None
    retrieval_version: str
    ranking_version: str


@dataclass(frozen=True)
class CandidateOrdering:
    """The final ordered cohort and whether graph ranking changed its order."""

    items: tuple[OrderedCandidate, ...]
    graph_reordered: bool


def _ranking_versions(
    method: str,
    components: Mapping,
    *,
    graph_mode: str,
    feedback_enabled: bool,
) -> tuple[str, str]:
    """Return immutable retrieval and ranking labels for one ordered row."""

    retrieval_version = str(
        components.get("version")
        or {
            "baseline_cosine": "cosine_v1",
            "external_search": "external_search_v1",
        }.get(method, f"{method}_v1")
    )
    ranking_version = f"{method}:graph={graph_mode}:feedback={'on' if feedback_enabled else 'off'}"
    return retrieval_version, ranking_version


def _graph_component(
    candidate: RankedCandidate,
    metadata: Mapping[int, GraphRankMetadata],
    features: Mapping[int, GraphFeatureData],
    snapshot: GraphSnapshot,
    *,
    graph_mode: str,
) -> dict | None:
    rank_metadata = metadata.get(candidate.target_id)
    target_feature = features.get(candidate.target_id)
    if rank_metadata is None or target_feature is None:
        return None
    return {
        "algorithm_version": snapshot.algorithm_version,
        "snapshot_id": snapshot.id,
        "graph_version": snapshot.graph_version,
        "source_out_degree": rank_metadata.source_out_degree,
        "source_hub": rank_metadata.source_hub,
        "source_saturated": rank_metadata.source_saturated,
        "target_in_degree": rank_metadata.target_in_degree,
        "target_orphan": rank_metadata.target_orphan,
        "target_underlinked": rank_metadata.target_underlinked,
        "target_hub": target_feature.hub,
        "target_saturated": target_feature.saturated,
        "target_hub_score": target_feature.hub_score,
        "target_saturation_score": target_feature.saturation_score,
        "opportunity": rank_metadata.opportunity,
        "adjustment": rank_metadata.adjustment,
        "baseline_rank": rank_metadata.baseline_rank,
        "final_rank": rank_metadata.final_rank,
        "applied": rank_metadata.applied,
        "mode": graph_mode,
    }


def order_candidates(
    candidates: Sequence[RankedCandidate],
    *,
    method: str,
    minimum_score_percent: int,
    remaining: int,
    graph_features: Mapping[int, GraphFeatureData],
    graph_snapshot: GraphSnapshot,
    source_article_id: int,
    graph_mode: str,
    minimum_relevance: float,
    feedback_profile: EditorialFeedbackProfile | None,
    feedback_weight: float,
    external_trust: Mapping[int, TrustEvaluation],
) -> CandidateOrdering:
    """Apply final ranking policy and build persistence-ready evidence.

    The incoming order and score semantics are preserved. Graph reranking and
    editorial feedback may reorder only eligible candidates; the returned
    ``OrderedCandidate`` records the resulting rank and every explanation that
    belongs to that row.
    """

    if remaining < 0:
        raise ValueError("remaining must be non-negative")
    if not candidates or not remaining:
        return CandidateOrdering((), False)

    ordered = [
        candidate
        for candidate in candidates
        if score_percent(candidate.semantic_score) >= minimum_score_percent
    ]
    graph_metadata: dict[int, GraphRankMetadata] = {}
    if method == "hybrid_bm25" and graph_features:
        ordered, graph_metadata = deterministic_rerank(
            ordered,
            graph_features,
            source_article_id=source_article_id,
            mode=graph_mode,
            minimum_relevance=minimum_relevance,
        )
    graph_reordered = any(
        metadata.final_rank != metadata.baseline_rank for metadata in graph_metadata.values()
    )

    feedback_components: dict[int, dict] = {}
    if feedback_profile is not None:
        ordered, feedback_components = rerank_with_editorial_feedback(
            ordered,
            feedback_profile,
            weight=feedback_weight,
        )
    ordered = ordered[:remaining]

    items: list[OrderedCandidate] = []
    for final_rank, candidate in enumerate(ordered, start=1):
        components: dict = {}
        if method == "hybrid_bm25":
            components.update(candidate.score_components())
        trust = external_trust.get(candidate.target_id)
        if trust is not None:
            components["external_trust"] = trust.as_score_component()
        graph = _graph_component(
            candidate,
            graph_metadata,
            graph_features,
            graph_snapshot,
            graph_mode=graph_mode,
        )
        if graph is not None:
            components["graph"] = graph
        feedback = feedback_components.get(candidate.target_id)
        if feedback is not None:
            components["editorial_feedback"] = feedback

        score_components = components or None
        retrieval_version, ranking_version = _ranking_versions(
            method,
            score_components or {},
            graph_mode=graph_mode,
            feedback_enabled=feedback_profile is not None,
        )
        items.append(
            OrderedCandidate(
                candidate=candidate,
                final_rank=final_rank,
                score_components=score_components,
                retrieval_version=retrieval_version,
                ranking_version=ranking_version,
            )
        )
    return CandidateOrdering(tuple(items), graph_reordered)
