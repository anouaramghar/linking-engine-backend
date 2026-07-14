"""WordPress connector — link extraction and publication, no network (MockTransport)."""

import json
from types import SimpleNamespace

import httpx

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
        target_article=SimpleNamespace(title="Tips & <Tricks>", url="https://example.com/t?a=1&b=2"),
        anchor_text=anchor_text,
    )


def test_apply_link_escapes_html():
    c = make_connector()
    captured = _mock_publish(c, "<p>body</p>")
    c.apply_link(_suggestion())
    assert "Tips &amp; &lt;Tricks&gt;" in captured["content"]
    assert 'href="https://example.com/t?a=1&amp;b=2"' in captured["content"]


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
