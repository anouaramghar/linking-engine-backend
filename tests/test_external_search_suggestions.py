from uuid import uuid4

from app.domain_policy import domain_from_url
from app.models import Article, Suggestion
from app.services.external_link_policy import PolicyState, evaluate_web_search_url


def _source_article(db, site) -> Article:
    article = Article(
        site_id=site.id,
        external_id=uuid4().hex,
        url=f"{site.base_url}/source",
        title="Internal linking strategy",
        content_text="A guide to building useful links between related pages.",
    )
    db.add(article)
    db.commit()
    db.refresh(article)
    return article


def test_direct_external_suggestion_is_exposed_as_web_search(client, db, site) -> None:
    source = _source_article(db, site)
    safety = {
        "domain": "reference.example",
        "eligible": True,
        "reasons": [],
        "checks": {
            "https": True,
            "blocklisted": False,
            "competitor": False,
            "owned_domain": False,
        },
    }
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=None,
        external_url="https://reference.example/internal-links",
        external_title="External linking reference",
        external_snippet="Independent guidance about internal and external links.",
        provider="tavily",
        provider_request_id="request-123",
        provider_score=0.91,
        search_query=source.title,
        method="external_search",
        score=0.82,
        rank_score=0.82,
        score_components={"external_safety": safety},
        status="pending",
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)

    response = client.get(
        "/api/v1/suggestions",
        params={"target_origin": "web_search", "q": "external linking"},
    )

    assert response.status_code == 200
    item = next(row for row in response.json()["items"] if row["id"] == suggestion.id)
    assert item["target_origin"] == "web_search"
    assert item["target_article"] == {
        "id": None,
        "title": "External linking reference",
        "url": "https://reference.example/internal-links",
    }
    assert item["target_site_name"] == "Tavily"
    assert item["provider"] == "tavily"
    assert item["provider_request_id"] == "request-123"
    assert item["provider_score"] == 0.91
    assert item["search_query"] == source.title
    assert item["external_snippet"].startswith("Independent guidance")

    internal = client.get(
        "/api/v1/suggestions",
        params={"target_origin": "internal", "include_total": True},
    )
    assert internal.status_code == 200
    assert all(row["id"] != suggestion.id for row in internal.json()["items"])


def test_web_search_safety_applies_only_approved_hard_guards(site) -> None:
    policy = PolicyState(
        site_id=site.id,
        external_links_enabled=True,
        blocklist_domains=("blocked.example",),
        competitor_domains=("competitor.example",),
    )
    owned = {domain_from_url(site.base_url): site.id}

    allowed = evaluate_web_search_url(
        source_site=site,
        target_url="https://reference.example/guide",
        policy=policy,
        owned_domains=owned,
    )
    assert allowed.eligible is True
    assert allowed.checks["https"] is True

    for url, reason in (
        ("http://reference.example/guide", "HTTPS"),
        ("https://blocked.example/guide", "blocklisted"),
        ("https://competitor.example/guide", "competitor"),
        (f"{site.base_url}/owned", "managed site"),
    ):
        evaluation = evaluate_web_search_url(
            source_site=site,
            target_url=url,
            policy=policy,
            owned_domains=owned,
        )
        assert evaluation.eligible is False
        assert any(reason in item for item in evaluation.reasons)
