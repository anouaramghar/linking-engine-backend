from types import SimpleNamespace

import pytest

from app.config import settings
from app.ml.candidate_ordering import order_candidates
from app.ml.hybrid import RankedCandidate, max_fusion_score
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


def test_fused_rows_rank_on_the_fusion_score_as_a_fraction_of_its_ceiling():
    """The queue's number has to separate rows that cosine cannot.

    On a real corpus every candidate that survives retrieval sits inside a
    cosine band a couple of points wide, so ordering the queue on cosine is
    close to ordering it at random. The fusion score is what actually chose the
    order, and it is bounded, so it is what the queue stores.
    """
    ordered = _order(
        [
            RankedCandidate(
                target_id=20,
                semantic_score=0.93,
                bm25_score=4.2,
                fusion_rank=1,
                fusion_score=max_fusion_score(),
            ),
            RankedCandidate(
                target_id=30,
                semantic_score=0.92,
                bm25_score=3.1,
                fusion_rank=2,
                fusion_score=max_fusion_score() / 4,
            ),
        ]
    )

    # First place in both retrievers is the strongest statement the fusion can
    # make, so it is exactly 1.0 rather than merely the largest value seen.
    assert ordered.items[0].rank_score == pytest.approx(1.0)
    assert ordered.items[1].rank_score == pytest.approx(0.25)
    # A one-point cosine gap could not have produced that separation.
    assert ordered.items[0].candidate.semantic_score == pytest.approx(0.93)


def test_rows_the_fusion_did_not_order_keep_cosine_as_their_rank_score(monkeypatch):
    """BM25 is unbounded, so there is no honest percentage to derive from it.

    Falling back to cosine keeps such a row in the position it has always had,
    which matters because the column is NOT NULL and one queue orders every
    method together.
    """
    baseline = _order(
        [RankedCandidate(target_id=30, semantic_score=0.77)], method="baseline_cosine"
    )
    assert baseline.items[0].rank_score == pytest.approx(0.77)

    monkeypatch.setattr(settings, "hybrid_final_order", "bm25_512")
    hybrid = _order(
        [
            RankedCandidate(
                target_id=20,
                semantic_score=0.88,
                bm25_score=12.5,
                fusion_rank=3,
                fusion_score=0.05,
            )
        ]
    )

    # The fusion score is present but did not decide anything here, so reading
    # it would describe a ranking that never happened.
    assert hybrid.items[0].score_components["final_order"] == "bm25_512"
    assert hybrid.items[0].rank_score == pytest.approx(0.88)


def test_the_fusion_ceiling_follows_the_configured_dense_weight():
    """A reweighted deployment must not rescale old rows against a new ceiling.

    ``max_fusion_score`` is computed from the live weights precisely so the
    ceiling recorded alongside a row stays the one that produced it.
    """
    assert max_fusion_score() == pytest.approx((settings.hybrid_dense_rrf_weight + 1.0) / 11)

    candidate = RankedCandidate(
        target_id=20, semantic_score=0.9, fusion_score=max_fusion_score() * 2
    )

    # A score above the ceiling is impossible, but a clamped 1.0 is still a
    # truthful percentage where an unclamped 200% would not be.
    assert candidate.normalized_fusion_score == pytest.approx(1.0)
