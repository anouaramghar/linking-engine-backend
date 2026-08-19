"""Ranker invariants that need no relevance judgment.

Every other evaluation of the suggestion pipeline needs someone to say whether a
target was a good one. These tests need nobody. They assert properties that must
hold whatever the right answer is: the same input ranks the same way twice,
formatting a document differently does not move it, and a near-duplicate of a
strong target lands beside it rather than somewhere else.

That makes them the cheapest quality signal available while the reviewer-label
set is still too small to measure relevance (four `reviewed` events as of
2026-08-19, see `docs/research/suggestion-quality-research-2026-08-19.md`). They
find defects, not bad recommendations. A ranker that fails one of these is
broken in a way no amount of relevance tuning will fix.

Two of them pin a *limitation* rather than a virtue:
`test_content_after_the_token_limit_cannot_change_the_score` and
`test_body_repetition_cannot_outweigh_a_title_term` both encode the frozen
BM25-512 recipe. They are here so that a change to the recipe has to be
deliberate.
"""

from types import SimpleNamespace

import pytest

from app.ml.candidate_ordering import order_candidates
from app.ml.hybrid import (
    CONTENT_TOKEN_LIMIT,
    CorpusArticle,
    RankedCandidate,
    structured_terms,
)
from app.ml.lexical import BM25Index, tokenize


def _article(article_id: int, title: str, body: str, *, taxonomy=()) -> CorpusArticle:
    return CorpusArticle(
        id=article_id,
        title=title,
        content_text=body,
        content_fingerprint=None,
        taxonomy_names=tuple(taxonomy),
    )


def _index(articles):
    return BM25Index({article.id: structured_terms(article) for article in articles})


#: A small corpus with one obvious winner per query, plus distractors that share
#: vocabulary. Deliberately not synthetic-looking filler: overlapping terms are
#: what makes an ordering invariant meaningful.
def _corpus():
    return [
        _article(
            1,
            "Sourdough starter maintenance",
            "A sourdough starter needs regular feeding with flour and water. "
            "Discard half the starter before each feeding to keep the culture active.",
            taxonomy=("baking",),
        ),
        _article(
            2,
            "Baking bread in a home oven",
            "Bread bakes best with steam in the first minutes. A home oven holds "
            "less heat than a deck oven, so preheat the stone for an hour.",
            taxonomy=("baking",),
        ),
        _article(
            3,
            "Choosing flour for pastry",
            "Pastry flour has less protein than bread flour. Lower protein gives "
            "a tender crumb and a softer bite.",
            taxonomy=("baking",),
        ),
        _article(
            4,
            "Repairing a bicycle puncture",
            "Remove the wheel, lever the tyre off the rim, and find the hole by "
            "listening for escaping air.",
            taxonomy=("cycling",),
        ),
    ]


QUERY = tokenize("sourdough starter feeding flour")


def test_ranking_is_deterministic_across_repeated_runs():
    """Two identical calls must produce identical ranks and identical scores."""
    articles = _corpus()
    first = _index(articles).rank(QUERY, limit=10)
    second = _index(articles).rank(QUERY, limit=10)

    assert first == second


def test_insertion_order_of_the_corpus_cannot_change_the_ranking():
    """Building the index from a shuffled corpus must not move anything.

    `rank_scores` breaks ties on the document id precisely so that dict
    iteration order never decides a suggestion. This is that guarantee.
    """
    articles = _corpus()
    forward = _index(articles).rank(QUERY, limit=10)
    reversed_order = _index(list(reversed(articles))).rank(QUERY, limit=10)

    assert forward == reversed_order


def test_reformatting_a_document_cannot_change_the_ranking():
    """Whitespace and case carry no meaning, so they must carry no score."""
    articles = _corpus()
    baseline = _index(articles).rank(QUERY, limit=10)

    reformatted = []
    for article in articles:
        noisy = article.content_text.replace(" ", "  \n\t").upper()
        reformatted.append(
            _article(article.id, article.title, noisy, taxonomy=article.taxonomy_names)
        )

    assert _index(reformatted).rank(QUERY, limit=10) == baseline


def test_identical_documents_receive_adjacent_ranks():
    """Two copies of the same text cannot be separated by a third document."""
    articles = _corpus()
    original = articles[0]
    twin = _article(99, original.title, original.content_text, taxonomy=original.taxonomy_names)

    ranked = [article_id for article_id, _ in _index([*articles, twin]).rank(QUERY, limit=10)]
    assert abs(ranked.index(original.id) - ranked.index(twin.id)) == 1


def test_a_near_duplicate_ranks_beside_its_original():
    """A copy with one sentence appended must not land far from the original.

    Near-duplicate *suppression* happens later, in the eligibility predicate.
    This asserts the scoring stays stable enough for that suppression to have a
    coherent pair to act on.
    """
    articles = _corpus()
    original = articles[0]
    near = _article(
        98,
        original.title,
        original.content_text + " Keep the jar loosely covered at room temperature.",
        taxonomy=original.taxonomy_names,
    )

    ranked = [article_id for article_id, _ in _index([*articles, near]).rank(QUERY, limit=10)]
    assert abs(ranked.index(original.id) - ranked.index(near.id)) == 1


