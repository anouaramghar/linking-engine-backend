import uuid
from datetime import datetime, timedelta, timezone

from app.models import Article, IngestionRun, InternalLink, JobRun, Site, Suggestion


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
    wrong = client.delete(f"/api/v1/sites/{site_id}", params={"confirm_name": "not-the-name"})
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
            rank_score=0.8,
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


def test_a_failed_crawl_reports_why_on_the_row(client, db, site):
    """ "Crawl failed" with no reason sends the operator to the engine logs."""
    db.add(
        IngestionRun(
            site_id=site.id,
            status="failed",
            error="  connection to https://vibe.com timed out\nafter 30s  ",
        )
    )
    db.commit()

    listed = client.get("/api/v1/sites").json()
    item = next(candidate for candidate in listed if candidate["id"] == site.id)
    detail = client.get(f"/api/v1/sites/{site.id}").json()

    for response in (item, detail):
        assert response["last_ingestion_status"] == "failed"
        assert response["last_ingestion_error"] == (
            "connection to https://vibe.com timed out after 30s"
        )


def test_a_succeeded_crawl_carries_no_failure_message(client, db, site):
    db.add(IngestionRun(site_id=site.id, status="succeeded", error="a stale message"))
    db.commit()

    assert client.get(f"/api/v1/sites/{site.id}").json()["last_ingestion_error"] is None


def test_a_long_failure_message_is_trimmed_for_the_row(client, db, site):
    db.add(IngestionRun(site_id=site.id, status="failed", error="x" * 1000))
    db.commit()

    reported = client.get(f"/api/v1/sites/{site.id}").json()["last_ingestion_error"]

    assert len(reported) == 300
    assert reported.endswith("…")


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
        candidate for candidate in client.get("/api/v1/sites").json() if candidate["id"] == site.id
    )
    for response in (detail, item):
        assert response["last_analysis_status"] == "succeeded"
        assert datetime.fromisoformat(response["last_analysis_at"]) == analysed_at


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


def _unauthenticated_site(client, db):
    """A site created through the API, so it starts with no account attached."""
    site_id = client.post("/api/v1/sites", json=_payload()).json()["id"]
    return db.get(Site, site_id)


def test_wordpress_credentials_can_be_set_rotated_and_cleared(client, db):
    """A site with a dead application password must be repairable in place.

    Before this route existed the only way to change one was to delete the site,
    which also deleted its articles, its internal links, and its review history.
    """
    site = _unauthenticated_site(client, db)
    assert client.get(f"/api/v1/sites/{site.id}").json()["has_wordpress_credentials"] is False

    created = client.put(
        f"/api/v1/sites/{site.id}/credentials",
        json={"wp_username": "editor", "wp_app_password": "abcd efgh ijkl mnop"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["has_wordpress_credentials"] is True

    # Rotation is the same call, and never has to prove the old value: WordPress
    # stores a hash, and the password is being replaced precisely because it stopped
    # working.
    rotated = client.put(
        f"/api/v1/sites/{site.id}/credentials",
        json={"wp_username": "editor", "wp_app_password": "qrst uvwx yzab cdef"},
    )
    assert rotated.status_code == 200, rotated.text
    db.refresh(site)
    assert site.wp_app_password == "qrst uvwx yzab cdef"

    cleared = client.delete(f"/api/v1/sites/{site.id}/credentials")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["has_wordpress_credentials"] is False
    db.refresh(site)
    assert site.wp_username is None
    assert site.wp_app_password is None


def test_credentials_require_both_halves_and_a_wordpress_site(client, db):
    site = _unauthenticated_site(client, db)
    partial = client.put(
        f"/api/v1/sites/{site.id}/credentials",
        json={"wp_username": "editor"},
    )
    assert partial.status_code == 422

    site.platform = "html"
    db.commit()
    wrong_platform = client.put(
        f"/api/v1/sites/{site.id}/credentials",
        json={"wp_username": "editor", "wp_app_password": "abcd efgh ijkl mnop"},
    )
    assert wrong_platform.status_code == 409
    db.refresh(site)
    assert site.wp_username is None
