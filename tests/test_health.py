# ponytail: integration smoke test — requires docker compose db+redis up
from fastapi.testclient import TestClient

from app.main import app


def test_health():
    resp = TestClient(app).get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "up", "redis": "up"}
