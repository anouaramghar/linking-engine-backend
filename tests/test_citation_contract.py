"""Regression tests for the citation detector's public bounds and threshold."""

from app.services.citation_need import analyze_citation_needs


def test_zero_threshold_does_not_promote_unscored_prose() -> None:
    analysis = analyze_citation_needs(
        "This is ordinary prose with no claim and no evidence signal.",
        threshold=0.0,
    )

    assert analysis.total_detected == 0
    assert analysis.needs == ()


def test_result_limit_cannot_exceed_the_contract_bound() -> None:
    text = " ".join(
        f"A 20{10 + index} study found that treatment reduces risk by {index + 10}%."
        for index in range(12)
    )

    analysis = analyze_citation_needs(text, max_results=100)

    assert analysis.total_detected == 12
    assert len(analysis.needs) == 10
