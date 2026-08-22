from types import SimpleNamespace

from app.ml.candidate_ordering import order_candidates
from app.ml.hybrid import RankedCandidate
from app.services.editorial_feedback import (
    EditorialFeedbackProfile,
    FeedbackBucket,
)


def _order(candidates, *, method="hybrid_bm25", remaining=3, feedback_profile=None):
    return order_candidates(
        candidates,
        method=method,
        minimum_score_percent=50,
        remaining=remaining,
        graph_features={},
        graph_snapshot=SimpleNamespace(),
        source_article_id=10,
        graph_mode="off",
        minimum_relevance=0.5,
        feedback_profile=feedback_profile,
        feedback_weight=1.0,
        external_trust={},
    )


def test_ordering_returns_final_rank_and_retrieval_evidence():
    ordered = _order(
        [
            RankedCandidate(
                target_id=20,
                semantic_score=0.81,
                bm25_score=4.2,
                fusion_rank=2,
                fusion_score=0.2,
            ),
            RankedCandidate(
                target_id=30,
                semantic_score=0.74,
                bm25_score=3.1,
                fusion_rank=1,
                fusion_score=0.3,
            ),
        ]
    )

    assert [item.candidate.target_id for item in ordered.items] == [20, 30]
    assert [item.final_rank for item in ordered.items] == [1, 2]
    assert ordered.items[0].score_components["final_order"] == "wrrf"
    assert ordered.items[0].retrieval_version == "hybrid_wrrf_v2"
    assert ordered.items[0].ranking_version == "hybrid_bm25:graph=off:feedback=off"


def test_ordering_applies_feedback_before_assigning_final_rank():
    profile = EditorialFeedbackProfile(
        site_id=1,
        accepted=2,
        rejected=8,
        buckets={
            (60, 69): FeedbackBucket(60, 69, accepted=2, rejected=0),
            (90, 100): FeedbackBucket(90, 100, accepted=0, rejected=8),
        },
    )

    ordered = _order(
        [
            RankedCandidate(target_id=20, semantic_score=0.95),
            RankedCandidate(target_id=30, semantic_score=0.65),
        ],
        feedback_profile=profile,
    )

    assert [item.candidate.target_id for item in ordered.items] == [30, 20]
    assert [item.final_rank for item in ordered.items] == [1, 2]
    assert ordered.items[0].score_components["editorial_feedback"]["feedback_rank"] == 1
    assert ordered.items[1].score_components["editorial_feedback"]["feedback_rank"] == 2


def test_ordering_filters_and_caps_before_returning_persistence_rows():
    ordered = _order(
        [
            RankedCandidate(target_id=20, semantic_score=0.49),
            RankedCandidate(target_id=30, semantic_score=0.81),
            RankedCandidate(target_id=40, semantic_score=0.79),
        ],
        remaining=1,
    )

    assert [item.candidate.target_id for item in ordered.items] == [30]
    assert ordered.items[0].final_rank == 1
