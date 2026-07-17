import uuid

from app.models import Article, InternalLink


def _payload():
    return {
        "name": "pytest site",
        "base_url": f"https://pytest-{uuid.uuid4().hex[:8]}.example.com/",
        "platform": "wordpress",
    }


def test_site_crud(client):
    # create — trailing slash normalized
    payload = _payload()
    resp = client.post("/api/v1/sites", json=payload)
    assert resp.status_code == 201, resp.text
    site = resp.json()
    assert site["base_url"] == payload["base_url"].rstrip("/")
    assert site["last_ingestion_status"] is None
    site_id = site["id"]

    # duplicate base_url rejected
    assert client.post("/api/v1/sites", json=payload).status_code == 409

    # get + list
    assert client.get(f"/api/v1/sites/{site_id}").status_code == 200
    assert any(s["id"] == site_id for s in client.get("/api/v1/sites").json())

    # delete, then 404
    assert client.delete(f"/api/v1/sites/{site_id}").status_code == 204
    assert client.get(f"/api/v1/sites/{site_id}").status_code == 404


def test_invalid_platform_rejected(client):
    payload = _payload() | {"platform": "drupal"}
    assert client.post("/api/v1/sites", json=payload).status_code == 422


def test_orphan_filter_ignores_expired_links(client, db, site):
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/source",
        title="source",
        content_text="source",
    )
    expired_target = Article(
        site_id=site.id,
        url=f"{site.base_url}/expired-target",
        title="expired target",
        content_text="expired target",
    )
    current_target = Article(
        site_id=site.id,
        url=f"{site.base_url}/current-target",
        title="current target",
        content_text="current target",
    )
    inactive_article = Article(
        site_id=site.id,
        url=f"{site.base_url}/inactive",
        title="inactive",
        content_text="inactive",
        is_active=False,
    )
    db.add_all([source, expired_target, current_target, inactive_article])
    db.flush()
    expired_link = InternalLink(
        source_article_id=source.id,
        target_article_id=expired_target.id,
    )
    expired_link.is_active = False
    db.add_all(
        [
            expired_link,
            InternalLink(
                source_article_id=source.id,
                target_article_id=current_target.id,
            ),
        ]
    )
    db.commit()

    current_response = client.get(f"/api/v1/sites/{site.id}/articles")
    response = client.get(f"/api/v1/sites/{site.id}/articles", params={"orphans": True})

    assert current_response.status_code == 200
    assert response.status_code == 200
    current_ids = {article["id"] for article in current_response.json()}
    orphan_ids = {article["id"] for article in response.json()}
    assert inactive_article.id not in current_ids
    assert inactive_article.id not in orphan_ids
    assert expired_target.id in orphan_ids
    assert current_target.id not in orphan_ids
