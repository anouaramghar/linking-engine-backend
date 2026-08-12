"""Per-tenant site isolation and database API keys.

Phase 2 tests talk to the shared test database like every other module, so they
leave no trace behind: a leaked ApiKey row flips `test_missing_api_key_fails_closed`
from 503 to 401, and a leaked tenant slug breaks every rerun. `_tenant` is a
context manager that purges owned sites before the tenant itself — the site FK is
ON DELETE RESTRICT, so cleaning up in the other order raises and strands the very
rows this warning is about.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import require_api_key
from app.config import settings
from app.main import app
from app.models import (
    Alert,
    ApiKey,
    Article,
    PipelineBatch,
    PipelineSiteRun,
    Site,
    Suggestion,
    SuggestionEvent,
    Tenant,
)
from app.services.authorization import (
    LAST_USED_REFRESH,
    Principal,
    generate_api_key,
    hash_api_key,
)

ISOLATION_METHOD = "gnn_graphsage"


@pytest.fixture
def real_auth(monkeypatch):
    """Swap the suite-wide admin override for the production dependency."""
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "operator_api_keys", {})
    monkeypatch.setattr(settings, "api_key_pepper", "test-pepper")
    app.dependency_overrides.pop(require_api_key, None)
    yield TestClient(app)
    app.dependency_overrides[require_api_key] = lambda: Principal(
        is_admin=True, source="legacy_env"
    )


def _purge_tenant(db, slug: str) -> None:
    tenant = db.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        return
    for site in db.scalars(select(Site).where(Site.tenant_id == tenant.id)):
        db.delete(site)
    db.flush()
    db.delete(tenant)
    db.commit()


@contextmanager
def _tenant(db, name: str, *, mint: bool = True):
    """A tenant with an optional key, purged with its sites on the way out."""
    slug = f"t-{name}"
    _purge_tenant(db, slug)
    tenant = Tenant(slug=slug, name=name)
    db.add(tenant)
    db.flush()
    plaintext = None
    if mint:
        plaintext, prefix, secret_hash = generate_api_key()
        db.add(
            ApiKey(
                tenant_id=tenant.id,
                prefix=prefix,
                secret_hash=secret_hash,
                name=name,
                is_admin=False,
            )
        )
    db.commit()
    try:
        yield tenant, plaintext
    finally:
        _purge_tenant(db, slug)


def _site(db, tenant, *, platform="html", name=None, approved_pool=False) -> Site:
    site = Site(
        name=name or f"{tenant.slug}-site",
        base_url=f"https://{uuid.uuid4().hex[:10]}.example.com",
        platform=platform,
        tenant_id=tenant.id,
        pool_source_approved=approved_pool,
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def _suggestion(db, site: Site, status: str = "pending") -> Suggestion:
    articles = [
        Article(
            site_id=site.id,
            url=f"{site.base_url}/{role}-{uuid.uuid4().hex[:8]}",
            title=role,
            content_text=role,
        )
        for role in ("src", "tgt")
    ]
    db.add_all(articles)
    db.flush()
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=articles[0].id,
        target_article_id=articles[1].id,
        method=ISOLATION_METHOD,
        score=0.9,
        status=status,
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    return suggestion


# --------------------------------------------------------------------------
# Principals
# --------------------------------------------------------------------------


def test_legacy_api_key_is_admin(monkeypatch, db):
    monkeypatch.setattr(settings, "api_key", "legacy-admin")
    monkeypatch.setattr(settings, "operator_api_keys", {})
    app.dependency_overrides.pop(require_api_key, None)
    try:
        client = TestClient(app)
        assert client.get("/api/v1/sites", headers={"X-API-Key": "legacy-admin"}).status_code == 200
    finally:
        app.dependency_overrides[require_api_key] = lambda: Principal(
            is_admin=True, source="legacy_env"
        )


def test_tenant_key_cannot_read_foreign_site(real_auth, db):
    with _tenant(db, "alpha") as (_a, key_a), _tenant(db, "beta") as (tenant_b, _):
        site_b = _site(db, tenant_b)
        headers = {"X-API-Key": key_a}

        listed = real_auth.get("/api/v1/sites", headers=headers)
        assert listed.status_code == 200
        assert site_b.id not in {row["id"] for row in listed.json()}

        assert real_auth.get(f"/api/v1/sites/{site_b.id}", headers=headers).status_code == 403


def test_tenant_key_can_manage_own_site(real_auth, db):
    with _tenant(db, "owner") as (tenant, key):
        headers = {"X-API-Key": key}
        created = real_auth.post(
            "/api/v1/sites",
            headers=headers,
            json={
                "name": "owned",
                "base_url": "https://owned-isolation.example.com",
                "platform": "html",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["tenant_id"] == tenant.id
        site_id = created.json()["id"]

        assert real_auth.get(f"/api/v1/sites/{site_id}", headers=headers).status_code == 200
        deleted = real_auth.delete(
            f"/api/v1/sites/{site_id}",
            headers=headers,
            params={"confirm_name": "owned"},
        )
        assert deleted.status_code == 204


# --------------------------------------------------------------------------
# Key lifecycle
# --------------------------------------------------------------------------


def test_admin_mints_and_revokes_tenant_key(monkeypatch, db):
    monkeypatch.setattr(settings, "api_key", "legacy-admin")
    monkeypatch.setattr(settings, "operator_api_keys", {})
    monkeypatch.setattr(settings, "api_key_pepper", "test-pepper")
    app.dependency_overrides.pop(require_api_key, None)
    _purge_tenant(db, "minted")
    admin = {"X-API-Key": "legacy-admin"}

    try:
        client = TestClient(app)
        tenant = client.post(
            "/api/v1/admin/tenants",
            headers=admin,
            json={"slug": "minted", "name": "Minted Co"},
        )
        assert tenant.status_code == 201, tenant.text
        tenant_id = tenant.json()["id"]

        minted = client.post(
            "/api/v1/admin/api-keys",
            headers=admin,
            json={"name": "minted-key", "tenant_id": tenant_id, "is_admin": False},
        )
        assert minted.status_code == 201, minted.text
        payload = minted.json()
        assert payload["api_key"].startswith("lm_")
        assert "secret_hash" not in payload
        raw = payload["api_key"]

        assert client.get("/api/v1/sites", headers={"X-API-Key": raw}).status_code == 200

        revoked = client.post(f"/api/v1/admin/api-keys/{payload['id']}/revoke", headers=admin)
        assert revoked.status_code == 200
        assert client.get("/api/v1/sites", headers={"X-API-Key": raw}).status_code == 401
    finally:
        _purge_tenant(db, "minted")
        app.dependency_overrides[require_api_key] = lambda: Principal(
            is_admin=True, source="legacy_env"
        )


def test_minted_key_honours_requested_expiry(monkeypatch, db):
    """A bounded credential must actually be bounded.

    The mint request used to ignore `expires_at` and answer 201, so an operator
    asking for a 30-day key silently received a permanent one.
    """
    monkeypatch.setattr(settings, "api_key", "legacy-admin")
    monkeypatch.setattr(settings, "operator_api_keys", {})
    monkeypatch.setattr(settings, "api_key_pepper", "test-pepper")
    app.dependency_overrides.pop(require_api_key, None)
    _purge_tenant(db, "expiring")
    admin = {"X-API-Key": "legacy-admin"}

    try:
        client = TestClient(app)
        tenant_id = client.post(
            "/api/v1/admin/tenants",
            headers=admin,
            json={"slug": "expiring", "name": "Expiring Co"},
        ).json()["id"]

        expires_at = datetime.now(UTC) + timedelta(seconds=2)
        minted = client.post(
            "/api/v1/admin/api-keys",
            headers=admin,
            json={
                "name": "short-lived",
                "tenant_id": tenant_id,
                "expires_at": expires_at.isoformat(),
            },
        )
        assert minted.status_code == 201, minted.text
        assert minted.json()["expires_at"] is not None
        raw = minted.json()["api_key"]
        assert client.get("/api/v1/sites", headers={"X-API-Key": raw}).status_code == 200

        # Expire it rather than sleeping; the check is on the stored value.
        record = db.scalar(select(ApiKey).where(ApiKey.prefix == raw.split("_", 3)[2]))
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        assert client.get("/api/v1/sites", headers={"X-API-Key": raw}).status_code == 401
    finally:
        _purge_tenant(db, "expiring")
        app.dependency_overrides[require_api_key] = lambda: Principal(
            is_admin=True, source="legacy_env"
        )


def test_mint_rejects_past_expiry_and_unknown_fields(monkeypatch, db):
    monkeypatch.setattr(settings, "api_key", "legacy-admin")
    monkeypatch.setattr(settings, "operator_api_keys", {})
    monkeypatch.setattr(settings, "api_key_pepper", "test-pepper")
    app.dependency_overrides.pop(require_api_key, None)
    _purge_tenant(db, "rejects")
    admin = {"X-API-Key": "legacy-admin"}

    try:
        client = TestClient(app)
        tenant_id = client.post(
            "/api/v1/admin/tenants",
            headers=admin,
            json={"slug": "rejects", "name": "Rejects Co"},
        ).json()["id"]

        past = client.post(
            "/api/v1/admin/api-keys",
            headers=admin,
            json={
                "name": "already-dead",
                "tenant_id": tenant_id,
                "expires_at": "2020-01-01T00:00:00Z",
            },
        )
        assert past.status_code == 422

        # An unrecognized lifetime control must not be silently dropped.
        unknown = client.post(
            "/api/v1/admin/api-keys",
            headers=admin,
            json={"name": "typo", "tenant_id": tenant_id, "expires_in_days": 30},
        )
        assert unknown.status_code == 422
    finally:
        _purge_tenant(db, "rejects")
        app.dependency_overrides[require_api_key] = lambda: Principal(
            is_admin=True, source="legacy_env"
        )


def test_last_used_at_survives_a_read_only_request(real_auth, db):
    """`get_db` never commits, so a flush alone left this permanently null."""
    with _tenant(db, "used") as (_tenant_row, key):
        assert real_auth.get("/api/v1/sites", headers={"X-API-Key": key}).status_code == 200
        db.expire_all()
        record = db.scalar(select(ApiKey).where(ApiKey.prefix == key.split("_", 3)[2]))
        assert record.last_used_at is not None

        # Within the refresh window a second read must not write again, so
        # concurrent traffic on one key does not serialize on its row lock.
        first = record.last_used_at
        assert real_auth.get("/api/v1/sites", headers={"X-API-Key": key}).status_code == 200
        db.expire_all()
        assert (
            db.scalar(select(ApiKey).where(ApiKey.prefix == key.split("_", 3)[2])).last_used_at
            == first
        )

        # Age it past the window and the next request refreshes it.
        record = db.scalar(select(ApiKey).where(ApiKey.prefix == key.split("_", 3)[2]))
        record.last_used_at = datetime.now(UTC) - LAST_USED_REFRESH - timedelta(seconds=5)
        db.commit()
        stale = record.last_used_at
        assert real_auth.get("/api/v1/sites", headers={"X-API-Key": key}).status_code == 200
        db.expire_all()
        assert (
            db.scalar(select(ApiKey).where(ApiKey.prefix == key.split("_", 3)[2])).last_used_at
            > stale
        )


def test_minting_requires_a_pepper_outside_development(monkeypatch, db):
    monkeypatch.setattr(settings, "api_key", "legacy-admin")
    monkeypatch.setattr(settings, "operator_api_keys", {})
    monkeypatch.setattr(settings, "api_key_pepper", "")
    monkeypatch.setattr(settings, "environment", "production")
    app.dependency_overrides.pop(require_api_key, None)
    _purge_tenant(db, "unpeppered")
    admin = {"X-API-Key": "legacy-admin"}

    try:
        client = TestClient(app)
        tenant_id = client.post(
            "/api/v1/admin/tenants",
            headers=admin,
            json={"slug": "unpeppered", "name": "Unpeppered Co"},
        ).json()["id"]
        refused = client.post(
            "/api/v1/admin/api-keys",
            headers=admin,
            json={"name": "no-pepper", "tenant_id": tenant_id},
        )
        assert refused.status_code == 503
        assert "API_KEY_PEPPER" in refused.json()["detail"]
    finally:
        _purge_tenant(db, "unpeppered")
        app.dependency_overrides[require_api_key] = lambda: Principal(
            is_admin=True, source="legacy_env"
        )


def test_hash_is_peppered(monkeypatch):
    monkeypatch.setattr(settings, "api_key_pepper", "pepper-one")
    first = hash_api_key("lm_dev_abcd_secret")
    monkeypatch.setattr(settings, "api_key_pepper", "pepper-two")
    second = hash_api_key("lm_dev_abcd_secret")
    assert first != second


# --------------------------------------------------------------------------
# Bulk review — the batched path, with rows that actually exist
# --------------------------------------------------------------------------


def test_bulk_review_rejects_cross_tenant_ids(real_auth, db):
    """A tenant naming another tenant's suggestion id must change nothing."""
    with _tenant(db, "bulk-a") as (tenant_a, key_a), _tenant(db, "bulk-b") as (tenant_b, _):
        mine = _suggestion(db, _site(db, tenant_a))
        theirs = _suggestion(db, _site(db, tenant_b))

        response = real_auth.post(
            "/api/v1/suggestions/bulk-review",
            headers={"X-API-Key": key_a},
            json={"suggestion_ids": [mine.id, theirs.id], "status": "approved"},
        )
        assert response.status_code == 403

        db.expire_all()
        assert db.get(Suggestion, mine.id).status == "pending"
        assert db.get(Suggestion, theirs.id).status == "pending"


