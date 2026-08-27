"""Static API-key auth — enforced on all routers except health."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.deps import require_api_key
from app.config import Settings, settings
from app.main import app
from app.services.authorization import hash_api_key
from app.services.dashboard_auth import hash_session_token


def _real_auth_client() -> TestClient:
    app.dependency_overrides.pop(require_api_key, None)
    return TestClient(app)


def test_api_key_enforced(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "sekret")
    client = _real_auth_client()
    assert client.get("/api/v1/sites").status_code == 401
    assert client.get("/api/v1/sites", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/v1/sites", headers={"X-API-Key": "sekret"}).status_code == 200


def test_health_stays_open(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "sekret")
    client = _real_auth_client()
    assert client.get("/api/v1/health").status_code == 200  # docker healthcheck has no headers


def test_missing_api_key_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    client = _real_auth_client()
    response = client.get("/api/v1/sites")
    assert response.status_code == 503
    assert response.json()["detail"] == "API authentication is not configured"


def test_api_key_required_outside_development():
    with pytest.raises(ValueError, match="API_KEY or OPERATOR_API_KEYS must be set"):
        Settings(environment="production", api_key="", _env_file=None)


def test_database_url_has_no_committed_default(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="database_url"):
        Settings(_env_file=None)


def test_dashboard_base_url_requires_https_outside_development():
    encryption_key = SecretStr("MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAw=")

    with pytest.raises(ValueError, match="DASHBOARD_BASE_URL must use HTTPS"):
        Settings(
            environment="production",
            database_url="postgresql+psycopg://localhost/test",
            api_key="sekret",
            api_key_pepper="test-pepper",
            credential_encryption_key=encryption_key,
            dashboard_base_url="http://dash.example.com",
            _env_file=None,
        )


def test_development_may_use_http_dashboard_url():
    configured = Settings(
        database_url="postgresql+psycopg://localhost/test",
        dashboard_base_url="http://localhost:5173",
        _env_file=None,
    )

    assert configured.dashboard_base_url == "http://localhost:5173"


def test_operator_key_can_access_protected_routes(monkeypatch):
    monkeypatch.setattr(settings, "operator_api_keys", {"alice": SecretStr("alice-key")})
    client = _real_auth_client()

    assert client.get("/api/v1/sites").status_code == 401
    assert client.get("/api/v1/sites", headers={"X-API-Key": "alice-key"}).status_code == 200


def test_credential_encryption_key_required_outside_development():
    with pytest.raises(ValueError, match="CREDENTIAL_ENCRYPTION_KEY must be set"):
        Settings(
            environment="production",
            api_key="sekret",
            credential_encryption_key=None,
            _env_file=None,
        )


def test_api_key_pepper_required_outside_development():
    with pytest.raises(ValueError, match="API_KEY_PEPPER must be set"):
        Settings(
            environment="production",
            api_key="sekret",
            api_key_pepper="",
            credential_encryption_key=SecretStr("MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="),
            _env_file=None,
        )


@pytest.mark.parametrize("hash_function", [hash_api_key, hash_session_token])
def test_production_hashes_refuse_the_development_pepper(monkeypatch, hash_function):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_key_pepper", "")

    with pytest.raises(RuntimeError, match="API_KEY_PEPPER must be set"):
        hash_function("secret")


def test_production_accepts_both_required_keys():
    configured = Settings(
        environment="production",
        database_url="postgresql+psycopg://localhost/test",
        api_key="sekret",
        api_key_pepper="test-pepper",
        credential_encryption_key=SecretStr("MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="),
        _env_file=None,
    )
    assert configured.credential_encryption_key is not None
