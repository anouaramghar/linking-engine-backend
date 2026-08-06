import uuid
from datetime import datetime, timedelta, timezone

from app.models import Article, IngestionRun, InternalLink, JobRun, Suggestion


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
    assert site["article_count"] == 0
    assert site["internal_link_count"] == 0
    assert site["last_crawl_at"] is None
    assert site["suggestion_mode"] == "experimental"
    assert site["suggestion_mode_managed"] is True
    assert site["suggestion_comparison_enabled"] is False
    assert site["suggestion_slots_available"] == 0
    site_id = site["id"]

    # duplicate base_url rejected
    assert client.post("/api/v1/sites", json=payload).status_code == 409

    # get + list
    assert client.get(f"/api/v1/sites/{site_id}").status_code == 200
    assert any(s["id"] == site_id for s in client.get("/api/v1/sites").json())

    # delete requires an exact name confirmation, then 404
    bare = client.delete(f"/api/v1/sites/{site_id}")
    assert bare.status_code == 422
    wrong = client.delete(
        f"/api/v1/sites/{site_id}", params={"confirm_name": "not-the-name"}
    )
    assert wrong.status_code == 409
    assert client.get(f"/api/v1/sites/{site_id}").status_code == 200
    assert (
        client.delete(
            f"/api/v1/sites/{site_id}", params={"confirm_name": payload["name"]}
        ).status_code
        == 204
    )
    assert client.get(f"/api/v1/sites/{site_id}").status_code == 404


def test_invalid_platform_rejected(client):
    payload = _payload() | {"platform": "drupal"}
    assert client.post("/api/v1/sites", json=payload).status_code == 422


def test_site_response_includes_active_counts_and_latest_crawl(client, db, site):
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/source",
        title="source",
        content_text="source",
    )
    target = Article(
        site_id=site.id,
        url=f"{site.base_url}/target",
        title="target",
        content_text="target",
    )
    inactive = Article(
        site_id=site.id,
        url=f"{site.base_url}/inactive",
        title="inactive",
        content_text="inactive",
        is_active=False,
    )
    db.add_all([source, target, inactive])
    db.flush()
    db.add_all(
        [
            InternalLink(source_article_id=source.id, target_article_id=target.id),
            InternalLink(
                source_article_id=source.id,
                target_article_id=inactive.id,
                is_active=False,
            ),
            InternalLink(
                source_article_id=inactive.id,
                target_article_id=target.id,
                is_active=True,
            ),
        ]
    )
    db.add(
        Suggestion(
            site_id=site.id,
            source_article_id=source.id,
            target_article_id=target.id,
            method="baseline_cosine",
            score=0.8,
            status="pending",
        )
    )
    finished_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.add(
        IngestionRun(
            site_id=site.id,
            status="succeeded",
            articles_upserted=2,
            links_found=1,
            started_at=finished_at - timedelta(minutes=5),
            finished_at=finished_at,
        )
    )
    db.commit()

    listed = client.get("/api/v1/sites").json()
    item = next(candidate for candidate in listed if candidate["id"] == site.id)
    detail = client.get(f"/api/v1/sites/{site.id}").json()

    for response in (item, detail):
        assert response["article_count"] == 2
        assert response["internal_link_count"] == 1
        assert response["last_ingestion_status"] == "succeeded"
        assert datetime.fromisoformat(response["last_crawl_at"]) == finished_at
        assert response["suggestion_slots_available"] == 5


def test_analysis_state_is_reported_apart_from_the_crawl(client, db, site):
    """A crawled site and an analysed one must not look identical to the UI."""
    crawled_at = datetime.now(timezone.utc) - timedelta(hours=3)
    db.add(
        IngestionRun(
            site_id=site.id,
            status="succeeded",
            articles_upserted=1,
            started_at=crawled_at - timedelta(minutes=5),
            finished_at=crawled_at,
        )
    )
    db.commit()

    listed = client.get(f"/api/v1/sites/{site.id}").json()
    assert listed["last_ingestion_status"] == "succeeded"
    assert listed["last_analysis_status"] is None

    analysed_at = crawled_at + timedelta(hours=1)
    db.add_all(
        [
            # An in-flight run must not overwrite the last finished outcome, and a
            # failed older attempt must not outrank the successful newer one.
            JobRun(site_id=site.id, kind="analysis", status="failed", finished_at=crawled_at),
            JobRun(site_id=site.id, kind="analysis", status="succeeded", finished_at=analysed_at),
            JobRun(site_id=site.id, kind="analysis", status="running"),
            JobRun(site_id=site.id, kind="ingestion", status="succeeded", finished_at=analysed_at),
        ]
    )
    db.commit()

    detail = client.get(f"/api/v1/sites/{site.id}").json()
    item = next(
        candidate
        for candidate in client.get("/api/v1/sites").json()
        if candidate["id"] == site.id
    )
    for response in (detail, item):
        assert response["last_analysis_status"] == "succeeded"
        assert datetime.fromisoformat(response["last_analysis_at"]) == analysed_at


def test_suggestion_mode_is_global_and_cannot_be_changed(client, site):
    response = client.put(
        f"/api/v1/sites/{site.id}/suggestion-mode",
        json={"suggestion_mode": "experimental"},
    )

    assert response.status_code == 409
    assert "global suggestion method" in response.json()["detail"]


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
