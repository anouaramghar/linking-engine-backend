from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.models import Article, ExternalLinkPolicy, Site, Suggestion, SuggestionEvent
from app.services.external_link_policy import (
    PolicyState,
    evaluate_external_url,
    external_target_context,
)


def _article(db, site: Site, *, url: str, title: str = "Article") -> Article:
    article = Article(
        site_id=site.id,
        external_id=uuid4().hex,
        url=url,
        title=title,
        content_text=f"Useful content for {title}",
        is_active=True,
    )
    db.add(article)
    db.commit()
    db.refresh(article)
    return article


def _pool(db, *, approved: bool = True, registered_days_ago: int | None = None) -> Site:
    suffix = uuid4().hex[:8]
    pool = Site(
        name=f"Pool {suffix}",
        base_url=f"https://news-{suffix}.wikipedia.org/feed.xml",
        platform="pool",
        pool_source_approved=approved,
        domain_registered_at=(
            datetime.now(UTC).date() - timedelta(days=registered_days_ago)
            if registered_days_ago is not None
            else None
        ),
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)
    return pool


def test_policy_api_returns_defaults_and_normalizes_rules(client, db, site):
    default = client.get(f"/api/v1/sites/{site.id}/external-link-policy")
    assert default.status_code == 200
    assert default.json() == {
        "site_id": site.id,
        "external_links_enabled": True,
        "require_https": True,
        "min_trust_score": 60,
        "min_domain_age_days": 0,
        "trusted_tlds": [],
        "allowlist_domains": [],
        "blocklist_domains": [],
        "competitor_domains": [],
        "owned_domain_protection": True,
        "expired_suggestions": 0,
        "updated_by": None,
        "updated_at": None,
    }

    updated = client.put(
        f"/api/v1/sites/{site.id}/external-link-policy",
        json={
            "min_trust_score": 75,
            "min_domain_age_days": 180,
            "trusted_tlds": [".ORG", "org"],
            "allowlist_domains": ["*.Trusted.Example"],
            "blocklist_domains": ["blocked.example"],
            "competitor_domains": ["Competitor.Example"],
        },
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["trusted_tlds"] == ["org"]
    assert body["allowlist_domains"] == ["trusted.example"]
    assert body["competitor_domains"] == ["competitor.example"]
    assert body["updated_by"] == "local-development"
    assert db.get(ExternalLinkPolicy, site.id).min_trust_score == 75


def test_policy_rejects_conflicting_domain_lists(client, site):
    response = client.put(
        f"/api/v1/sites/{site.id}/external-link-policy",
        json={
            "allowlist_domains": ["example.com"],
            "competitor_domains": ["EXAMPLE.COM"],
        },
    )
    assert response.status_code == 422
    assert "both allowed and blocked" in response.text


def test_trust_score_uses_https_tld_age_allowlist_and_approval(db, site):
    pool = _pool(db, registered_days_ago=400)
    policy = PolicyState(
        site_id=site.id,
        min_trust_score=80,
        trusted_tlds=("org",),
        allowlist_domains=("wikipedia.org",),
    )

    evaluation = evaluate_external_url(
        source_site=site,
        target_site=pool,
        target_url=f"https://articles.{pool.base_url.split('//', 1)[1].split('/', 1)[0]}/guide",
        policy=policy,
        owned_domains={site.base_url.split("//", 1)[1]: site.id},
    )

    assert evaluation.eligible is True
    assert evaluation.trust_score == 100
    assert evaluation.checks == {
        "https": True,
        "trusted_tld": True,
        "domain_age_days": 400,
        "allowlisted": True,
        "blocklisted": False,
        "competitor": False,
        "owned_domain": False,
        "approved_source": True,
    }
    db.delete(pool)
    db.commit()


def test_competitor_and_owned_domains_are_hard_blocks(db, site):
    competitor = _pool(db, registered_days_ago=400)
    competitor_result = evaluate_external_url(
        source_site=site,
        target_site=competitor,
        target_url="https://news.competitor.example/report",
        policy=PolicyState(
            site_id=site.id,
            min_trust_score=0,
            competitor_domains=("competitor.example",),
        ),
        owned_domains={},
    )
    assert competitor_result.eligible is False
    assert "domain is marked as a competitor" in competitor_result.reasons

    owned = Site(
        name="Other managed site",
        base_url=f"https://owned-{uuid4().hex[:8]}.example.com",
        platform="html",
    )
    db.add(owned)
    db.commit()
    owned_result = evaluate_external_url(
        source_site=site,
        target_site=competitor,
        target_url=f"{owned.base_url}/article",
        policy=PolicyState(site_id=site.id, min_trust_score=0),
        owned_domains={owned.base_url.split("//", 1)[1]: owned.id},
    )
    assert owned_result.eligible is False
    assert "domain belongs to a managed site" in owned_result.reasons

    db.delete(owned)
    db.delete(competitor)
    db.commit()


def test_policy_update_expires_blocked_suggestions_and_records_trace(client, db, site):
    pool = _pool(db)
    source = _article(db, site, url=f"{site.base_url}/source", title="Source")
    target = _article(
        db,
        pool,
        url="https://competitor.example/report",
        title="Target",
    )
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="hybrid_bm25",
        score=0.9,
        status="pending",
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)

    response = client.put(
        f"/api/v1/sites/{site.id}/external-link-policy",
        json={"competitor_domains": ["competitor.example"]},
    )

    assert response.status_code == 200
    assert response.json()["expired_suggestions"] == 1
    db.refresh(suggestion)
    assert suggestion.status == "expired"
    event = db.scalar(
        select(SuggestionEvent).where(
            SuggestionEvent.suggestion_id == suggestion.id,
            SuggestionEvent.event_type == "policy_expired",
        )
    )
    assert event.event_type == "policy_expired"
    assert event.details["external_trust"]["eligible"] is False

    db.delete(pool)
    db.commit()


