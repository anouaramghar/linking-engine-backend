"""Global Hybrid generation must never take rows away from an editor.

Two separate promises are covered here:

* normal generation never expires or hides an existing suggestion — it only
  fills slots a source has free, and clearing the queue is a deliberate,
  site-scoped operator action instead (`scripts.expire_pending_suggestions`);
* a ranking or index failure degrades to the baseline path and cannot take the
  existing queue down with it.
"""

import hashlib
import math

import pytest
from sqlalchemy import func, select, text

from app.config import settings
from app.models import Article, Embedding, Suggestion
from app.models.article import EMBEDDING_DIM
from app.services.suggestion_service import generate_suggestions
from scripts.expire_pending_suggestions import main as expire_main

LEXICAL_BODY = "tomato canning jars boiling water safety altitude pressure"

#: Every status that records a decision somebody made.
REVIEWED_STATUSES = ("approved", "rejected", "applying", "applied")


def _vector(direction: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[direction] = 1.0
    return vector


def _similar_vector(similarity: float, axis: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[0] = similarity
    vector[axis] = math.sqrt(max(0.0, 1.0 - similarity**2))
    return vector


@pytest.fixture(autouse=True)
def valid_dimension_probe(monkeypatch):
    monkeypatch.setattr(
        "app.ml.embeddings.encode",
        lambda texts: [_vector(0) for _text in texts],
    )


@pytest.fixture
def pilot_site(db, site):
    """A Hybrid site with one source and seven targets."""
    articles = []
    for index in range(8):
        title = f"Canning topic {index}"
        content = f"{LEXICAL_BODY} topic{index}"
        article = Article(
            site_id=site.id,
            url=f"{site.base_url}/topic-{index}",
            title=title,
            content_text=content,
        )
        db.add(article)
        db.flush()
        db.add(
            Embedding(
                article_id=article.id,
                model=settings.embedding_model,
                vector=_vector(0) if index == 0 else _similar_vector(0.9 - index * 0.05, index),
                content_fingerprint=hashlib.sha256(f"{title}\n{content}".encode()).hexdigest(),
                input_recipe_version=1,
                vector_size=EMBEDDING_DIM,
            )
        )
        articles.append(article)
    db.commit()
    return site, articles[0], articles[1:]


@pytest.fixture
def site_factory(db):
    """A second, independent site — the blast radius the expiry script must not reach."""
    import uuid

    from app.models import Site

    created = []

    def _make():
        second = Site(
            name="second-pilot-site",
            base_url=f"https://second-{uuid.uuid4().hex[:8]}.example.com",
            platform="html",
        )
        db.add(second)
        db.commit()
        created.append(second)
        articles = []
        for index in range(3):
            title = f"Second site topic {index}"
            article = Article(
                site_id=second.id,
                url=f"{second.base_url}/topic-{index}",
                title=title,
                content_text=LEXICAL_BODY,
            )
            db.add(article)
            db.flush()
            db.add(
                Embedding(
                    article_id=article.id,
                    model=settings.embedding_model,
                    vector=_similar_vector(0.5, index + 1),
                    content_fingerprint=hashlib.sha256(
                        f"{title}\n{LEXICAL_BODY}".encode()
                    ).hexdigest(),
                    input_recipe_version=1,
                    vector_size=EMBEDDING_DIM,
                )
            )
            articles.append(article)
        db.commit()
        return second, articles[0], articles[1:]

    yield _make
    for second in created:
        db.delete(second)
        db.commit()


def _add_suggestion(db, site, source, target, *, status, method="baseline_cosine", score=0.8):
    row = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method=method,
        score=score,
        status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _statuses(db, site) -> dict[str, int]:
    return dict(
        db.execute(
            select(Suggestion.status, func.count())
            .where(Suggestion.site_id == site.id)
            .group_by(Suggestion.status)
        ).all()
    )


# --- generation never replaces the existing queue (correction 2) -------------


def test_hybrid_generation_never_expires_existing_pending_cosine_rows(db, pilot_site):
    site, source, targets = pilot_site
    existing = [
        _add_suggestion(db, site, source, target, status="pending") for target in targets[:3]
    ]

    generate_suggestions(site.id)

    for row in existing:
        db.refresh(row)
        assert row.status == "pending", "an existing pending row was expired by generation"
        assert row.method == "baseline_cosine"
    assert db.scalar(
        select(func.count()).select_from(Suggestion).where(Suggestion.status == "expired")
    ) == 0


def test_hybrid_fills_only_the_slots_a_source_has_free(db, pilot_site, monkeypatch):
    """Two of three slots are taken, so at most one new row may appear."""
    site, source, targets = pilot_site
    monkeypatch.setattr(settings, "hybrid_max_suggestions_per_article", 3)
    for target in targets[:2]:
        _add_suggestion(db, site, source, target, status="pending")

    generate_suggestions(site.id)

    from_source = db.scalars(
        select(Suggestion).where(Suggestion.source_article_id == source.id)
    ).all()
    assert len(from_source) == 3
    created = [row for row in from_source if row.method == "hybrid_bm25"]
    assert len(created) == 1


def test_a_full_queue_produces_nothing_rather_than_making_room(db, pilot_site, monkeypatch):
    site, source, targets = pilot_site
    monkeypatch.setattr(settings, "hybrid_max_suggestions_per_article", 2)
    for target in targets[:2]:
        _add_suggestion(db, site, source, target, status="pending")
    before = _statuses(db, site)

    generate_suggestions(site.id)

    from_source = db.scalars(
        select(Suggestion).where(Suggestion.source_article_id == source.id)
    ).all()
    assert len(from_source) == 2
    assert {row.method for row in from_source} == {"baseline_cosine"}
    assert before["pending"] <= _statuses(db, site)["pending"]


@pytest.mark.parametrize("status", REVIEWED_STATUSES)
def test_generation_leaves_every_reviewed_status_untouched(db, pilot_site, status):
    site, source, targets = pilot_site
    reviewed = _add_suggestion(db, site, source, targets[0], status=status)

    generate_suggestions(site.id)

    db.refresh(reviewed)
    assert reviewed.status == status


# --- the explicit, site-scoped replacement operation (correction 2) ----------


def test_the_expiry_script_reports_without_changing_anything_by_default(db, pilot_site, capsys):
    site, source, targets = pilot_site
    pending = [
        _add_suggestion(db, site, source, target, status="pending") for target in targets[:2]
    ]

    assert expire_main(["--site-id", str(site.id), "--method", "baseline_cosine"]) == 0

    assert "would expire 2 pending row(s)" in capsys.readouterr().out
    for row in pending:
        db.refresh(row)
        assert row.status == "pending"


def test_the_expiry_script_expires_only_pending_rows_of_the_named_site(db, pilot_site, capsys):
    site, source, targets = pilot_site
    pending = _add_suggestion(db, site, source, targets[0], status="pending")
    reviewed = {
        status: _add_suggestion(db, site, source, target, status=status)
        for status, target in zip(REVIEWED_STATUSES, targets[1:])
    }

    assert expire_main(["--site-id", str(site.id), "--method", "all", "--yes"]) == 0

    capsys.readouterr()
    db.refresh(pending)
    assert pending.status == "expired"
    for status, row in reviewed.items():
        db.refresh(row)
        assert row.status == status, f"{status} row must survive an explicit expiry"


def test_the_expiry_script_can_target_one_method(db, pilot_site):
    site, source, targets = pilot_site
    cosine = _add_suggestion(db, site, source, targets[0], status="pending")
    hybrid = _add_suggestion(
        db, site, source, targets[1], status="pending", method="hybrid_bm25"
    )

    assert expire_main(["--site-id", str(site.id), "--method", "hybrid_bm25", "--yes"]) == 0

    db.refresh(cosine)
    db.refresh(hybrid)
    assert cosine.status == "pending"
    assert hybrid.status == "expired"


def test_the_expiry_script_never_reaches_another_site(db, pilot_site, site_factory):
    site, source, targets = pilot_site
    other_site, other_source, other_targets = site_factory()
    mine = _add_suggestion(db, site, source, targets[0], status="pending")
    theirs = _add_suggestion(db, other_site, other_source, other_targets[0], status="pending")

    assert expire_main(["--site-id", str(site.id), "--method", "all", "--yes"]) == 0

    db.refresh(mine)
    db.refresh(theirs)
    assert mine.status == "expired"
    assert theirs.status == "pending"


def test_the_expiry_script_has_no_fleet_wide_form():
    """`--site-id` is required, so "every site" cannot be reached by omission."""
    with pytest.raises(SystemExit):
        expire_main(["--method", "all", "--yes"])


def test_the_expiry_script_reports_an_unknown_site(capsys):
    assert expire_main(["--site-id", "999999999", "--method", "all", "--yes"]) == 1
    assert "not found" in capsys.readouterr().err


# --- a ranking failure cannot expire or hide rows (correction 9) -------------


def test_an_index_failure_leaves_the_existing_queue_intact(db, pilot_site, monkeypatch):
    site, source, targets = pilot_site
    existing = {
        "pending": _add_suggestion(db, site, source, targets[0], status="pending"),
        "approved": _add_suggestion(db, site, source, targets[1], status="approved"),
        "rejected": _add_suggestion(db, site, source, targets[2], status="rejected"),
    }
    monkeypatch.setattr(
        "app.services.suggestion_service.HybridRanker.load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("index build failed")),
    )

    result = generate_suggestions(site.id)

    assert result["hybrid_ranker_loaded"] is False
    for status, row in existing.items():
        db.refresh(row)
        assert row.status == status, f"the {status} row changed when the index failed"


def test_a_load_sql_error_rolls_back_before_baseline_fallback(
    db, pilot_site, monkeypatch
):
    """A caught PostgreSQL error must not poison the fallback transaction."""
    site, source, targets = pilot_site
    existing = {
        "pending": _add_suggestion(db, site, source, targets[0], status="pending"),
        "approved": _add_suggestion(db, site, source, targets[1], status="approved"),
    }

    def fail_load(session, **_kwargs):
        session.execute(text("SELECT codex_missing_load_column FROM suggestions"))

    monkeypatch.setattr(
        "app.services.suggestion_service.HybridRanker.load",
        fail_load,
    )

    result = generate_suggestions(site.id)

    assert result["hybrid_ranker_loaded"] is False
    assert result["hybrid_fallback_sources"] == result["eligible_sources"]
    assert result["suggestions_created"] > 0
    for status, row in existing.items():
        db.refresh(row)
        assert row.status == status
    assert {
        row.method
        for row in db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    } == {"baseline_cosine"}


def test_a_per_source_ranking_failure_leaves_the_existing_queue_intact(
    db, pilot_site, monkeypatch
):
    site, source, targets = pilot_site
    existing = {
        "pending": _add_suggestion(db, site, source, targets[0], status="pending"),
        "applied": _add_suggestion(db, site, source, targets[1], status="applied"),
    }

    class ExplodingRanker:
        def rank(self, *_args, **_kwargs):
            raise RuntimeError("ranking failed for this source")

    monkeypatch.setattr(
        "app.services.suggestion_service.HybridRanker.load",
        lambda *_args, **_kwargs: ExplodingRanker(),
    )

    result = generate_suggestions(site.id)

    assert result["hybrid_fallback_sources"] > 0
    for status, row in existing.items():
        db.refresh(row)
        assert row.status == status
    # The failure fell back rather than producing nothing.
    assert result["suggestions_created"] > 0
    assert {
        row.method
        for row in db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    } == {"baseline_cosine"}


def test_a_ranking_sql_error_rolls_back_before_baseline_fallback(
    db, pilot_site, monkeypatch
):
    """Per-source SQL failures recover just like Python ranking failures."""
    site, source, targets = pilot_site
    existing = {
        "pending": _add_suggestion(db, site, source, targets[0], status="pending"),
        "rejected": _add_suggestion(db, site, source, targets[1], status="rejected"),
    }

    class SqlExplodingRanker:
        def rank(self, session, **_kwargs):
            session.execute(text("SELECT codex_missing_rank_column FROM suggestions"))

    monkeypatch.setattr(
        "app.services.suggestion_service.HybridRanker.load",
        lambda *_args, **_kwargs: SqlExplodingRanker(),
    )

    result = generate_suggestions(site.id)

    assert result["hybrid_fallback_sources"] > 0
    assert result["suggestions_created"] > 0
    for status, row in existing.items():
        db.refresh(row)
        assert row.status == status
    assert {
        row.method
        for row in db.scalars(select(Suggestion).where(Suggestion.site_id == site.id)).all()
    } == {"baseline_cosine"}


def test_a_ranking_failure_does_not_hide_rows_from_the_queue(db, pilot_site, client, monkeypatch):
    """"Hidden" means invisible to the endpoints the dashboard actually reads."""
    site, source, targets = pilot_site
    visible = _add_suggestion(db, site, source, targets[0], status="pending")

    class ExplodingRanker:
        def rank(self, *_args, **_kwargs):
            raise RuntimeError("ranking failed")

    monkeypatch.setattr(
        "app.services.suggestion_service.HybridRanker.load",
        lambda *_args, **_kwargs: ExplodingRanker(),
    )
    generate_suggestions(site.id)

    listed = client.get("/api/v1/suggestions", params={"site_id": site.id}).json()
    counts = client.get("/api/v1/suggestions/counts", params={"site_id": site.id}).json()

    assert visible.id in {row["id"] for row in listed["items"]}
    assert counts["pending"] >= 1
    assert counts["expired"] == 0
