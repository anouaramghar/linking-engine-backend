"""Focused contract tests for the crawl snapshot module."""

import pytest

import app.services.crawl_snapshot as crawl_snapshot
from app.connectors.base import ArticleData, OutboundLink
from app.services.crawl_snapshot import CrawlSnapshot, normalize_url


def test_normalize_url_is_a_comparison_key_not_a_stored_url() -> None:
    assert normalize_url("HTTPS://Example.com/article/?utm_source=mail#intro") == (
        "example.com/article"
    )


def test_snapshot_stages_articles_and_resolves_forward_links(monkeypatch) -> None:
    snapshot = CrawlSnapshot(site_id=7, run_id=11)
    source = ArticleData(
        url="https://example.com/source",
        title="Source",
        content_text="source",
        outbound_internal_links=[
            OutboundLink(url="https://example.com/target#section", anchor_text="read target"),
            OutboundLink(url="https://example.com/target/", anchor_text="same target"),
        ],
    )
    target = ArticleData(
        url="http://example.com/target/",
        title="Target",
        content_text="target",
    )
    snapshot.record_accepted_article(source)
    snapshot.stage_article(source, article_id=101)
    snapshot.record_accepted_article(target)
    snapshot.stage_article(target, article_id=202)

    writes = []
    monkeypatch.setattr(
        crawl_snapshot,
        "_upsert_link",
        lambda db, source_id, target_id, run_id, anchor_text: writes.append(
            (db, source_id, target_id, run_id, anchor_text)
        ),
    )

    marker = object()
    assert snapshot.resolve_links(marker) == 1
    assert writes == [(marker, 101, 202, 11, "read target")]
    assert snapshot.article_count == 2
    assert [item.url for item in snapshot.accepted_observations] == [
        source.url,
        target.url,
    ]


def test_snapshot_requires_validation_and_link_resolution_before_promotion(monkeypatch) -> None:
    snapshot = CrawlSnapshot(site_id=7, run_id=11)
    db = object()
    promoted = []

    monkeypatch.setattr(crawl_snapshot, "_validate_snapshot_completeness", lambda *args: None)
    monkeypatch.setattr(
        crawl_snapshot,
        "_reconcile_snapshot",
        lambda *args: promoted.append(args),
    )

    with pytest.raises(RuntimeError, match="completeness"):
        snapshot.promote(db)

    snapshot.validate_completeness(db)
    with pytest.raises(RuntimeError, match="links"):
        snapshot.promote(db)

    assert snapshot.resolve_links(db) == 0
    snapshot.promote(db)
    assert promoted == [(db, 7, 11)]


def test_snapshot_can_record_acceptance_before_persistence() -> None:
    snapshot = CrawlSnapshot(site_id=7, run_id=11)
    article = ArticleData(
        url="https://example.com/article",
        title="Article",
        content_text="content",
    )

    snapshot.record_accepted_article(article)

    assert [item.url for item in snapshot.accepted_observations] == [article.url]
