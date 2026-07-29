"""Limited-pilot ranking, shadowing, and fallback behavior."""

import hashlib

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings, settings
from app.ml.hybrid import (
    CorpusArticle,
    HybridRanker,
    HybridRanking,
    RankedCandidate,
    structured_terms,
    weighted_reciprocal_rank_fusion,
)
from app.models import Article, Embedding, Suggestion
from app.models.article import EMBEDDING_DIM
from app.services.suggestion_service import generate_suggestions


def _vector(direction: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[direction] = 1.0
    return vector


@pytest.fixture(autouse=True)
def valid_dimension_probe(monkeypatch):
    monkeypatch.setattr(
        "app.ml.embeddings.encode",
        lambda texts: [_vector(0) for _text in texts],
    )


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
        encode_input = f"{title}\n{content}"
        db.add(
            Embedding(
                article_id=article.id,
                model=settings.embedding_model,
                vector=vector,
                content_fingerprint=hashlib.sha256(encode_input.encode()).hexdigest(),
                input_recipe_version=1,
                vector_size=EMBEDDING_DIM,
            )
        )
        articles.append(article)
    db.commit()
    return articles


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


def test_hybrid_ranker_uses_bm25_for_final_order(monkeypatch):
    articles = {
        1: CorpusArticle(1, "Beach guide", "beach food", None, ()),
        2: CorpusArticle(2, "Beach food", "beach restaurant", None, ()),
        3: CorpusArticle(3, "Ski lodge", "snow mountain", None, ()),
    }
    ranker = HybridRanker(articles=articles, blocked_targets={})
    monkeypatch.setattr(
        "app.ml.hybrid.top_candidates",
        lambda *_args, **_kwargs: [(3, 0.9), (2, 0.8)],
    )
    monkeypatch.setattr(
        "app.ml.hybrid.semantic_scores_for_targets",
        lambda *_args, target_ids, **_kwargs: {
            target_id: {2: 0.8, 3: 0.9}[target_id] for target_id in target_ids
        },
    )

    ranking = ranker.rank(object(), source_id=1, model="model", limit=2)

    assert [candidate.target_id for candidate in ranking.candidates] == [2, 3]
    assert [candidate.target_id for candidate in ranking.baseline_candidates[:2]] == [3, 2]
    assert ranking.union_count == 2


def test_pilot_site_persists_hybrid_method(db, site, monkeypatch):
    source, target, _other = _make_articles(db, site)
    site.suggestion_mode = "experimental"
    db.commit()
    monkeypatch.setattr(settings, "v1_pilot_site_ids", frozenset())
    monkeypatch.setattr(settings, "v1_shadow_site_ids", frozenset())

    class FakeRanker:
        def rank(self, _db, *, source_id, model, limit):
            del model, limit
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


def test_explicit_comparison_never_persists_suggestions(db, site, monkeypatch):
    source, target, other = _make_articles(db, site)
    calls = []

    class FakeRanker:
        def rank(self, _db, *, source_id, model, limit):
            del model, limit
            calls.append(source_id)
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
    assert result["ranking_mode"] == "shadow"
    assert result["comparison_only"] is True
    assert result["suggestions_created"] == 0


def test_shadow_site_persists_baseline_and_reports_overlap(db, site, monkeypatch):
    source, target, other = _make_articles(db, site)
    monkeypatch.setattr(settings, "v1_shadow_site_ids", frozenset({site.id}))
    monkeypatch.setattr(settings, "v1_pilot_site_ids", frozenset())

    class FakeRanker:
        def rank(self, _db, *, source_id, model, limit):
            del model, limit
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
    assert result["ranking_mode"] == "shadow"
    assert result["shadow_mean_overlap_at_5"] == 1.0
    assert result["shadow_exact_order_rate"] == 0.0


def test_shadow_sample_runs_when_existing_queue_fills_the_quota(db, site, monkeypatch):
    _make_articles(db, site)
    monkeypatch.setattr(settings, "max_suggestions_per_article", 1)
    baseline = generate_suggestions(site.id)
    assert baseline["suggestions_created"] == 3

    monkeypatch.setattr(settings, "v1_shadow_site_ids", frozenset({site.id}))
    monkeypatch.setattr(settings, "v1_pilot_site_ids", frozenset())
    monkeypatch.setattr(settings, "v1_shadow_max_sources", 2)
    calls = []

    class FakeRanker:
        def rank(self, _db, *, source_id, model, limit):
            del model
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
    monkeypatch.setattr(settings, "v1_shadow_site_ids", frozenset())
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
