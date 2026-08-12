"""The offline ranker's candidate pool and its deliberate break from production."""

import hashlib
import math
from datetime import UTC, datetime

import pytest

from app.config import settings
from app.ml.evaluation.ranking import EvaluationRanker
from app.models import Article, Embedding, InternalLink, Site, Suggestion
from app.models.article import EMBEDDING_DIM


CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)
OLD = datetime(2025, 6, 1, tzinfo=UTC)
NEW = datetime(2026, 5, 1, tzinfo=UTC)

# The shared vocabulary every fixture article is built from, so each one reaches
# the BM25 pool and has to be removed by a rule rather than by not matching.
BODY = "tomato canning jars boiling water safety altitude"


def _vector(axis: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[axis] = 1.0
    return vector


def _similar_vector(similarity: float, axis: int) -> list[float]:
    """A unit vector whose cosine against ``SOURCE_VECTOR`` is exactly ``similarity``.

    The remainder goes on a per-article axis so the fixture's targets are not all
    near-duplicates of each other as a side effect of resembling the source.
    """
    vector = [0.0] * EMBEDDING_DIM
    vector[0] = similarity
    vector[axis] = math.sqrt(max(0.0, 1.0 - similarity**2))
    return vector


# Every source article uses this, so a target's stated similarity is its cosine
# against the source and each threshold in the ranker can be aimed at directly.
SOURCE_VECTOR = _vector(0)


def _fingerprint(title: str, content: str) -> str:
    return hashlib.sha256(f"{title}\n{content}".encode()).hexdigest()


def _site(db, suffix: str) -> Site:
    site = Site(
        name=f"Evaluation {suffix}",
        base_url=f"https://evaluation-{suffix}.example.com",
        platform="html",
    )
    db.add(site)
    db.flush()
    return site


def _article(
    db,
    site: Site,
    *,
    slug: str,
    title: str,
    published_at: datetime,
    vector: list[float] | None = None,
    embed: bool = True,
    fingerprint: str | None = None,
) -> Article:
    content = f"{BODY} {slug}"
    article = Article(
        site_id=site.id,
        external_id=slug,
        url=f"{site.base_url}/{slug}",
        title=title,
        content_text=content,
        published_at=published_at,
    )
    db.add(article)
    db.flush()
    if embed:
        db.add(
            Embedding(
                article_id=article.id,
                model=settings.embedding_model,
                vector=vector if vector is not None else _similar_vector(0.7, 1),
                content_fingerprint=fingerprint or _fingerprint(title, content),
                input_recipe_version=1,
                vector_size=EMBEDDING_DIM,
            )
        )
    return article


def _source(db, site: Site, *, title: str = "Source jars", embed: bool = True) -> Article:
    return _article(
        db,
        site,
        slug="source",
        title=title,
        published_at=NEW,
        vector=SOURCE_VECTOR,
        embed=embed,
    )


def _load(db, site: Site, source: Article) -> EvaluationRanker:
    return EvaluationRanker.load(
        db,
        site_id=site.id,
        model=settings.embedding_model,
        cutoff_at=CUTOFF,
        source_ids={source.id},
    )


def test_the_pool_holds_only_articles_that_existed_at_the_cutoff(db):
    site = _site(db, "pool")
    old = _article(db, site, slug="old", title="Old jars", published_at=OLD)
    newer = _article(
        db,
        site,
        slug="newer",
        title="Newer jars",
        published_at=datetime(2026, 3, 1, tzinfo=UTC),
        vector=_similar_vector(0.8, 2),
    )
    source = _source(db, site)
    db.commit()

    ranker = _load(db, site, source)
    ranked = ranker.rank(db, source_id=source.id)

    assert ranker.stats.pool_articles == 1
    assert ranked == [old.id]
    # It did not exist at the cutoff, so proposing it would be predicting the past.
    assert newer.id not in ranked

    db.delete(site)
    db.commit()


def test_a_target_the_source_already_links_to_is_still_ranked(db):
    """The production predicate hides these. The ground truth is made of them."""
    site = _site(db, "linked")
    target = _article(db, site, slug="target", title="Target jars", published_at=OLD)
    source = _source(db, site)
    db.flush()
    db.add_all(
        [
            InternalLink(
                source_article_id=source.id,
                target_article_id=target.id,
                first_seen_at=datetime(2026, 8, 3, tzinfo=UTC),
            ),
            Suggestion(
                site_id=site.id,
                source_article_id=source.id,
                target_article_id=target.id,
                method="hybrid_bm25",
                score=0.8,
                status="pending",
            ),
        ]
    )
    db.commit()

    ranked = _load(db, site, source).rank(db, source_id=source.id)

    assert ranked == [target.id]

    db.delete(site)
    db.commit()


def test_low_value_targets_never_enter_the_pool(db):
    site = _site(db, "low-value")
    useful = _article(db, site, slug="useful", title="Useful jars", published_at=OLD)
    _article(
        db,
        site,
        slug="by-title",
        title="Privacy Policy",
        published_at=OLD,
        vector=_similar_vector(0.7, 2),
    )
    _article(
        db,
        site,
        slug="checkout",
        title="Buy jars",
        published_at=OLD,
        vector=_similar_vector(0.7, 4),
    )
    source = _source(db, site)
    db.commit()

    ranker = _load(db, site, source)

    # One removed by its title, one by its url slug.
    assert ranker.stats.excluded_low_value == 2
    assert ranker.rank(db, source_id=source.id) == [useful.id]

    db.delete(site)
    db.commit()


def test_content_rules_reject_targets_the_pool_still_contains(db):
    site = _site(db, "duplicates")
    source_title = "Source jars"
    useful = _article(db, site, slug="useful", title="Useful jars", published_at=OLD)
    _article(
        db,
        site,
        slug="near-duplicate",
        title="Almost the same",
        published_at=OLD,
        vector=_similar_vector(0.999, 2),
    )
    _article(
        db,
        site,
        slug="same-title",
        title=source_title,
        published_at=OLD,
        vector=_similar_vector(0.7, 4),
    )
    source = _source(db, site, title=source_title)
    db.commit()

    ranker = _load(db, site, source)

    # The pool is built once for the site; these two are rejected per source by
    # the near-duplicate ceiling and by the identical title.
    assert ranker.stats.pool_articles == 3
    assert ranker.rank(db, source_id=source.id) == [useful.id]

    db.delete(site)
    db.commit()


def test_targets_below_the_minimum_score_are_not_proposed(db, monkeypatch):
    # conftest sets this to 0.0 for the whole suite, so the rule has to be put
    # back to be tested at all.
    monkeypatch.setattr(settings, "suggestion_min_score", 0.5)
    site = _site(db, "minimum-score")
    close_enough = _article(db, site, slug="useful", title="Useful jars", published_at=OLD)
    too_distant = _article(
        db,
        site,
        slug="too-distant",
        title="Unrelated jars",
        published_at=OLD,
        vector=_similar_vector(0.1, 5),
    )
    source = _source(db, site)
    db.commit()

    ranked = _load(db, site, source).rank(db, source_id=source.id)

    assert ranked == [close_enough.id]
    assert too_distant.id not in ranked

    db.delete(site)
    db.commit()


def test_a_source_without_an_embedding_ranks_nothing(db):
    site = _site(db, "unembedded")
    _article(db, site, slug="target", title="Target jars", published_at=OLD)
    source = _source(db, site, embed=False)
    db.commit()

    ranker = _load(db, site, source)

    assert ranker.stats.source_articles == 0
    assert ranker.rank(db, source_id=source.id) == []

    db.delete(site)
    db.commit()


def test_each_method_orders_the_same_candidates_its_own_way(db):
    """The comparison table is only readable if the pool is shared.

    Two targets, chosen so the two signals disagree: one is close in embedding
    space and shares no title term, the other repeats the source's title terms and
    is further away. A method that reordered the candidate set instead of the
    ordering would make the table compare two different questions.
    """
    site = _site(db, "methods")
    lexical_favourite = _article(
        db,
        site,
        slug="sourdough-guide",
        title="Sourdough jars guide",
        published_at=OLD,
        vector=_similar_vector(0.60, 2),
    )
    dense_favourite = _article(
        db,
        site,
        slug="pickles",
        title="Pickles and brine",
        published_at=OLD,
        vector=_similar_vector(0.95, 3),
    )
    source = _source(db, site, title="Sourdough jars")
    db.commit()

    ranker = _load(db, site, source)
    ranked = ranker.rank_all(db, source_id=source.id)

    assert set(ranked) == {"lexical", "dense", "hybrid"}
    assert {tuple(sorted(order)) for order in ranked.values()} == {
        tuple(sorted((lexical_favourite.id, dense_favourite.id)))
    }
    assert ranked["dense"] == [dense_favourite.id, lexical_favourite.id]
    assert ranked["lexical"] == [lexical_favourite.id, dense_favourite.id]
    # Production orders by BM25 and breaks ties on the fused rank, so the hybrid
    # row follows the lexical one here rather than the dense one.
    assert ranked["hybrid"] == [lexical_favourite.id, dense_favourite.id]

    db.delete(site)
    db.commit()


def test_rank_returns_one_method_and_refuses_a_name_it_cannot_produce(db):
    site = _site(db, "one-method")
    target = _article(db, site, slug="target", title="Target jars", published_at=OLD)
    source = _source(db, site)
    db.commit()

    ranker = _load(db, site, source)

    assert ranker.rank(db, source_id=source.id, method="dense") == [target.id]
    # The default stays hybrid: every caller written before the other methods
    # existed still measures what production ships.
    assert ranker.rank(db, source_id=source.id) == ranker.rank(
        db, source_id=source.id, method="hybrid"
    )
    with pytest.raises(ValueError, match="unsupported ranking methods"):
        ranker.rank_all(db, source_id=source.id, methods=("gnn",))

    db.delete(site)
    db.commit()


def test_a_source_without_an_embedding_ranks_nothing_under_every_method(db):
    site = _site(db, "unembedded-methods")
    _article(db, site, slug="target", title="Target jars", published_at=OLD)
    source = _source(db, site, embed=False)
    db.commit()

    ranked = _load(db, site, source).rank_all(db, source_id=source.id)

    assert ranked == {"lexical": [], "dense": [], "hybrid": []}

    db.delete(site)
    db.commit()
