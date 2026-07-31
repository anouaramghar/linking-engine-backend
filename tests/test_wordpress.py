"""WordPress connector — link extraction and publication, no network (MockTransport)."""

import json
from types import SimpleNamespace

import httpx

from app.connectors.base import TaxonomyData
from app.connectors.wordpress import WordPressConnector


def make_connector(base_url="https://example.com"):
    site = SimpleNamespace(name="wp", base_url=base_url, wp_username=None, wp_app_password=None)
    return WordPressConnector(site)


def test_internal_hrefs_resolves_relative_links():
    c = make_connector()
    content = (
        '<p><a href="/rooted/">root-relative</a>'
        '<a href="sibling/">doc-relative</a>'
        '<a href="https://example.com/abs">absolute</a>'
        '<a href="https://other.com/x">external</a>'
        '<a href="mailto:a@b.c">mail</a></p>'
    )
    assert c._internal_hrefs(content, "https://example.com/post/") == [
        "https://example.com/rooted/",
        "https://example.com/post/sibling/",
        "https://example.com/abs",
    ]


def test_resolve_internal_url_follows_redirect_alias():
    c = make_connector()
    canonical = "https://example.com/canonical/"

    def handler(request):
        if request.url.path == "/blog/legacy/":
            return httpx.Response(301, headers={"location": canonical}, request=request)
        return httpx.Response(200, request=request)

    c.client = httpx.Client(
        base_url="https://example.com",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )

    assert c.resolve_internal_url("https://example.com/blog/legacy/") == canonical


def test_to_article_handles_comment_only_rendered_content():
    c = make_connector()
    article = c._to_article(
        {
            "id": 10,
            "link": "https://example.com/dynamic-block",
            "title": {"rendered": "Dynamic block"},
            "content": {"rendered": "<!-- wp:latest-posts /-->"},
            "categories": [],
            "tags": [],
            "date_gmt": None,
        },
        {},
    )

    assert article.content_text == ""
    assert article.outbound_internal_urls == []


def test_to_article_keeps_category_and_tag_with_same_wordpress_id():
    c = make_connector()
    category = TaxonomyData(kind="category", name="News", external_id="7")
    tag = TaxonomyData(kind="tag", name="Featured", external_id="7")

    article = c._to_article(
        {
            "id": 10,
            "link": "https://example.com/post",
            "title": {"rendered": "Post"},
            "content": {"rendered": "<p>body</p>"},
            "categories": [7],
            "tags": [7],
            "date_gmt": None,
        },
        {("category", 7): category, ("tag", 7): tag},
    )

    assert article.taxonomies == [category, tag]


def test_paginate_continues_when_total_pages_header_is_missing():
    connector = make_connector()
    requested_pages = []

    def handler(request):
        page = int(request.url.params["page"])
        requested_pages.append(page)
        if page > 2:
            return httpx.Response(400, json={"code": "rest_post_invalid_page_number"})
        return httpx.Response(200, json=[{"id": page}])

    connector.client = httpx.Client(
        base_url="https://example.com", transport=httpx.MockTransport(handler)
    )

    assert list(connector._paginate("/wp-json/wp/v2/posts")) == [{"id": 1}, {"id": 2}]
    assert requested_pages == [1, 2, 3]


def _mock_publish(connector, raw_content):
    """Route GET -> post with raw_content; capture the update POST body, if any."""
    captured = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"content": {"raw": raw_content}})
        captured["content"] = json.loads(request.content)["content"]
        return httpx.Response(200, json={})

    connector.client = httpx.Client(
        base_url="https://example.com", transport=httpx.MockTransport(handler)
    )
    return captured


def _suggestion(anchor_text=None):
    return SimpleNamespace(
        source_article=SimpleNamespace(id=1, external_id="10", url="https://example.com/src"),
        target_article=SimpleNamespace(
            title="Tips & <Tricks>", url="https://example.com/t?a=1&b=2"
        ),
        anchor_text=anchor_text,
    )


def test_apply_link_escapes_html():
    c = make_connector()
    captured = _mock_publish(c, "<p>body</p>")
    c.apply_link(_suggestion())
    assert "Tips &amp; &lt;Tricks&gt;" in captured["content"]
    assert 'href="https://example.com/t?a=1&amp;b=2"' in captured["content"]


def test_apply_link_handles_comment_only_content():
    c = make_connector()
    captured = _mock_publish(c, "<!-- wp:latest-posts /-->")

    c.apply_link(_suggestion())

    assert captured["content"].startswith("<!-- wp:latest-posts /-->")
    assert "Read also" in captured["content"]


def test_apply_link_skips_only_on_exact_href():
    # exact target href present -> idempotent no-op
    c = make_connector()
    captured = _mock_publish(c, '<p><a href="https://example.com/t?a=1&b=2">already</a></p>')
    c.apply_link(_suggestion())
    assert captured == {}

    # a *prefix-sharing* href must not suppress the insert (old substring-check bug)
    c = make_connector()
    captured = _mock_publish(c, '<p><a href="https://example.com/t?a=1&b=25">other post</a></p>')
    c.apply_link(_suggestion())
    assert "Read also" in captured["content"]
