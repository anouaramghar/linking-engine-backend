"""The login endpoints, and the boundary they must not widen.

Opening a router for login is how an auth system accidentally opens everything
else, so the last test here pins that the rest of the API still refuses
anonymous callers.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.deps import require_api_key
from app.api.routes import auth as auth_routes
from app.api.routes.auth import SESSION_COOKIE
from app.config import settings
from app.main import app
from app.models import DashboardSession, DashboardUser, LoginNonce
from app.services import dashboard_auth


@pytest.fixture(autouse=True)
def configure_login(db, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("test-token"))
    monkeypatch.setattr(settings, "telegram_bot_username", "LinkMeshTestBot")

    def wipe() -> None:
        db.query(DashboardSession).delete()
        db.query(LoginNonce).delete()
        db.query(DashboardUser).delete()
        db.commit()

    wipe()
    yield
    wipe()


def _login_as(client: TestClient, db, telegram_id: int = 4242, admin: bool = True) -> str:
    """Drive the full flow and return the session cookie.

    Signed in as an admin unless a test is specifically about what an ordinary
    approved account may do — approve and revoke belong to the admin group.
    """
    user, code = dashboard_auth.create_login_code(db, telegram_id)
    assert code is None
    dashboard_auth.approve_user(db, user, approved_by="bootstrap")
    dashboard_auth.set_admin(db, user, admin)
    db.commit()
    _user, code = dashboard_auth.create_login_code(db, telegram_id)
    db.commit()
    response = client.post("/api/v1/auth/login/complete", json={"code": code})
    assert response.json()["state"] == "approved"
    return response.cookies[SESSION_COOKIE]


def _request_user(db, telegram_id: int, **identity) -> DashboardUser:
    user, code = dashboard_auth.create_login_code(db, telegram_id, **identity)
    db.commit()
    assert code is None
    return user


def test_start_login_returns_a_deep_link_and_code_lifetime(client, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_login_nonce_ttl_seconds", 300)
    body = client.post("/api/v1/auth/login/start").json()

    assert "nonce" not in body
    assert body["deep_link"] == "https://t.me/LinkMeshTestBot?start=login"
    assert body["expires_in_seconds"] == 300


def test_start_login_clears_out_expired_codes(client, db):
    """Housekeeping rides along here because nothing else schedules it."""
    user = _request_user(db, 4242)
    dashboard_auth.approve_user(db, user, approved_by="bootstrap")
    db.commit()
    _user, _code = dashboard_auth.create_login_code(db, 4242)
    stale = db.query(LoginNonce).one()
    stale.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    client.post("/api/v1/auth/login/start")

    assert db.query(LoginNonce).count() == 0


def test_start_login_fails_closed_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", None)

    response = client.post("/api/v1/auth/login/start")

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_pending_user_gets_no_redeemable_code(client, db):
    user = _request_user(db, 4242, username="anouar")

    body = client.post("/api/v1/auth/login/complete", json={"code": "AAAA-BBBB-CCCC"}).json()

    assert user.status == "pending"
    assert body["state"] == "invalid"
    assert SESSION_COOKIE not in client.cookies


def test_complete_sets_a_cookie_only_once_approved(client, db):
    token = _login_as(client, db)

    assert token


def test_unknown_code_answers_invalid_rather_than_erroring(client):
    body = client.post("/api/v1/auth/login/complete", json={"code": "AAAA-BBBB-CCCC"})

    assert body.status_code == 200
    assert body.json()["state"] == "invalid"


def test_session_requires_a_cookie(client):
    assert client.get("/api/v1/auth/session").status_code == 401


def test_session_reports_the_logged_in_user(client, db):
    _login_as(client, db, telegram_id=4242)

    body = client.get("/api/v1/auth/session").json()

    assert body["user"]["telegram_id"] == 4242
    assert body["user"]["status"] == "approved"


def test_logout_ends_the_session(client, db):
    _login_as(client, db)
    assert client.get("/api/v1/auth/session").status_code == 200

    assert client.post("/api/v1/auth/logout").status_code == 204

    assert client.get("/api/v1/auth/session").status_code == 401


def test_logout_without_a_session_is_not_an_error(client):
    assert client.post("/api/v1/auth/logout").status_code == 204


def test_admission_routes_require_a_session(client):
    assert client.get("/api/v1/auth/users").status_code == 401
    assert client.post("/api/v1/auth/users/1/approve").status_code == 401
    assert client.post("/api/v1/auth/users/1/revoke").status_code == 401
    assert client.post("/api/v1/auth/users/1/admin").status_code == 401
    assert client.delete("/api/v1/auth/users/1/admin").status_code == 401


def test_an_approved_non_admin_cannot_admit_or_remove_anyone(client, db):
    """The gate Amir asked to see enforced past the hidden buttons.

    A non-admin holds a perfectly good session, so every one of these requests
    is authenticated. What stops them is authorization, in the API.
    """
    _login_as(client, db, telegram_id=4242, admin=False)
    newcomer = _request_user(db, 9999)

    assert client.get("/api/v1/auth/users").status_code == 200
    assert client.post(f"/api/v1/auth/users/{newcomer.id}/approve").status_code == 403
    assert client.post(f"/api/v1/auth/users/{newcomer.id}/revoke").status_code == 403
    assert client.post(f"/api/v1/auth/users/{newcomer.id}/admin").status_code == 403
    assert client.delete(f"/api/v1/auth/users/{newcomer.id}/admin").status_code == 403

    db.refresh(newcomer)
    assert newcomer.status == "pending"


def test_the_roster_says_who_holds_the_keys(client, db):
    _login_as(client, db, telegram_id=4242)
    _request_user(db, 9999)

    listed = client.get("/api/v1/auth/users").json()

    assert {row["telegram_id"]: row["is_admin"] for row in listed} == {4242: True, 9999: False}


def test_an_admin_can_promote_and_demote_another_account(client, db):
    _login_as(client, db, telegram_id=4242)
    newcomer = _request_user(db, 9999)

    assert client.post(f"/api/v1/auth/users/{newcomer.id}/admin").status_code == 409

    client.post(f"/api/v1/auth/users/{newcomer.id}/approve")
    assert client.post(f"/api/v1/auth/users/{newcomer.id}/admin").json()["is_admin"] is True
    assert client.delete(f"/api/v1/auth/users/{newcomer.id}/admin").json()["is_admin"] is False


def test_an_admin_cannot_demote_themselves(client, db):
    """Same rule as revoking yourself: the last admin out locks the door."""
    _login_as(client, db, telegram_id=4242)
    me = client.get("/api/v1/auth/session").json()["user"]["id"]

    assert client.delete(f"/api/v1/auth/users/{me}/admin").status_code == 409
    assert client.get("/api/v1/auth/session").json()["user"]["is_admin"] is True


def test_a_promoted_account_keeps_its_access_when_demoted(client, db):
    _login_as(client, db, telegram_id=4242)
    newcomer = _request_user(db, 9999)
    client.post(f"/api/v1/auth/users/{newcomer.id}/approve")
    client.post(f"/api/v1/auth/users/{newcomer.id}/admin")

    body = client.delete(f"/api/v1/auth/users/{newcomer.id}/admin").json()

    assert body["status"] == "approved"
    assert body["is_admin"] is False


def test_approving_admits_the_next_login(client, db):
    _login_as(client, db, telegram_id=4242)
    newcomer = _request_user(db, 9999)
    assert newcomer.status == "pending"

    response = client.post(f"/api/v1/auth/users/{newcomer.id}/approve")

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["approved_by"] == "4242"


def test_approving_tells_the_person_they_are_in(client, db, offline_telegram):
    """Otherwise the only way to discover an approval is to keep retrying."""
    _login_as(client, db, telegram_id=4242)
    newcomer = _request_user(db, 9999)

    client.post(f"/api/v1/auth/users/{newcomer.id}/approve")

    assert offline_telegram == [(9999, auth_routes.ADMITTED_NOTICE)]


def test_re_approving_someone_already_in_says_nothing(client, db, offline_telegram):
    _login_as(client, db, telegram_id=4242)
    newcomer = _request_user(db, 9999)
    client.post(f"/api/v1/auth/users/{newcomer.id}/approve")
    offline_telegram.clear()

    client.post(f"/api/v1/auth/users/{newcomer.id}/approve")

    assert offline_telegram == []


def test_pending_users_are_listed_first(client, db):
    _login_as(client, db, telegram_id=4242)
    _request_user(db, 9999)

    listed = client.get("/api/v1/auth/users").json()

    assert [row["status"] for row in listed][0] == "pending"
    assert {row["telegram_id"] for row in listed} == {4242, 9999}


def test_revoking_is_refused_for_your_own_account(client, db):
    _login_as(client, db, telegram_id=4242)
    me = client.get("/api/v1/auth/session").json()["user"]["id"]

    response = client.post(f"/api/v1/auth/users/{me}/revoke")

    assert response.status_code == 409
    assert client.get("/api/v1/auth/session").status_code == 200


def test_revoking_an_admin_leaves_the_admin_group_alone(client, db):
    """Suspending access and demoting somebody are two separate decisions.

    Silently dropping the flag would make restoring a colleague a quiet
    demotion nobody asked for and nobody is told about. "Remove admin" is the
    action that changes membership, so a revoked admin comes back an admin.
    """
    _login_as(client, db, telegram_id=4242)
    newcomer = _request_user(db, 9999)
    client.post(f"/api/v1/auth/users/{newcomer.id}/approve")
    client.post(f"/api/v1/auth/users/{newcomer.id}/admin")

    revoked = client.post(f"/api/v1/auth/users/{newcomer.id}/revoke").json()

    assert revoked["status"] == "revoked"
    assert revoked["is_admin"] is True

    restored = client.post(f"/api/v1/auth/users/{newcomer.id}/approve").json()

    assert restored["status"] == "approved"
    assert restored["is_admin"] is True


def test_revoking_someone_ends_their_open_session(client, db):
    admin = TestClient(app)
    _login_as(admin, db, telegram_id=4242)
    other = TestClient(app)
    other_token = _login_as(other, db, telegram_id=9999)
    assert other.get("/api/v1/auth/session").status_code == 200

    target = next(
        row for row in admin.get("/api/v1/auth/users").json() if row["telegram_id"] == 9999
    )
    assert admin.post(f"/api/v1/auth/users/{target['id']}/revoke").status_code == 200

    assert other_token
    assert other.get("/api/v1/auth/session").status_code == 401


def test_session_supplies_the_operator_identity_the_shared_key_cannot(
    client, db, site, monkeypatch
):
    """The live 401 this feature exists to fix.

    The proxy attaches the shared service key, which `require_operator_identity`
    rejects, so every pool approval from the dashboard failed. A session now
    answers for a person instead. 409 here means auth passed and the route
    reached its own "not a pool source" guard.
    """
    monkeypatch.setattr(settings, "api_key", "sekret")  # defeat the dev fallback

    anonymous = TestClient(app)
    assert anonymous.post(f"/api/v1/sites/{site.id}/pool-source/reactivate").status_code == 401

    _login_as(client, db, telegram_id=4242)
    assert client.post(f"/api/v1/sites/{site.id}/pool-source/reactivate").status_code == 409


def test_opening_the_login_router_does_not_open_the_rest(monkeypatch):
    """The regression that would matter most: login routes are unauthenticated,
    and everything else must stay behind the key."""
    monkeypatch.setattr(settings, "api_key", "sekret")
    app.dependency_overrides.pop(require_api_key, None)
    unauthenticated = TestClient(app)

    assert unauthenticated.post("/api/v1/auth/login/start").status_code in (200, 503)
    assert unauthenticated.get("/api/v1/sites").status_code == 401
    assert unauthenticated.get("/api/v1/suggestions").status_code == 401


def test_dashboard_user_out_includes_photo_url(client, db):
    _login_as(client, db, telegram_id=4242)
    session = client.get("/api/v1/auth/session").json()
    user = session["user"]
    assert "photo_url" in user
    assert user["photo_url"] == f"/api/v1/auth/users/{user['id']}/avatar"


def test_user_avatar_is_cached_privately(client, db, monkeypatch):
    """A shared cache must never hold this.

    The route is behind a dashboard session and the picture belongs to the
    account it names, so `public` would let a proxy serve one operator's face to
    whoever asked next. `private` keeps the browser caching that the long
    max-age is actually for.
    """
    _login_as(client, db, telegram_id=4242)
    session = client.get("/api/v1/auth/session").json()
    user = session["user"]

    monkeypatch.setattr("app.services.telegram.client_from_settings", lambda: object())
    monkeypatch.setattr(
        "app.services.telegram.get_user_profile_photo_bytes",
        lambda _client, _tid: (b"\x89PNG\r\n\x1a\n", "image/png"),
    )

    res = client.get(f"/api/v1/auth/users/{user['id']}/avatar")
    assert res.status_code == 200
    assert "public" not in res.headers["cache-control"]
    assert res.headers["cache-control"] == "private, max-age=86400"


def test_user_avatar_endpoint_returns_404_when_user_has_no_photo(client, db, monkeypatch):
    _login_as(client, db, telegram_id=4242)
    session = client.get("/api/v1/auth/session").json()
    user = session["user"]

    # Mock telegram get_user_profile_photo_bytes to return None
    monkeypatch.setattr(
        "app.services.telegram.get_user_profile_photo_bytes", lambda _client, _tid: None
    )

    res = client.get(f"/api/v1/auth/users/{user['id']}/avatar")
    assert res.status_code == 404
