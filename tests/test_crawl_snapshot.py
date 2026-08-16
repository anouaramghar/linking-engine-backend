"""Focused contract tests for the crawl snapshot module."""

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
    snapshot.stage_article(source, article_id=101)
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