def test_content_after_the_token_limit_is_never_read():
    """Pins the frozen recipe: only the first 512 body tokens reach the index.

    This is a limitation, not a feature. A long article's later sections are
    invisible to lexical ranking. The test exists so that raising or removing
    `CONTENT_TOKEN_LIMIT` is a deliberate act with a failing test attached.

    Asserted on the term list rather than on scores: BM25 shares corpus
    statistics across documents, so lengthening one document moves every other
    document's score even when nothing past the limit was read.
    """
    body = " ".join(["alpha"] * CONTENT_TOKEN_LIMIT)
    within_limit = _article(1, "Title", body)
    past_limit = _article(1, "Title", body + " " + " ".join(["beta"] * 300))

    assert structured_terms(within_limit) == structured_terms(past_limit)
    assert "beta" not in structured_terms(past_limit)


def test_body_repetition_currently_outweighs_a_title_term():
    """Characterizes a real weakness of the frozen BM25-512 recipe.

    A document that repeats a term 200 times in its body outranks a document
    carrying that term in its title, despite the title weight of 3. BM25
    saturates term frequency, but a title term reaches only `TITLE_WEIGHT`
    occurrences, and that ceiling sits below the saturation point.

    This test asserts the behavior that exists, not the behavior we want. It is
    a tripwire: if a defense against thin, repetitive target pages is added --
    a length prior, a distinct-term floor, a cross-encoder over the final
    candidates -- this test fails and should be rewritten to assert the new
    guarantee. It matters because `app/ml/hybrid.py` states that BM25-512 alone
    decides the delivered order.

    Verified against a 30-document corpus as well as this pair, so it is not an
    artifact of a tiny index.
    """
    titled = _article(10, "Sourdough starter", "A short note about culture care.")
    stuffed = _article(11, "Untitled note", " ".join(["sourdough"] * 200))
    filler = [
        _article(index, f"Filler {index}", "unrelated words about tyres and rims " * 10)
        for index in range(12, 40)
    ]

    ranked = [
        article_id
        for article_id, _ in _index([titled, stuffed, *filler]).rank(
            tokenize("sourdough"), limit=10
        )
    ]
    assert ranked[0] == stuffed.id, "a defense against repetition landed; update this test"


def test_taxonomy_terms_contribute_to_the_score():
    """A category name must be searchable, at weight 2."""
    with_taxonomy = _article(20, "Weekly notes", "Nothing in particular.", taxonomy=("cycling",))
    without = _article(21, "Weekly notes", "Nothing in particular.")

    scores = _index([with_taxonomy, without]).score_documents(tokenize("cycling"))
    assert scores.get(with_taxonomy.id, 0.0) > scores.get(without.id, 0.0)


def test_an_oversized_article_is_refused_rather_than_silently_truncated():
    """The recipe must not accept input the crawler would never have stored."""
    from app.config import settings

    oversized = _article(30, "Long", "x" * (settings.crawl_max_article_chars + 1))
    with pytest.raises(ValueError):
        structured_terms(oversized)


# --- final ordering ---------------------------------------------------------


def _candidates(*scores: float):
    return [
        RankedCandidate(
            target_id=index,
            semantic_score=score,
            bm25_score=score,
            fusion_rank=index,
            lexical_rank=index,
        )
        for index, score in enumerate(scores, start=1)
    ]


def _order(candidates, *, remaining=3, minimum_score_percent=50):
    return order_candidates(
        candidates,
        method="hybrid_bm25",
        minimum_score_percent=minimum_score_percent,
        remaining=remaining,
        graph_features={},
        graph_snapshot=SimpleNamespace(),
        source_article_id=10,
        graph_mode="off",
        minimum_relevance=0.5,
        feedback_profile=None,
        feedback_weight=1.0,
        external_trust={},
    )


def test_final_ordering_is_deterministic():
    candidates = _candidates(0.91, 0.83, 0.77, 0.62)
    first = [item.candidate.target_id for item in _order(candidates).items]
    second = [item.candidate.target_id for item in _order(candidates).items]

    assert first == second


def test_final_ordering_never_returns_a_candidate_below_the_score_floor():
    """The floor is a hard rule, so no later stage may reintroduce a candidate."""
    ordered = _order(_candidates(0.91, 0.83, 0.40, 0.10), minimum_score_percent=50)

    assert [item.candidate.target_id for item in ordered.items] == [1, 2]


def test_final_ordering_respects_the_remaining_cap():
    ordered = _order(_candidates(0.91, 0.83, 0.77, 0.72, 0.68), remaining=3)

    assert len(ordered.items) == 3
    assert [item.final_rank for item in ordered.items] == [1, 2, 3]


def test_final_ranks_are_contiguous_and_start_at_one():
    """`final_rank` is stored as ranking evidence, so it must mean what it says."""
    ordered = _order(_candidates(0.91, 0.83, 0.77))

    assert [item.final_rank for item in ordered.items] == [1, 2, 3]