def test_bulk_review_applies_to_owned_ids(real_auth, db):
    with _tenant(db, "bulk-own") as (tenant, key):
        owned = _suggestion(db, _site(db, tenant))
        response = real_auth.post(
            "/api/v1/suggestions/bulk-review",
            headers={"X-API-Key": key},
            json={"suggestion_ids": [owned.id], "status": "approved"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["reviewed"] == [owned.id]
        db.expire_all()
        assert db.get(Suggestion, owned.id).status == "approved"


def test_queue_hides_foreign_suggestions(real_auth, db):
    with _tenant(db, "queue-a") as (tenant_a, key_a), _tenant(db, "queue-b") as (tenant_b, _):
        mine = _suggestion(db, _site(db, tenant_a))
        theirs = _suggestion(db, _site(db, tenant_b))

        page = real_auth.get(
            "/api/v1/suggestions",
            headers={"X-API-Key": key_a},
            params={"method": ISOLATION_METHOD, "limit": 100},
        )
        assert page.status_code == 200
        ids = {row["id"] for row in page.json()["items"]}
        assert mine.id in ids
        assert theirs.id not in ids


def test_traceability_and_single_event_history_hide_foreign_tenants(real_auth, db):
    with _tenant(db, "trace-a") as (tenant_a, key_a), _tenant(
        db, "trace-b"
    ) as (tenant_b, _):
        mine = _suggestion(db, _site(db, tenant_a))
        theirs = _suggestion(db, _site(db, tenant_b))
        db.add_all(
            [
                SuggestionEvent(
                    suggestion_id=mine.id,
                    event_type="reviewed",
                    actor="tenant-a",
                    details={},
                ),
                SuggestionEvent(
                    suggestion_id=theirs.id,
                    event_type="reviewed",
                    actor="tenant-b",
                    details={},
                ),
            ]
        )
        db.commit()
        headers = {"X-API-Key": key_a}

        page = real_auth.get("/api/v1/suggestion-events", headers=headers)
        assert page.status_code == 200, page.text
        visible_ids = {item["suggestion_id"] for item in page.json()["items"]}
        assert mine.id in visible_ids
        assert theirs.id not in visible_ids

        exported = real_auth.get("/api/v1/suggestion-events/export.csv", headers=headers)
        assert exported.status_code == 200
        assert mine.trace_id in exported.text
        assert theirs.trace_id not in exported.text

        foreign_history = real_auth.get(
            f"/api/v1/suggestions/{theirs.id}/events", headers=headers
        )
        assert foreign_history.status_code == 403


def test_filtered_bulk_undo_rejects_a_foreign_tenant(real_auth, db):
    with _tenant(db, "undo-a") as (tenant_a, key_a), _tenant(
        db, "undo-b"
    ) as (_tenant_b, key_b):
        mine = _suggestion(db, _site(db, tenant_a))
        created = real_auth.post(
            "/api/v1/suggestions/bulk-review-by-filter",
            headers={"X-API-Key": key_a},
            json={
                "site_id": mine.site_id,
                "status": "approved",
                "match_status": "pending",
                "threshold_percent": 0,
            },
        )
        assert created.status_code == 200, created.text
        operation_id = created.json()["undo_operation_id"]
        assert operation_id

        denied = real_auth.post(
            f"/api/v1/suggestions/bulk-review-operations/{operation_id}/undo",
            headers={"X-API-Key": key_b},
        )
        assert denied.status_code == 403
        db.expire_all()
        assert db.get(Suggestion, mine.id).status == "approved"

        restored = real_auth.post(
            f"/api/v1/suggestions/bulk-review-operations/{operation_id}/undo",
            headers={"X-API-Key": key_a},
        )
        assert restored.status_code == 200, restored.text
        db.expire_all()
        assert db.get(Suggestion, mine.id).status == "pending"


# --------------------------------------------------------------------------
# Shared content pool
# --------------------------------------------------------------------------


def test_tenant_may_read_but_not_create_pool_sources(real_auth, db):
    """Pool sources are shared: readable by everyone, introduced only by admins.

    An approved pool source is a link target in every tenant's queue, so a
    tenant must be able to inspect it — and must not be able to stage one.
    """
    with _tenant(db, "pool-reader") as (_reader, key), _tenant(db, "pool-owner") as (owner, _):
        pool = _site(db, owner, platform="pool", name="shared-pool", approved_pool=True)
        headers = {"X-API-Key": key}

        assert real_auth.get(f"/api/v1/sites/{pool.id}", headers=headers).status_code == 200
        listed = real_auth.get("/api/v1/sites", headers=headers, params={"limit": 200})
        assert pool.id in {row["id"] for row in listed.json()}

        refused = real_auth.post(
            "/api/v1/sites",
            headers=headers,
            json={
                "name": "my-pool",
                "base_url": "https://tenant-made-pool.example.com",
                "platform": "pool",
            },
        )
        assert refused.status_code == 403

        # Reading is wider than writing: mutation stays owner/admin-only.
        assert (
            real_auth.delete(
                f"/api/v1/sites/{pool.id}",
                headers=headers,
                params={"confirm_name": "shared-pool"},
            ).status_code
            == 403
        )


def test_tenant_key_cannot_mutate_a_pool_source_owned_by_its_own_tenant(real_auth, db):
    """Pool authority is based on platform, not an accidentally matching tenant id."""
    with _tenant(db, "pool-row-owner") as (tenant, key):
        pool = _site(db, tenant, platform="pool", name="same-tenant-pool", approved_pool=True)
        headers = {"X-API-Key": key}

        assert real_auth.get(f"/api/v1/sites/{pool.id}", headers=headers).status_code == 200
        assert real_auth.post(f"/api/v1/sites/{pool.id}/ingest", headers=headers).status_code == 403
        assert (
            real_auth.delete(
                f"/api/v1/sites/{pool.id}",
                headers=headers,
                params={"confirm_name": pool.name},
            ).status_code
            == 403
        )


def test_bulk_import_cannot_smuggle_a_pool_source(real_auth, db):
    """The check runs on the validated row, so casing cannot get around it."""
    with _tenant(db, "smuggler") as (_tenant_row, key):
        response = real_auth.post(
            "/api/v1/sites/bulk",
            headers={"X-API-Key": key},
            json={
                "sites": [
                    {
                        "name": "ordinary",
                        "base_url": "https://ordinary.example.com",
                        "platform": "html",
                    },
                    {
                        "name": "sneaky",
                        "base_url": "https://sneaky.example.com",
                        "platform": "POOL",
                    },
                ]
            },
        )
        assert response.status_code == 403
        # Nothing commits until the loop finishes, so the batch landed nowhere.
        assert (
            db.scalar(select(Site).where(Site.base_url == "https://ordinary.example.com")) is None
        )


# --------------------------------------------------------------------------
# Tenancy invariants
# --------------------------------------------------------------------------


def test_two_tenants_may_hold_the_same_base_url(real_auth, db):
    """Global uniqueness both blocked legitimate overlap and leaked inventory."""
    shared_url = f"https://shared-{uuid.uuid4().hex[:8]}.example.com"
    with _tenant(db, "url-a") as (_a, key_a), _tenant(db, "url-b") as (_b, key_b):
        for key in (key_a, key_b):
            created = real_auth.post(
                "/api/v1/sites",
                headers={"X-API-Key": key},
                json={"name": "same-url", "base_url": shared_url, "platform": "html"},
            )
            assert created.status_code == 201, created.text

        # Still rejected inside one tenant.
        duplicate = real_auth.post(
            "/api/v1/sites",
            headers={"X-API-Key": key_a},
            json={"name": "same-url-again", "base_url": shared_url, "platform": "html"},
        )
        assert duplicate.status_code == 409


def test_fleet_wide_alerts_are_admin_only(real_auth, db):
    with _tenant(db, "alerted") as (tenant, key):
        site = _site(db, tenant)
        mine = Alert(site_id=site.id, kind="ingestion_failed", subject="mine", payload={})
        fleet = Alert(site_id=None, kind="queue_backlog", subject="fleet-wide", payload={})
        db.add_all([mine, fleet])
        db.commit()
        fleet_id = fleet.id

        try:
            listed = real_auth.get(
                "/api/v1/alerts", headers={"X-API-Key": key}, params={"limit": 200}
            )
            assert listed.status_code == 200
            ids = {row["id"] for row in listed.json()}
            assert mine.id in ids
            assert fleet_id not in ids

            acked = real_auth.post(
                f"/api/v1/alerts/{fleet_id}/acknowledge", headers={"X-API-Key": key}
            )
            assert acked.status_code == 403
        finally:
            db.delete(db.get(Alert, fleet_id))
            db.commit()


def test_tenant_delete_refuses_while_sites_remain(monkeypatch, db):
    monkeypatch.setattr(settings, "api_key", "legacy-admin")
    monkeypatch.setattr(settings, "operator_api_keys", {})
    monkeypatch.setattr(settings, "api_key_pepper", "test-pepper")
    app.dependency_overrides.pop(require_api_key, None)
    admin = {"X-API-Key": "legacy-admin"}

    try:
        client = TestClient(app)
        with _tenant(db, "deletable", mint=False) as (tenant, _):
            site = _site(db, tenant)
            blocked = client.delete(f"/api/v1/admin/tenants/{tenant.id}", headers=admin)
            assert blocked.status_code == 409
            assert "still owns" in blocked.json()["detail"]

            db.delete(site)
            db.commit()
            tenant_id = tenant.id
            removed = client.delete(f"/api/v1/admin/tenants/{tenant_id}", headers=admin)
            assert removed.status_code == 204
            db.expire_all()
            assert db.get(Tenant, tenant_id) is None
    finally:
        app.dependency_overrides[require_api_key] = lambda: Principal(
            is_admin=True, source="legacy_env"
        )


# --------------------------------------------------------------------------
# Pipeline batches
# --------------------------------------------------------------------------


def _batch(db, *sites) -> int:
    batch = PipelineBatch(status="running")
    db.add(batch)
    db.flush()
    db.add_all(
        PipelineSiteRun(batch_id=batch.id, site_id=site.id, status="queued") for site in sites
    )
    db.commit()
    return batch.id


def test_foreign_pipeline_batch_cannot_be_read_streamed_or_cancelled(real_auth, db):
    """Cancelling is a mutation of another scope's work, not a read of it.

    Read was authorized site by site from the start; cancel and the live stream
    were not, so any valid key could stop another scope's batch and watch its
    site ids, job state and errors go by.
    """
    with _tenant(db, "runner") as (tenant_r, _), _tenant(db, "watcher") as (_w, key_w):
        batch_id = _batch(db, _site(db, tenant_r))
        headers = {"X-API-Key": key_w}
        try:
            assert real_auth.get(f"/api/v1/pipelines/batches/{batch_id}", headers=headers).status_code == 403
            assert real_auth.get(f"/api/v1/pipelines/batches/{batch_id}/events", headers=headers).status_code == 403
            assert real_auth.post(f"/api/v1/pipelines/batches/{batch_id}/cancel", headers=headers).status_code == 403
            db.expire_all()
            assert db.get(PipelineBatch, batch_id).status == "running"
        finally:
            db.delete(db.get(PipelineBatch, batch_id))
            db.commit()


def test_retrying_own_site_in_a_shared_batch_is_refused(real_auth, db):
    """Owning one site in a batch does not authorize the batch.

    Only an admin can build a batch spanning tenants, and this is the route that
    made that dangerous: it authorized the single site being retried, then
    answered with the whole batch — every run in it, with site ids, statuses and
    error text. The retry itself is the smaller half of the problem.
    """
    with _tenant(db, "shares") as (tenant_a, key_a), _tenant(db, "shared-with") as (tenant_b, _):
        site_a = _site(db, tenant_a)
        site_b = _site(db, tenant_b)
        batch = PipelineBatch(status="failed")
        db.add(batch)
        db.flush()
        run_a = PipelineSiteRun(
            batch_id=batch.id,
            site_id=site_a.id,
            status="failed",
            stage="analysis",
            error="analysis failed",
        )
        db.add_all([run_a, PipelineSiteRun(batch_id=batch.id, site_id=site_b.id, status="queued")])
        db.commit()
        batch_id = batch.id

        try:
            refused = real_auth.post(
                f"/api/v1/pipelines/batches/{batch_id}/sites/{site_a.id}/retry",
                headers={"X-API-Key": key_a},
            )
            assert refused.status_code == 403, refused.text
            assert str(site_b.id) not in refused.text
            db.expire_all()
            assert db.get(PipelineSiteRun, run_a.id).status == "failed"
        finally:
            db.delete(db.get(PipelineBatch, batch_id))
            db.commit()


def test_own_pipeline_batch_stays_cancellable(real_auth, db):
    with _tenant(db, "cancels") as (tenant, key):
        batch_id = _batch(db, _site(db, tenant))
        try:
            cancelled = real_auth.post(
                f"/api/v1/pipelines/batches/{batch_id}/cancel", headers={"X-API-Key": key}
            )
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == "cancelled"
        finally:
            db.delete(db.get(PipelineBatch, batch_id))
            db.commit()


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def test_evaluation_api_is_admin_only(real_auth, db):
    """`site_id` is a filter, not a scope.

    Omitting it reports on every site at once, so a scoped key that could reach
    these routes would read fleet titles and export them as CSV — exactly the
    blast radius scoped keys exist to bound.
    """
    with _tenant(db, "evaluator") as (tenant, key):
        site = _site(db, tenant)
        headers = {"X-API-Key": key}
        assert real_auth.get("/api/v1/evaluation/metrics", headers=headers).status_code == 403
        assert real_auth.get(
            "/api/v1/evaluation/metrics", headers=headers, params={"site_id": site.id}
        ).status_code == 403
        assert real_auth.get(
            "/api/v1/evaluation/suggestions", headers=headers, params={"metric": "decided"}
        ).status_code == 403
        assert real_auth.get("/api/v1/evaluation/export.csv", headers=headers).status_code == 403
