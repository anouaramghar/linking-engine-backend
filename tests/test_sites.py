import uuid


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
    site_id = site["id"]

    # duplicate base_url rejected
    assert client.post("/api/v1/sites", json=payload).status_code == 409

    # get + list
    assert client.get(f"/api/v1/sites/{site_id}").status_code == 200
    assert any(s["id"] == site_id for s in client.get("/api/v1/sites").json())

    # delete, then 404
    assert client.delete(f"/api/v1/sites/{site_id}").status_code == 204
    assert client.get(f"/api/v1/sites/{site_id}").status_code == 404


def test_invalid_platform_rejected(client):
    payload = _payload() | {"platform": "drupal"}
    assert client.post("/api/v1/sites", json=payload).status_code == 422
