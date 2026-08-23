"""Credential-free, review-gated site creation proposals."""

import uuid

from sqlalchemy import delete, func, select

from app.agent_tools import call_tool
from app.models import Site
from app.services.authorization import Principal


def _admin() -> Principal:
    return Principal(is_admin=True, source="legacy_env")


def _candidate(name: str, platform: str = "html") -> dict[str, str]:
    return {
        "name": name,
        "base_url": f"https://agent-create-{uuid.uuid4().hex[:10]}.example.com/",
        "platform": platform,
    }


def test_single_preview_normalizes_and_creates_nothing_until_confirmed(client, db):
    candidate = _candidate("  Agent Docs  ", "wordpress")
    before = db.scalar(select(func.count()).select_from(Site)) or 0

    preview = call_tool(
        db,
        _admin(),
        "preview_site_creation",
        {"sites": [candidate]},
    )

    normalized_url = candidate["base_url"].rstrip("/")
    assert preview["ready"] is True
    assert preview["credentials_included"] is False
    assert preview["sites"] == [
        {
            "name": "Agent Docs",
            "base_url": normalized_url,
            "platform": "wordpress",
            "crawl_frequency": "manual",
        }
    ]
    assert preview["proposal"] == {
        "kind": "site_create",
        "risk": "sensitive",
        "method": "POST",
        "endpoint": "/api/v1/sites",
        "payload": {
            "name": "Agent Docs",
            "base_url": normalized_url,
            "platform": "wordpress",
            "expected_absent": True,
        },
        "impact": {"site_count": 1, "wordpress_count": 1, "html_count": 0},
    }
    assert (db.scalar(select(func.count()).select_from(Site)) or 0) == before

    response = client.post("/api/v1/sites", json=preview["proposal"]["payload"])
    assert response.status_code == 201, response.text
    created_id = response.json()["id"]
    assert response.json()["has_wordpress_credentials"] is False
    db.execute(delete(Site).where(Site.id == created_id))
    db.commit()


def test_preview_refuses_credentials_and_content_pool_sources(db):
    with_credentials = _candidate("Credentialed", "wordpress") | {
        "wp_username": "editor",
        "wp_app_password": "secret",
    }
    credentials = call_tool(
        db,
        _admin(),
        "preview_site_creation",
        {"sites": [with_credentials]},
    )
    pool = call_tool(
        db,
        _admin(),
        "preview_site_creation",
        {"sites": [_candidate("Pool", "pool")]},
    )

    assert credentials["status"] == 422
    assert "Extra inputs are not permitted" in credentials["error"]
    assert pool["status"] == 422
    assert "wordpress" in pool["error"] and "html" in pool["error"]


def test_existing_normalized_url_blocks_the_proposal(db, site):
    preview = call_tool(
        db,
        _admin(),
        "preview_site_creation",
        {
            "sites": [
                {
                    "name": "Duplicate",
                    "base_url": site.base_url + "/",
                    "platform": site.platform,
                }
            ]
        },
    )

    assert preview["ready"] is False
    assert preview["conflicts"][0]["id"] == site.id
    assert "proposal" not in preview


def test_guarded_bulk_confirmation_is_atomic_when_availability_changes(client, db):
    candidates = [_candidate("First"), _candidate("Second", "wordpress")]
    preview = call_tool(
        db,
        _admin(),
        "preview_site_creation",
        {"sites": candidates},
    )
    proposal = preview["proposal"]
    assert proposal["kind"] == "site_bulk_create"
    assert proposal["payload"]["expected_absent_base_urls"] == sorted(
        candidate["base_url"].rstrip("/") for candidate in candidates
    )

    # A concurrent dashboard request claims one URL after the preview. The
    # guarded bulk route must create neither row, not return partial success.
    claimed = Site(
        name="Claimed elsewhere",
        base_url=candidates[1]["base_url"].rstrip("/"),
        platform="wordpress",
        crawl_frequency="manual",
    )
    db.add(claimed)
    db.commit()
    db.refresh(claimed)

    response = client.post("/api/v1/sites/bulk", json=proposal["payload"])
    assert response.status_code == 409
    untouched_url = candidates[0]["base_url"].rstrip("/")
    assert db.scalar(select(Site.id).where(Site.base_url == untouched_url)) is None

    db.delete(claimed)
    db.commit()
