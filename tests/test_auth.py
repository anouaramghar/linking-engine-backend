"""Static API-key auth — enforced on all routers except health."""

import pytest

from app.config import Settings, settings


def test_api_key_enforced(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "sekret")
    assert client.get("/api/v1/sites").status_code == 401
    assert client.get("/api/v1/sites", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/v1/sites", headers={"X-API-Key": "sekret"}).status_code == 200


def test_health_stays_open(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "sekret")
    assert client.get("/api/v1/health").status_code == 200  # docker healthcheck has no headers


def test_auth_disabled_when_key_empty(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    assert client.get("/api/v1/sites").status_code == 200


def test_api_key_required_outside_development():
    with pytest.raises(ValueError, match="API_KEY must be set"):
        Settings(environment="production", api_key="", _env_file=None)
