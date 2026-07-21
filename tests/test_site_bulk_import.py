import uuid

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.models import Site
from app.schemas.site import MAX_BULK_SITES

BULK_URL = "/api/v1/sites/bulk"


@pytest.fixture(autouse=True)
def guard_private_addresses(monkeypatch):
    """Pin the SSRF guard on, so the rejection tests do not depend on the local .env."""
    monkeypatch.setattr(settings, "allow_unsafe_crawl_targets", False)


@pytest.fixture
def imported(db):
    """Cascade-delete anything a bulk import created under the test's URL prefix."""
    prefix = f"https://bulk-{uuid.uuid4().hex[:8]}"
    yield prefix
    db.execute(delete(Site).where(Site.base_url.like(f"{prefix}%")))
    db.commit()


def _row(prefix: str, slug: str, **overrides) -> dict:
    return {
        "name": f"site {slug}",
        "base_url": f"{prefix}-{slug}.example.com",
        "platform": "wordpress",
    } | overrides


def test_bulk_import_creates_every_valid_row(client, db, imported):
    rows = [_row(imported, "a"), _row(imported, "b"), _row(imported, "c")]

    response = client.post(BULK_URL, json={"sites": rows})

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["created"]) == 3
    assert body["skipped"] == []
    assert body["rejected"] == []
    assert [entry["row"] for entry in body["created"]] == [1, 2, 3]
    persisted = db.scalars(select(Site.base_url).where(Site.base_url.like(f"{imported}%"))).all()
    assert set(persisted) == {row["base_url"] for row in rows}


def test_bulk_import_reports_each_failure_against_its_row(client, imported):
    rows = [
        _row(imported, "ok"),
        _row(imported, "bad-platform", platform="drupal"),
        _row(imported, "no-name", name=""),
        {"base_url": f"{imported}-no-name-either.example.com", "platform": "html"},
    ]

    body = client.post(BULK_URL, json={"sites": rows}).json()

    assert [entry["row"] for entry in body["created"]] == [1]
    assert [entry["row"] for entry in body["rejected"]] == [2, 3, 4]
    assert "platform" in body["rejected"][0]["reason"]
    # A row can fail without a usable base_url, so the echo stays nullable.
    assert body["rejected"][2]["base_url"] == f"{imported}-no-name-either.example.com"


def test_bulk_import_skips_sites_that_already_exist(client, imported):
    existing = _row(imported, "existing")
    assert client.post("/api/v1/sites", json=existing).status_code == 201

    body = client.post(BULK_URL, json={"sites": [existing, _row(imported, "fresh")]}).json()

    assert [entry["row"] for entry in body["skipped"]] == [1]
    assert body["skipped"][0]["reason"] == "a site with this base_url already exists"
    assert [entry["row"] for entry in body["created"]] == [2]


def test_bulk_import_skips_duplicates_inside_one_upload(client, db, imported):
    # Same site, written two ways — normalization must collapse them before insert.
    rows = [
        _row(imported, "dupe"),
        _row(imported, "dupe", base_url=f"{imported}-dupe.example.com/", name="second try"),
    ]

    body = client.post(BULK_URL, json={"sites": rows}).json()

    assert [entry["row"] for entry in body["created"]] == [1]
    assert [entry["row"] for entry in body["skipped"]] == [2]
    assert body["skipped"][0]["reason"] == "duplicate base_url within this upload"
    assert db.scalar(select(Site.base_url).where(Site.base_url.like(f"{imported}-dupe%")))


def test_bulk_import_rejects_unsafe_crawl_targets(client, imported):
    rows = [
        _row(imported, "safe"),
        _row(imported, "loopback", base_url="http://127.0.0.1"),
        _row(imported, "metadata", base_url="http://169.254.169.254"),
    ]

    body = client.post(BULK_URL, json={"sites": rows}).json()

    assert [entry["row"] for entry in body["created"]] == [1]
    assert [entry["row"] for entry in body["rejected"]] == [2, 3]


def test_bulk_import_normalizes_csv_shaped_cells(client, db, imported):
    """Blank cells arrive as "" and must not be stored as empty credentials."""
    rows = [
        _row(imported, "blanks", wp_username="", wp_app_password=""),
        _row(imported, "padded", name="  padded  ", platform="WordPress"),
    ]

    body = client.post(BULK_URL, json={"sites": rows}).json()

    assert len(body["created"]) == 2
    blanks = db.scalar(select(Site).where(Site.base_url == f"{imported}-blanks.example.com"))
    assert blanks.wp_username is None and blanks.wp_app_password is None
    padded = db.scalar(select(Site).where(Site.base_url == f"{imported}-padded.example.com"))
    assert padded.name == "padded"
    assert padded.platform == "wordpress"


def test_bulk_import_requires_https_when_credentials_are_supplied(client, imported):
    rows = [
        _row(
            imported,
            "plain",
            base_url="http://plain.example.com",
            wp_username="editor",
            wp_app_password="secret",
        )
    ]

    body = client.post(BULK_URL, json={"sites": rows}).json()

    assert body["created"] == []
    assert "https" in body["rejected"][0]["reason"].lower()


def test_bulk_import_rejects_a_half_filled_credential_pair(client, imported):
    """A CSV with a username column but a blank password must not import silently."""
    rows = [
        _row(imported, "user-only", wp_username="editor"),
        _row(imported, "password-only", wp_app_password="secret"),
    ]

    body = client.post(BULK_URL, json={"sites": rows}).json()

    assert body["created"] == []
    assert [entry["row"] for entry in body["rejected"]] == [1, 2]
    assert "together" in body["rejected"][0]["reason"]


def test_bulk_import_rejects_an_empty_or_oversized_batch(client, imported):
    assert client.post(BULK_URL, json={"sites": []}).status_code == 422

    oversized = [_row(imported, str(n)) for n in range(MAX_BULK_SITES + 1)]
    assert client.post(BULK_URL, json={"sites": oversized}).status_code == 422