def test_external_target_context_only_admits_safe_pool_articles(db, site):
    pool = _pool(db)
    safe = _article(db, pool, url="https://en.wikipedia.org/wiki/Safe", title="Safe")
    blocked = _article(
        db,
        pool,
        url="https://competitor.example/report",
        title="Blocked",
    )
    internal = _article(db, site, url=f"{site.base_url}/internal", title="Internal")
    db.add(
        ExternalLinkPolicy(
            site_id=site.id,
            min_trust_score=0,
            competitor_domains=["competitor.example"],
        )
    )
    db.commit()

    allowed, evaluations = external_target_context(db, site)

    assert internal.id in allowed
    assert safe.id in allowed
    assert blocked.id not in allowed
    assert evaluations[blocked.id].checks["competitor"] is True

    db.delete(pool)
    db.commit()


def test_policy_source_preview_reports_eligible_and_blocked_article_counts(client, db, site):
    pool = _pool(db)
    _article(db, pool, url="https://en.wikipedia.org/wiki/Safe", title="Safe")
    _article(db, pool, url="https://competitor.example/report", title="Blocked")
    client.put(
        f"/api/v1/sites/{site.id}/external-link-policy",
        json={"min_trust_score": 0, "competitor_domains": ["competitor.example"]},
    )

    response = client.get(f"/api/v1/sites/{site.id}/external-link-policy/sources")

    assert response.status_code == 200
    item = next(item for item in response.json()["items"] if item["site_id"] == pool.id)
    assert item["eligible_articles"] == 1
    assert item["blocked_articles"] == 1

    db.delete(pool)
    db.commit()


def test_pool_site_cannot_own_an_outgoing_external_policy(client, db):
    pool = _pool(db)
    response = client.get(f"/api/v1/sites/{pool.id}/external-link-policy")
    assert response.status_code == 409
    db.delete(pool)
    db.commit()
