import pytest

from app.ml.evaluation.metrics import (
    QueryScore,
    compare_rankings,
    evaluate_rankings,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
    summarize,
)


# One source article, five ranked candidate targets, two of them truly linked.
# The true targets sit at positions 3 and 5.
RANKED = [88, 301, 57, 190, 219]
RELEVANT = {57, 219}


def test_worked_example_scores_all_three_metrics():
    assert recall_at_k(RANKED, RELEVANT, 5) == 1.0
    assert ndcg_at_k(RANKED, RELEVANT, 5) == pytest.approx(0.5438, abs=1e-4)
    assert reciprocal_rank(RANKED, RELEVANT) == pytest.approx(1 / 3)


def test_perfect_ordering_scores_one():
    assert recall_at_k([57, 219, 88], RELEVANT, 5) == 1.0
    assert ndcg_at_k([57, 219, 88], RELEVANT, 5) == 1.0
    assert reciprocal_rank([57, 219, 88], RELEVANT) == 1.0


def test_missing_targets_score_zero():
    assert recall_at_k([1, 2, 3], RELEVANT, 5) == 0.0
    assert ndcg_at_k([1, 2, 3], RELEVANT, 5) == 0.0
    assert reciprocal_rank([1, 2, 3], RELEVANT) == 0.0


def test_recall_ignores_position_but_ndcg_does_not():
    top = [57, 219, 1, 2, 3]
    bottom = [1, 2, 3, 57, 219]

    assert recall_at_k(top, RELEVANT, 5) == recall_at_k(bottom, RELEVANT, 5)
    assert ndcg_at_k(top, RELEVANT, 5) > ndcg_at_k(bottom, RELEVANT, 5)


def test_recall_counts_only_the_first_k():
    # The second true target sits at position 5 and is outside k=3.
    assert recall_at_k(RANKED, RELEVANT, 3) == 0.5
    assert recall_at_k(RANKED, RELEVANT, 5) == 1.0


def test_ndcg_is_normalized_when_fewer_targets_than_k_exist():
    # One true target at rank 1 cannot be beaten, so the ideal ordering is itself.
    assert ndcg_at_k([57, 1, 2], {57}, 5) == 1.0


def test_a_ranked_list_shorter_than_k_is_allowed():
    assert recall_at_k([57], RELEVANT, 5) == 0.5
    assert reciprocal_rank([57], RELEVANT) == 1.0


def test_reciprocal_rank_can_be_cut_at_k():
    assert reciprocal_rank(RANKED, RELEVANT) == pytest.approx(1 / 3)
    assert reciprocal_rank(RANKED, RELEVANT, k=2) == 0.0


def test_unscorable_and_malformed_inputs_are_rejected():
    with pytest.raises(ValueError, match="must not be empty"):
        recall_at_k(RANKED, set(), 5)
    with pytest.raises(ValueError, match="duplicate"):
        recall_at_k([57, 57, 88], RELEVANT, 5)
    with pytest.raises(ValueError, match="k must be at least 1"):
        recall_at_k(RANKED, RELEVANT, 0)


def test_evaluate_rankings_averages_over_source_articles():
    summary = evaluate_rankings(
        rankings={10: [57, 219], 11: [1, 2, 3]},
        relevant_by_source={10: {57}, 11: {99}},
        k=5,
    )

    assert summary.queries == 2
    assert summary.skipped_without_relevant == 0
    # One perfect query and one that found nothing.
    assert summary.recall_at_k == 0.5
    assert summary.ndcg_at_k == 0.5
    assert summary.mrr == 0.5


def test_evaluate_rankings_skips_source_articles_without_a_true_target():
    summary = evaluate_rankings(
        rankings={10: [57], 12: [1]},
        relevant_by_source={10: {57}, 12: set()},
        k=5,
    )

    assert summary.queries == 1
    assert summary.skipped_without_relevant == 1
    assert summary.ndcg_at_k == 1.0


def test_a_source_article_with_no_suggestions_scores_zero_and_is_not_skipped():
    summary = evaluate_rankings(rankings={}, relevant_by_source={10: {57}}, k=5)

    assert summary.queries == 1
    assert summary.skipped_without_relevant == 0
    assert summary.recall_at_k == 0.0


def test_summary_reports_a_confidence_interval_around_the_mean():
    mixed = [
        QueryScore(source_article_id=i, recall=value, ndcg=value, reciprocal_rank=value)
        for i, value in enumerate([0.0, 1.0, 0.0, 1.0, 0.5, 0.5])
    ]
    identical = [
        QueryScore(source_article_id=i, recall=0.5, ndcg=0.5, reciprocal_rank=0.5) for i in range(6)
    ]

    mixed_summary = summarize(mixed, k=5)
    identical_summary = summarize(identical, k=5)

    low, high = mixed_summary.ndcg_ci95
    assert low < mixed_summary.ndcg_at_k < high
    # Agreeing queries give a tighter interval than disagreeing ones.
    assert high - low > identical_summary.ndcg_ci95[1] - identical_summary.ndcg_ci95[0]
    # The interval cannot leave the range the metric itself lives in.
    assert 0.0 <= low and high <= 1.0


def test_empty_summary_is_reportable_rather_than_an_error():
    summary = summarize([], k=5, skipped_without_relevant=3)

    assert summary.queries == 0
    assert summary.ndcg_at_k == 0.0
    assert summary.skipped_without_relevant == 3
    assert summary.to_dict()["k"] == 5


def test_compare_rankings_reports_paired_relevance_tradeoffs():
    comparison = compare_rankings(
        candidate_rankings={10: [1, 57], 11: [57, 3], 12: [7]},
        baseline_rankings={10: [57, 1], 11: [3, 57], 12: [7]},
        relevant_by_source={10: {57}, 11: {57}, 12: {99}},
        k=1,
    )

    assert comparison.queries == 3
    assert comparison.reordered_queries == 2
    assert comparison.top_k_changed_queries == 2
    assert comparison.baseline_relevant_hits_at_k == 1
    assert comparison.candidate_relevant_hits_at_k == 1
    assert comparison.relevant_hit_gain_queries == 1
    assert comparison.relevant_hit_loss_queries == 1
    assert comparison.relevant_hit_unchanged_queries == 1
    assert comparison.ndcg_improved_queries == 1
    assert comparison.ndcg_worsened_queries == 1
    assert comparison.ndcg_unchanged_queries == 1
