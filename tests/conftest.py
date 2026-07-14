import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import Site


@pytest.fixture(autouse=True)
def disable_api_key(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def site(db):
    """A throwaway site, cascade-deleted after the test."""
    site = Site(
        name="test-site",
        base_url=f"https://test-{uuid.uuid4().hex[:8]}.example.com",
        platform="wordpress",
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    yield site
    db.delete(site)
    db.commit()
