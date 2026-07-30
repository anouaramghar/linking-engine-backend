"""Limited-pilot ranking, eligibility, shadowing, and fallback behavior."""

import hashlib
import math

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings, settings
from app.ml.hybrid import (
    DENSE_RRF_WEIGHT,
    LEXICAL_RRF_WEIGHT,
    RRF_RANK_CONSTANT,
    CorpusArticle,
    HybridRanker,
    HybridRanking,
    RankedCandidate,
    normalized_title,
    structured_terms,
    weighted_reciprocal_rank_fusion,
    weighted_rrf_scores,
)
from app.models import Article, Embedding, InternalLink, Suggestion
from app.models.article import EMBEDDING_DIM
from app.services.suggestion_service import generate_suggestions

DUPLICATE_THRESHOLD = 0.99


def _vector(direction: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[direction] = 1.0
    return vector


def _similar_vector(similarity: float, axis: int) -> list[float]:
    """A unit vector whose cosine against ``_vector(0)`` is exactly ``similarity``.

    The remainder goes on a per-article axis so the fixture's articles are not all
    near-duplicates of *each other* as a side effect of resembling the source.
    """
    vector = [0.0] * EMBEDDING_DIM
    vector[0] = similarity
    vector[axis] = math.sqrt(max(0.0, 1.0 - similarity**2))
    return vector


def _fingerprint(title: str, content: str) -> str:
    """The fingerprint `_embed_missing` computes, so it leaves the fixture alone."""
    return hashlib.sha256(f"{title}\n{content}".encode()).hexdigest()


@pytest.fixture(autouse=True)
def valid_dimension_probe(monkeypatch):
    monkeypatch.setattr(
        "app.ml.embeddings.encode",
        lambda texts: [_vector(0) for _text in texts],
    )


@pytest.fixture(autouse=True)
def no_site_is_enrolled_by_default(monkeypatch):
    """Every test starts from the committed defaults: both rollout lists empty."""
    monkeypatch.setattr(settings, "v1_shadow_site_ids", frozenset())
    monkeypatch.setattr(settings, "v1_pilot_site_ids", frozenset())


def _make_articles(db, site) -> list[Article]:
    values = (
        ("Beach guide", "beach restaurants parking ocean", _vector(0)),
        ("Beach restaurants", "restaurants near the beach", _vector(1)),
        ("Ski lodge", "mountain snow ski", _vector(2)),
    )
    articles = []
    for index, (title, content, vector) in enumerate(values):
        article = Article(
            site_id=site.id,
            url=f"{site.base_url}/pilot-{index}",
            title=title,
            content_text=content,
        )
        db.add(article)
        db.flush()
        db.add(
            Embedding(
                article_id=article.id,
                model=settings.embedding_model,
                vector=vector,
                content_fingerprint=_fingerprint(title, content),
                input_recipe_version=1,
                vector_size=EMBEDDING_DIM,
            )
        )
        articles.append(article)
    db.commit()
    return articles


def _add_article(
    db,
    site,
    *,
    slug: str,
    title: str,
    content: str,
    vector: list[float],
    is_active: bool = True,
    fingerprint: str | None = None,
) -> Article:
    article = Article(
        site_id=site.id,
        url=f"{site.base_url}/{slug}",
        title=title,
        content_text=content,
        is_active=is_active,
    )
    db.add(article)
    db.flush()
    db.add(
        Embedding(
            article_id=article.id,
            model=settings.embedding_model,
            vector=vector,
            content_fingerprint=fingerprint or _fingerprint(title, content),
            input_recipe_version=1,
            vector_size=EMBEDDING_DIM,
        )
    )
    return article


# The shared vocabulary every fixture article below is built from, so that each
# one lands high in the BM25 pool and has to be removed by an eligibility rule
# rather than by simply not matching.
LEXICAL_BODY = "tomato canning jars boiling water safety altitude"


@pytest.fixture
def eligibility_corpus(db, site):
    """One source plus a target for every exclusion rule, all lexically strong."""
    source = _add_article(
        db,
        site,
        slug="source",
        title="Tomato canning basics",
        content=LEXICAL_BODY,
        vector=_vector(0),
    )
    targets = {
        # Cosine 0.995 to the source: the same page in different words. Only a
        # vector comparison can catch this, so BM25 alone would suggest it.
        "vector_duplicate": _add_article(
            db,
            site,
            slug="vector-duplicate",
            title="Preserving tomatoes at home",
            content=f"{LEXICAL_BODY} guide",
            vector=_similar_vector(0.995, axis=5),
        ),
        # Same title once lower(btrim) is applied.
        "same_title": _add_article(
            db,
            site,
            slug="same-title",
            title="  TOMATO CANNING BASICS  ",
            content=f"{LEXICAL_BODY} reprint",
            vector=_similar_vector(0.60, axis=6),
        ),
        # Byte-identical content fingerprint.
        "same_fingerprint": _add_article(
            db,
            site,
            slug="same-fingerprint",
            title="Canning tomatoes duplicate",
            content=f"{LEXICAL_BODY} copy",
            vector=_similar_vector(0.55, axis=7),
            fingerprint=_fingerprint("Tomato canning basics", LEXICAL_BODY),
        ),
        "inactive": _add_article(
            db,
            site,
            slug="inactive",
            title="Retired canning guide",
            content=f"{LEXICAL_BODY} retired",
            vector=_similar_vector(0.50, axis=8),
            is_active=False,
        ),
        "linked": _add_article(
            db,
            site,
            slug="linked",
            title="Canning jars explained",
            content=f"{LEXICAL_BODY} jars",
            vector=_similar_vector(0.45, axis=9),
        ),
        "decided": _add_article(
            db,
            site,
            slug="decided",
            title="Water bath canning",
            content=f"{LEXICAL_BODY} bath",
            vector=_similar_vector(0.40, axis=10),
        ),
        "eligible": _add_article(
            db,
            site,
            slug="eligible",
            title="Altitude adjustments for canning",
            content=f"{LEXICAL_BODY} altitude chart",
            vector=_similar_vector(0.35, axis=11),
        ),
    }
    db.flush()
    db.add(
        InternalLink(
            source_article_id=source.id,
            target_article_id=targets["linked"].id,
            is_active=True,
        )
    )
    db.add(
        Suggestion(
            site_id=site.id,
            source_article_id=source.id,
            target_article_id=targets["decided"].id,
            method="baseline_cosine",
            score=0.5,
            status="rejected",
        )
    )
    db.commit()
    return source, targets


def _rank(db, site, source, limit=5):
    ranker = HybridRanker.load(db, site_id=site.id, model=settings.embedding_model)
    return ranker.rank(
        db,
        source_id=source.id,
        model=settings.embedding_model,
        limit=limit,
        duplicate_similarity_threshold=DUPLICATE_THRESHOLD,
    )


# --- frozen recipe and fusion -------------------------------------------------


def test_frozen_structured_recipe_and_weighted_rrf():
    article = CorpusArticle(
        id=1,
        title="Beach Guide",
        content_text="restaurants ocean",
        content_fingerprint=None,
        taxonomy_names=("Travel",),
    )

    terms = structured_terms(article)

    assert terms.count("beach") == 3
    assert terms.count("guide") == 3
    assert terms.count("travel") == 2
    assert terms[-2:] == ["restaurants", "ocean"]
    assert weighted_reciprocal_rank_fusion([1, 2], [3, 2]) == [2, 3, 1]


def test_the_fusion_weights_are_the_frozen_ones():
    """Arithmetic, not just order: a silent weight change would still rank the same
    way in the small case above."""
    assert (DENSE_RRF_WEIGHT, LEXICAL_RRF_WEIGHT, RRF_RANK_CONSTANT) == (0.25, 1.0, 10)

    scores = dict(weighted_rrf_scores([7, 8], [8, 9]))

    assert scores[7] == pytest.approx(0.25 / 11)
    assert scores[8] == pytest.approx(0.25 / 12 + 1.0 / 11)
    assert scores[9] == pytest.approx(1.0 / 12)


def test_normalized_title_matches_sql_lower_btrim():
    """Collapsing internal whitespace would make the in-memory pre-filter stricter
    than `lower(btrim(title))` and silently drop allowed candidates."""
    assert normalized_title("  TOMATO Canning Basics  ") == "tomato canning basics"
    assert normalized_title("tomato  canning") != normalized_title("tomato canning")


# --- final ordering -----------------------------------------------------------


def test_hybrid_ranker_uses_bm25_for_final_order(monkeypatch):
    articles = {
        1: CorpusArticle(1, "Beach guide", "beach food", None, ()),
        2: CorpusArticle(2, "Beach food", "beach restaurant", None, ()),
        3: CorpusArticle(3, "Ski lodge", "snow mountain", None, ()),
    }
    ranker = HybridRanker(articles=articles, blocked_targets={})
    monkeypatch.setattr(
        "app.ml.hybrid.eligible_top_candidates",
        lambda *_args, **_kwargs: [(3, 0.9), (2, 0.8)],
    )
    monkeypatch.setattr(
        "app.ml.hybrid.eligible_candidate_scores",
        lambda _db, _source, _model, target_ids, _threshold: {
            target_id: 0.5 for target_id in target_ids
        },
    )

    ranking = ranker.rank(
        object(),
        source_id=1,
        model="model",
        limit=2,
        duplicate_similarity_threshold=DUPLICATE_THRESHOLD,
    )

    # Dense preferred 3 then 2; BM25 puts the lexically similar 2 first, and BM25
    # is what decides the delivered order.
    assert [candidate.target_id for candidate in ranking.candidates] == [2, 3]
    assert ranking.union_count == 2


def test_bm25_order_wins_over_the_fused_order(db, site, eligibility_corpus):
    """The documented policy, on real rows: the fused rank only breaks ties."""
    source, _targets = eligibility_corpus

    ranking = _rank(db, site, source)

    delivered = [candidate.bm25_score for candidate in ranking.candidates]
    assert delivered == sorted(delivered, reverse=True)


# --- eligibility (correction 5) ----------------------------------------------


def test_every_exclusion_rule_applies_to_lexically_retrieved_candidates(
    db, site, eligibility_corpus
):
    """The rules BM25 cannot see are still enforced.

    Each excluded article here is lexically strong enough to sit near the top of
    the BM25 pool, so its absence is the eligibility predicate doing work rather
    than a weak text match.
    """
    source, targets = eligibility_corpus

    ranking = _rank(db, site, source)

    delivered = {candidate.target_id for candidate in ranking.candidates}
    assert delivered == {targets["eligible"].id}
    for name in (
        "vector_duplicate",
        "same_title",
        "same_fingerprint",
        "inactive",
        "linked",
        "decided",
    ):
        assert targets[name].id not in delivered, f"{name} should have been excluded"
    assert source.id not in delivered


def test_a_lexical_only_near_duplicate_is_excluded_by_the_vector_rule(
    db, site, eligibility_corpus
):
    """Isolates the rule that only the shared SQL predicate can enforce.

    The near-duplicate is the strongest lexical match in the corpus and is not
    caught by the title or fingerprint filters, so it reaches the pool through
    lexical retrieval alone. Only the cosine ceiling removes it.
    """
    source, targets = eligibility_corpus
    duplicate = targets["vector_duplicate"]

    ranker = HybridRanker.load(db, site_id=site.id, model=settings.embedding_model)
    lexical_pool = ranker.index.rank(
        ranker.terms_by_article[source.id],
        limit=100,
        excluded_ids={source.id},
    )
    assert duplicate.id in {target_id for target_id, _score in lexical_pool}

    ranking = ranker.rank(
        db,
        source_id=source.id,
        model=settings.embedding_model,
        limit=5,
        duplicate_similarity_threshold=DUPLICATE_THRESHOLD,
    )

    assert duplicate.id not in {candidate.target_id for candidate in ranking.candidates}


def test_relaxing_the_threshold_readmits_the_near_duplicate(db, site, eligibility_corpus):
    """Proves the exclusion above is the threshold at work and not an accident."""
    source, targets = eligibility_corpus

    ranker = HybridRanker.load(db, site_id=site.id, model=settings.embedding_model)
    ranking = ranker.rank(
        db,
        source_id=source.id,
        model=settings.embedding_model,
        limit=5,
        duplicate_similarity_threshold=0.999,
    )

    assert targets["vector_duplicate"].id in {c.target_id for c in ranking.candidates}


def test_lexical_pool_backfills_after_an_ineligible_first_page(monkeypatch):
    """The pool is the top 100 eligible rows, not the raw top 100 before filtering."""
    articles = {
        article_id: CorpusArticle(
            article_id,
            f"Title {article_id}",
            f"Body {article_id}",
            f"fingerprint-{article_id}",
            (),
        )
        for article_id in range(202)
    }
    ranker = HybridRanker(articles=articles, blocked_targets={})
    ranker.index.score_documents = lambda *_args, **_kwargs: {
        **{article_id: float(202 - article_id) for article_id in range(1, 102)},
        **{article_id: 0.001 for article_id in range(102, 202)},
    }
    monkeypatch.setattr(
        "app.ml.hybrid.eligible_top_candidates",
        lambda *_args, **_kwargs: [
            (article_id, 0.8) for article_id in range(102, 202)
        ],
    )
    checked_pages: list[list[int]] = []

    def eligible_scores(_db, _source, _model, target_ids, _threshold):
        checked_pages.append(list(target_ids))
        return {101: 0.2} if 101 in target_ids else {}

    monkeypatch.setattr(
        "app.ml.hybrid.eligible_candidate_scores",
        eligible_scores,
    )

    ranking = ranker.rank(
        object(),
        source_id=0,
        model="model",
        limit=5,
        duplicate_similarity_threshold=DUPLICATE_THRESHOLD,
    )

    assert checked_pages == [list(range(1, 101)), [101]]
    assert ranking.lexical_count == 100
    assert ranking.union_count == 101
    assert ranking.candidates[0].target_id == 101
    assert ranking.candidates[0].bm25_score == pytest.approx(101.0)


def test_a_cross_site_article_is_never_a_candidate(db, site, eligibility_corpus):
    import uuid

    from app.models import Site

    source, _targets = eligibility_corpus
    other = Site(
        name="other-pilot-site",
        base_url=f"https://other-{uuid.uuid4().hex[:8]}.example.com",
        platform="html",
    )
    db.add(other)
    db.commit()
    _add_article(
        db,
        other,
        slug="cross-site",
        title="Tomato canning on another site",
        content=LEXICAL_BODY,
        vector=_similar_vector(0.9, axis=12),
    )
    db.commit()
    try:
        ranking = _rank(db, site, source)

        target_ids = {candidate.target_id for candidate in ranking.candidates}
        cross_site_ids = set(
            db.scalars(select(Article.id).where(Article.site_id == other.id)).all()
        )
        assert not (target_ids & cross_site_ids)
    finally:
        db.delete(other)
        db.commit()


# --- stored score and components (correction 3) ------------------------------


def test_pilot_rows_store_cosine_as_the_score_and_bm25_in_the_components(
    db, site, eligibility_corpus
):
    source, targets = eligibility_corpus
    site.suggestion_mode = "experimental"
    db.commit()

    generate_suggestions(site.id)

    row = db.scalars(
        select(Suggestion).where(
            Suggestion.source_article_id == source.id,
            Suggestion.target_article_id == targets["eligible"].id,
        )
    ).one()
    assert row.method == "hybrid_bm25"

    # `score` is the pair's cosine similarity, so the dashboard percentage and
    # its thresholds keep the meaning they have for baseline rows.
    components = row.score_components
    assert row.score == pytest.approx(components["semantic"])
    assert 0.0 <= row.score <= 1.0
    assert row.score == pytest.approx(0.35, abs=1e-6)

    # BM25 is reported separately, raw, and is not rescaled into anything that
    # reads as a confidence.
    assert components["version"] == "hybrid_bm25_v1"
    assert components["final_order"] == "bm25_512"
    assert components["score_is"] == "cosine_semantic_similarity"
    assert components["recipe"] == "structured_t3_tax2_c512"
    assert components["bm25_score"] > 0.0
    assert components["bm25_score"] != pytest.approx(row.score)
    assert components["fusion"] == {
        "name": "wrrf_d025_l100_k10",
        "dense_weight": 0.25,
        "lexical_weight": 1.0,
        "rank_constant": 10,
    }
    assert components["fusion_rank"] >= 1
    assert components["fusion_score"] > 0.0
    assert components["lexical_rank"] >= 1


def test_baseline_rows_store_no_components(db, site):
    _make_articles(db, site)

    generate_suggestions(site.id)

    rows = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    assert rows
    assert {row.method for row in rows} == {"baseline_cosine"}
    assert all(row.score_components is None for row in rows)


def test_the_api_serves_the_components_for_a_pilot_row(db, site, client, eligibility_corpus):
    source, _targets = eligibility_corpus
    site.suggestion_mode = "experimental"
    db.commit()
    generate_suggestions(site.id)

    response = client.get(
        f"/api/v1/suggestions/{site.id}", params={"method": "hybrid_bm25"}
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload
    served = next(row for row in payload if row["source_article"]["id"] == source.id)
    assert served["method"] == "hybrid_bm25"
    assert served["score_components"]["final_order"] == "bm25_512"
    assert served["score_components"]["bm25_score"] > 0.0
    assert served["score"] == pytest.approx(served["score_components"]["semantic"])


# --- rollout controls (correction 1) -----------------------------------------


def test_committed_defaults_enroll_no_site():
    defaults = Settings(_env_file=None)

    assert defaults.v1_shadow_site_ids == frozenset()
    assert defaults.v1_pilot_site_ids == frozenset()
    assert defaults.v1_pilot_max_suggestions_per_article == 1
    assert defaults.suggestion_duplicate_similarity_threshold == 0.99


def test_a_standard_site_still_gets_the_baseline_path(db, site):
    """The default mode must not be quietly upgraded to hybrid."""
    _make_articles(db, site)
    assert site.suggestion_mode == "standard"

    result = generate_suggestions(site.id)

    rows = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    assert rows
    assert {row.method for row in rows} == {"baseline_cosine"}
    assert "ranking_mode" not in result


def test_a_standard_site_keeps_the_normal_per_source_cap(db, site, monkeypatch):
    articles = _make_articles(db, site)
    monkeypatch.setattr(settings, "max_suggestions_per_article", 2)
    monkeypatch.setattr(settings, "v1_pilot_max_suggestions_per_article", 1)

    generate_suggestions(site.id)

    rows = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    counts = {
        article.id: sum(row.source_article_id == article.id for row in rows)
        for article in articles
    }
    assert set(counts.values()) == {2}


def test_pilot_site_persists_hybrid_method(db, site, monkeypatch):
    source, target, _other = _make_articles(db, site)
    site.suggestion_mode = "experimental"
    db.commit()

    class FakeRanker:
        def rank(self, _db, *, source_id, **_kwargs):
            target_id = target.id if source_id != target.id else source.id
            candidate = RankedCandidate(target_id=target_id, semantic_score=0.77)
            return HybridRanking(
                candidates=(candidate,),
                baseline_candidates=(candidate,),
                dense_count=2,
                lexical_count=2,
                union_count=3,
            )

    monkeypatch.setattr(
        "app.services.suggestion_service.HybridRanker.load",
        lambda *_args, **_kwargs: FakeRanker(),
    )

    result = generate_suggestions(site.id)

    suggestions = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    assert suggestions
    assert {suggestion.method for suggestion in suggestions} == {"hybrid_bm25"}
    assert result["ranking_mode"] == "pilot"
    assert result["hybrid_fallback_sources"] == 0
    assert result["mean_union_candidates"] == 3.0


def test_pilot_site_persists_only_rank_one_per_source(db, site, monkeypatch):
    articles = _make_articles(db, site)
    site.suggestion_mode = "experimental"
    db.commit()
    calls = []

    class FakeRanker:
        def rank(self, _db, *, source_id, limit, **_kwargs):
            calls.append((source_id, limit))
            candidates = tuple(
                RankedCandidate(target_id=article.id, semantic_score=0.8)
                for article in articles
                if article.id != source_id
            )
            return HybridRanking(
                candidates=candidates[:limit],
                baseline_candidates=candidates[:limit],
                dense_count=2,
                lexical_count=2,
                union_count=2,
            )

    monkeypatch.setattr(
        "app.services.suggestion_service.HybridRanker.load",
        lambda *_args, **_kwargs: FakeRanker(),
    )

    result = generate_suggestions(site.id)

    rows = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    counts = {
        article.id: sum(row.source_article_id == article.id for row in rows)
        for article in articles
    }
    assert set(counts.values()) == {1}
    assert {limit for _source_id, limit in calls} == {1}
    assert result["suggestion_cap_per_source"] == 1


def test_explicit_comparison_never_persists_suggestions(db, site, monkeypatch):
    source, target, other = _make_articles(db, site)
    calls = []
    monkeypatch.setattr(settings, "max_suggestions_per_article", 2)
    monkeypatch.setattr(settings, "v1_pilot_max_suggestions_per_article", 1)

    class FakeRanker:
        def rank(self, _db, *, source_id, limit, **_kwargs):
            calls.append((source_id, limit))
            available = [
                article.id for article in (source, target, other) if article.id != source_id
            ]
            baseline = tuple(
                RankedCandidate(target_id=target_id, semantic_score=0.8 - index * 0.1)
                for index, target_id in enumerate(available)
            )
            return HybridRanking(
                candidates=tuple(reversed(baseline)),
                baseline_candidates=baseline,
                dense_count=2,
                lexical_count=2,
                union_count=2,
            )

    monkeypatch.setattr(
        "app.services.suggestion_service.HybridRanker.load",
        lambda *_args, **_kwargs: FakeRanker(),
    )

    result = generate_suggestions(
        site.id,
        ranking_mode_override="shadow",
        comparison_only=True,
    )

    suggestions = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    assert suggestions == []
    assert len(calls) == 3
    assert {limit for _source_id, limit in calls} == {2}
    assert result["ranking_mode"] == "shadow"
    assert result["comparison_only"] is True
    assert result["suggestions_created"] == 0
    assert result["suggestion_cap_per_source"] == 2


def test_shadow_site_persists_baseline_and_reports_overlap(db, site, monkeypatch):
    source, target, other = _make_articles(db, site)
    monkeypatch.setattr(settings, "v1_shadow_site_ids", frozenset({site.id}))

    class FakeRanker:
        def rank(self, _db, *, source_id, include_baseline=False, **_kwargs):
            assert include_baseline, "shadow must compare against the real baseline rows"
            available = [
                article.id for article in (source, target, other) if article.id != source_id
            ]
            baseline = tuple(
                RankedCandidate(target_id=target_id, semantic_score=0.8 - index * 0.1)
                for index, target_id in enumerate(available)
            )
            return HybridRanking(
                candidates=tuple(reversed(baseline)),
                baseline_candidates=baseline,
                dense_count=2,
                lexical_count=2,
                union_count=2,
            )

    monkeypatch.setattr(
        "app.services.suggestion_service.HybridRanker.load",
        lambda *_args, **_kwargs: FakeRanker(),
    )

    result = generate_suggestions(site.id)

    suggestions = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    assert suggestions
    assert {suggestion.method for suggestion in suggestions} == {"baseline_cosine"}
    assert all(suggestion.score_components is None for suggestion in suggestions)
    assert result["ranking_mode"] == "shadow"
    assert result["shadow_mean_overlap_at_5"] == 1.0
    assert result["shadow_exact_order_rate"] == 0.0


def test_shadow_sample_runs_when_existing_queue_fills_the_quota(db, site, monkeypatch):
    _make_articles(db, site)
    monkeypatch.setattr(settings, "max_suggestions_per_article", 1)
    baseline = generate_suggestions(site.id)
    assert baseline["suggestions_created"] == 3

    monkeypatch.setattr(settings, "v1_shadow_site_ids", frozenset({site.id}))
    monkeypatch.setattr(settings, "v1_shadow_max_sources", 2)
    calls = []

    class FakeRanker:
        def rank(self, _db, *, source_id, limit, **_kwargs):
            calls.append((source_id, limit))
            candidate = RankedCandidate(target_id=source_id, semantic_score=1.0)
            return HybridRanking(
                candidates=(candidate,),
                baseline_candidates=(candidate,),
                dense_count=2,
                lexical_count=2,
                union_count=2,
            )

    monkeypatch.setattr(
        "app.services.suggestion_service.HybridRanker.load",
        lambda *_args, **_kwargs: FakeRanker(),
    )

    result = generate_suggestions(site.id)

    assert result["suggestions_created"] == 0
    assert result["eligible_sources"] == 0
    assert result["shadow_sources_selected"] == 2
    assert result["hybrid_sources_evaluated"] == 2
    assert len(calls) == 2
    assert {limit for _source_id, limit in calls} == {1}


def test_pilot_initialization_failure_falls_back_to_baseline(db, site, monkeypatch):
    _make_articles(db, site)
    monkeypatch.setattr(settings, "v1_pilot_site_ids", frozenset({site.id}))
    monkeypatch.setattr(
        "app.services.suggestion_service.HybridRanker.load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("index failed")),
    )

    result = generate_suggestions(site.id)

    suggestions = db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    assert suggestions
    assert {suggestion.method for suggestion in suggestions} == {"baseline_cosine"}
    assert result["ranking_mode"] == "pilot"
    assert result["hybrid_ranker_loaded"] is False
    assert result["hybrid_fallback_sources"] == result["eligible_sources"]


def test_shadow_and_pilot_site_sets_must_not_overlap():
    with pytest.raises(ValidationError, match="overlap: 7"):
        Settings(
            _env_file=None,
            v1_shadow_site_ids=frozenset({7}),
            v1_pilot_site_ids=frozenset({7}),
        )
