"""The login endpoints, and the boundary they must not widen.

Opening a router for login is how an auth system accidentally opens everything
else, so the last test here pins that the rest of the API still refuses
anonymous callers.
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.deps import require_api_key
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


def _login_as(client: TestClient, db, telegram_id: int = 4242) -> str:
    """Drive the full flow and return the session cookie."""
    nonce = client.post("/api/v1/auth/login/start").json()["nonce"]
    user = dashboard_auth.bind_nonce(db, nonce, telegram_id=telegram_id)
    dashboard_auth.approve_user(db, user, approved_by="bootstrap")
    db.commit()
    response = client.get(f"/api/v1/auth/login/{nonce}")
    assert response.json()["state"] == "approved"
    return response.cookies[SESSION_COOKIE]


def test_start_login_returns_a_deep_link(client):
    body = client.post("/api/v1/auth/login/start").json()

    assert body["nonce"]
    assert body["deep_link"] == f"https://t.me/LinkMeshTestBot?start={body['nonce']}"
    assert body["expires_in_seconds"] > 0


def test_start_login_fails_closed_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", None)

    response = client.post("/api/v1/auth/login/start")

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_poll_reports_waiting_then_pending(client, db):
    nonce = client.post("/api/v1/auth/login/start").json()["nonce"]

    assert client.get(f"/api/v1/auth/login/{nonce}").json()["state"] == "waiting"

    dashboard_auth.bind_nonce(db, nonce, telegram_id=4242, username="anouar")
    body = client.get(f"/api/v1/auth/login/{nonce}").json()

    assert body["state"] == "pending"
    assert body["user"]["username"] == "anouar"
    assert SESSION_COOKIE not in client.cookies


def test_poll_sets_a_cookie_only_once_approved(client, db):
    token = _login_as(client, db)

    assert token


def test_unknown_nonce_answers_invalid_rather_than_erroring(client):
    body = client.get("/api/v1/auth/login/never-issued")

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


def test_approving_admits_the_next_login(client, db):
    _login_as(client, db, telegram_id=4242)
    pending_nonce = client.post("/api/v1/auth/login/start").json()["nonce"]
    newcomer = dashboard_auth.bind_nonce(db, pending_nonce, telegram_id=9999)
    assert newcomer.status == "pending"

    response = client.post(f"/api/v1/auth/users/{newcomer.id}/approve")

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["approved_by"] == "4242"


def test_pending_users_are_listed_first(client, db):
    _login_as(client, db, telegram_id=4242)
    nonce = client.post("/api/v1/auth/login/start").json()["nonce"]
    dashboard_auth.bind_nonce(db, nonce, telegram_id=9999)

    listed = client.get("/api/v1/auth/users").json()

    assert [row["status"] for row in listed][0] == "pending"
    assert {row["telegram_id"] for row in listed} == {4242, 9999}


def test_revoking_is_refused_for_your_own_account(client, db):
    _login_as(client, db, telegram_id=4242)
    me = client.get("/api/v1/auth/session").json()["user"]["id"]

    response = client.post(f"/api/v1/auth/users/{me}/revoke")

    assert response.status_code == 409
    assert client.get("/api/v1/auth/session").status_code == 200


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
