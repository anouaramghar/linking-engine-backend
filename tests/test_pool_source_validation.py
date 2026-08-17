import uuid

from sqlalchemy import select

from app.api.routes import sites as site_routes
from app.config import settings
from app.models import Site
from app.services.pool_source_policy import PoolSourceFetchError
from app.services.pool_source_validation import classify_pool_source

VALIDATE_URL = "/api/v1/sites/pool-source/validate"


def test_validation_probes_wikipedia_and_rss_without_creating_sources(client, db, monkeypatch):
    monkeypatch.setattr(settings, "pool_allowed_domains", "wikipedia.org,example.com")
    probed: list[str] = []

    def probe(site: Site):
        probed.append(site.base_url)
        return classify_pool_source(site.base_url)

    monkeypatch.setattr(site_routes, "probe_pool_source", probe)
    rows = [
        {
            "name": "Wikipedia AI",
            "base_url": "https://en.wikipedia.org/wiki/Artificial_intelligence",
        },
        {"name": "Industry feed", "base_url": "https://news.example.com/feed.xml"},
    ]

    responses = [client.post(VALIDATE_URL, json=row) for row in rows]

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json()["source_type"] for response in responses] == [
        "wikipedia",
        "rss_atom",
    ]
    assert all(response.json()["valid"] for response in responses)
    assert probed == [row["base_url"] for row in rows]
    assert db.scalars(select(Site).where(Site.base_url.in_(probed))).all() == []


def test_validation_rejects_a_domain_outside_the_pool_allowlist(client, monkeypatch):
    monkeypatch.setattr(settings, "pool_allowed_domains", "wikipedia.org")

    def unexpected_probe(_site: Site):
        raise AssertionError("a disallowed source must never be fetched")

    monkeypatch.setattr(site_routes, "probe_pool_source", unexpected_probe)

    response = client.post(
        VALIDATE_URL,
        json={"name": "News", "base_url": "https://news.example.com/feed.xml"},
    )

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert "POOL_ALLOWED_DOMAINS" in response.json()["reason"]


def test_validation_reports_an_existing_source_before_fetching(client, monkeypatch):
    monkeypatch.setattr(settings, "pool_allowed_domains", "example.com")
    base_url = f"https://existing-{uuid.uuid4().hex}.example.com/feed.xml"
    created = client.post(
        "/api/v1/sites",
        json={"name": "Existing", "base_url": base_url, "platform": "pool"},
    )
    assert created.status_code == 201, created.text

    def unexpected_probe(_site: Site):
        raise AssertionError("an existing source must never be fetched")

    monkeypatch.setattr(site_routes, "probe_pool_source", unexpected_probe)

    response = client.post(
        VALIDATE_URL,
        json={"name": "Existing again", "base_url": base_url},
    )

    assert response.status_code == 200
    assert response.json() == {
        "base_url": base_url,
        "valid": False,
        "source_type": "rss_atom",
        "reason": "a site with this base_url already exists",
    }


def test_validation_returns_a_readable_remote_failure(client, monkeypatch):
    monkeypatch.setattr(settings, "pool_allowed_domains", "example.com")

    def failed_probe(_site: Site):
        raise PoolSourceFetchError("invalid RSS/Atom feed: missing feed version")

    monkeypatch.setattr(site_routes, "probe_pool_source", failed_probe)

    response = client.post(
        VALIDATE_URL,
        json={"name": "Broken feed", "base_url": "https://broken.example.com/feed.xml"},
    )

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["reason"] == "invalid RSS/Atom feed: missing feed version"
