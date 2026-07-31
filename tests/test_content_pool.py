"""Content pools are read-only cross-site targets, never suggestion sources."""

import hashlib
import uuid
from types import SimpleNamespace

import feedparser
import pytest
from sqlalchemy import select

from app.config import settings
from app.connectors.registry import get_connector
from app.connectors.rss_connector import RSSConnector
from app.connectors.wikipedia_connector import WikipediaConnector
from app.models import Article, Embedding, IngestionRun, Site, Suggestion
from app.models.article import EMBEDDING_DIM
from app.schemas.site import SiteCreate
from app.services.ingestion_service import _reconcile_snapshot
from app.services.suggestion_service import generate_suggestions
from app.tasks import pool_ingestion


def _vector(first: float, second: float = 0.0) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[0], vector[1] = first, second
    return vector


def _fingerprint(title: str, content: str) -> str:
    return hashlib.sha256(f"{title}\n{content}".encode()).hexdigest()


def _pool(db, *, frequency: str = "daily") -> Site:
    site = Site(
        name="News pool",
        base_url=f"https://pool-{uuid.uuid4().hex[:8]}.example.com/feed.xml",
        platform="pool",
        crawl_frequency=frequency,
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def test_pool_schema_defaults_to_daily_and_rejects_credentials():
    payload = SiteCreate(
        name="Wikipedia",
        base_url="https://en.wikipedia.org/wiki/Search_engine_optimization",
        platform="pool",
    )
    assert payload.crawl_frequency == "daily"

    with pytest.raises(ValueError, match="only valid for WordPress"):
        SiteCreate(
            name="Feed",
            base_url="https://example.com/feed.xml",
            platform="pool",
            wp_username="editor",
            wp_app_password="secret",
        )

    with pytest.raises(ValueError, match="reserved for content-pool"):
        SiteCreate(
            name="Docs",
            base_url="https://example.com/docs",
            platform="html",
            crawl_frequency="daily",
        )


def test_registry_selects_rss_or_wikipedia_connector():
    rss = Site(name="RSS", base_url="https://example.com/feed.xml", platform="pool")
    wiki = Site(
        name="Wiki",
        base_url="https://en.wikipedia.org/wiki/Link_analysis",
        platform="pool",
    )
    assert isinstance(get_connector(rss), RSSConnector)
    assert isinstance(get_connector(wiki), WikipediaConnector)


def test_rss_entry_is_normalized_without_fetching_the_article_page():
    site = Site(name="RSS", base_url="https://example.com/feed.xml", platform="pool")
    connector = RSSConnector(site)
    parsed = feedparser.parse(
        b"""<?xml version='1.0'?><rss version='2.0'><channel><title>News</title>
        <item><guid>item-1</guid><title>Useful &amp; safe</title>
        <link>https://example.com/useful</link><description><![CDATA[<p>Hello <b>world</b>.</p>]]></description>
        </item></channel></rss>"""
    )
    article = connector._to_article(parsed.entries[0], "en")
    connector.client.close()

    assert article is not None
    assert article.title == "Useful & safe"
    assert article.content_text == "Hello world ."
    assert article.external_id == "item-1"


def test_pool_routes_disallow_generation_and_publication(client, db):
    pool = _pool(db)
    try:
        response = client.get(f"/api/v1/sites/{pool.id}")
        assert response.json()["suggestion_slots_available"] == 0
        assert client.post(f"/api/v1/suggestions/{pool.id}").status_code == 409
        assert client.post(f"/api/v1/publish/{pool.id}").status_code == 409
    finally:
        db.delete(pool)
        db.commit()


def test_global_coordinator_discovers_new_daily_pool(monkeypatch, db):
    pool = _pool(db)
    queued: list[int] = []

    def fake_enqueue(_db, site_id, _kind, _fn, job_timeout):
        assert job_timeout == 3600
        queued.append(site_id)

    monkeypatch.setattr(pool_ingestion, "enqueue_job", fake_enqueue)
    try:
        assert pool_ingestion.enqueue_daily_pool_ingestions() == {
            "queued": 1,
            "skipped": 0,
            "failed": 0,
        }
        assert queued == [pool.id]
    finally:
        db.delete(pool)
        db.commit()


def test_coordinator_isolates_one_failing_source_and_keeps_going(monkeypatch, db):
    """A broken source costs its own crawl, not the whole daily run.

    The coordinator must also survive it: RQ schedules the next repeat only from
    the success path, so raising here would silently end the daily chain.
    """
    first, second = _pool(db), _pool(db)
    broken, healthy = min(first.id, second.id), max(first.id, second.id)
    alerts: list[tuple[str, dict, str, int | None]] = []

    def fake_enqueue(_db, site_id, _kind, _fn, job_timeout):
        if site_id == broken:
            raise RuntimeError("redis is down")

    monkeypatch.setattr(pool_ingestion, "enqueue_job", fake_enqueue)
    monkeypatch.setattr(
        pool_ingestion,
        "send_alert",
        lambda subject, payload, *, kind, site_id: alerts.append((subject, payload, kind, site_id)),
    )
    try:
        result = pool_ingestion.enqueue_daily_pool_ingestions()

        assert result == {"queued": 1, "skipped": 0, "failed": 1}
        assert [alert[2] for alert in alerts] == ["pool_ingestion_enqueue_failed"]
        assert alerts[0][3] == broken
        assert "redis is down" in alerts[0][1]["error"]
        assert healthy  # the later source was still reached
    finally:
        db.delete(first)
        db.delete(second)
        db.commit()


def test_coordinator_is_registered_as_a_unique_repeating_retrying_job(monkeypatch):
    """Pin the enqueue contract the daily chain depends on.

    `repeat` is what makes it recur at all, `retry` is what keeps a worker death
    from ending the chain, and `unique` + the fixed id keep re-running the
    registration script from stacking duplicate coordinators.
    """
    captured: dict = {}

    def fake_enqueue(fn, **kwargs):
        captured["fn"] = fn
        captured.update(kwargs)
        return SimpleNamespace(id=kwargs["job_id"])

    monkeypatch.setattr(pool_ingestion.ingestion_queue, "enqueue", fake_enqueue)

    job = pool_ingestion.schedule_pool_ingestion()

    assert job.id == "linkmesh-pool-daily"
    assert captured["fn"] is pool_ingestion.enqueue_daily_pool_ingestions
    assert captured["unique"] is True
    assert captured["repeat"].times == settings.pool_poll_repeat_count
    assert captured["repeat"].intervals == [settings.pool_poll_interval_seconds]
    assert captured["retry"].max == 2


def test_coordinator_reports_a_total_failure_without_ending_the_repeat_chain(monkeypatch, db):
    pool = _pool(db)
    alerts: list[tuple[str, dict, str, int | None]] = []

    def exploding_session():
        raise RuntimeError("database is unreachable")

    monkeypatch.setattr(pool_ingestion, "SessionLocal", exploding_session)
    monkeypatch.setattr(
        pool_ingestion,
        "send_alert",
        lambda subject, payload, *, kind, site_id: alerts.append((subject, payload, kind, site_id)),
    )
    try:
        # Returning rather than raising is the whole point: RQ only schedules the
        # next repeat when the job succeeds.
        result = pool_ingestion.enqueue_daily_pool_ingestions()

        assert result["queued"] == 0
        assert "database is unreachable" in result["error"]
        assert [alert[2] for alert in alerts] == ["pool_coordinator_failed"]
        assert alerts[0][3] is None
    finally:
        db.delete(pool)
        db.commit()


def test_hybrid_can_target_pool_articles_but_keeps_customer_sources(db, site, monkeypatch):
    monkeypatch.setattr(
        "app.ml.embeddings.encode",
        lambda texts: [_vector(1.0) for _text in texts],
    )
    pool = _pool(db)
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/tomato-guide",
        title="Tomato canning guide",
        content_text="tomato canning jars safety",
    )
    target = Article(
        site_id=pool.id,
        url="https://example.com/tomato-safety",
        title="Safe tomato canning",
        content_text="tomato canning jars boiling water safety",
    )
    db.add_all([source, target])
    db.flush()
    db.add_all(
        [
            Embedding(
                article_id=source.id,
                model=settings.embedding_model,
                vector=_vector(1.0),
                content_fingerprint=_fingerprint(source.title, source.content_text),
                input_recipe_version=1,
                vector_size=EMBEDDING_DIM,
            ),
            Embedding(
                article_id=target.id,
                model=settings.embedding_model,
                vector=_vector(0.8, 0.6),
                content_fingerprint=_fingerprint(target.title, target.content_text),
                input_recipe_version=1,
                vector_size=EMBEDDING_DIM,
            ),
        ]
    )
    db.commit()
    try:
        generate_suggestions(site.id)
        suggestion = db.scalar(select(Suggestion).where(Suggestion.site_id == site.id))
        assert suggestion is not None
        assert suggestion.source_article_id == source.id
        assert suggestion.target_article_id == target.id
    finally:
        db.delete(pool)
        db.commit()


def test_pool_reconciliation_expires_customer_suggestions_to_missing_targets(db, site):
    pool = _pool(db)
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/source",
        title="Source",
        content_text="source",
    )
    target = Article(
        site_id=pool.id,
        url="https://example.com/removed",
        title="Removed",
        content_text="removed",
    )
    db.add_all([source, target])
    db.flush()
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="hybrid_bm25",
        score=0.8,
        status="pending",
    )
    run = IngestionRun(site_id=pool.id, status="running")
    db.add_all([suggestion, run])
    db.commit()
    try:
        _reconcile_snapshot(db, pool.id, run.id)
        db.commit()
        db.refresh(suggestion)
        assert suggestion.status == "expired"
        assert target.is_active is False
    finally:
        db.delete(pool)
        db.commit()
