import json
import logging

import httpx
from sqlalchemy import select

from app.config import settings
from app.models import Alert
from app.services import alerts


def _mock_client(monkeypatch, handler):
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        alerts.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )


def test_alert_webhook_receives_structured_payload(db, site, monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_url", "https://alerts.example.test/hook")
    received = []

    def handler(request):
        received.append(request)
        return httpx.Response(204)

    _mock_client(monkeypatch, handler)
    alerts.send_alert(
        "job failed",
        {"site_id": site.id},
        kind="job_failed",
        site_id=site.id,
    )

    assert len(received) == 1
    assert received[0].url == "https://alerts.example.test/hook"
    assert json.loads(received[0].content) == {
        "kind": "job_failed",
        "site_id": site.id,
        "subject": "job failed",
        "payload": {"site_id": site.id},
    }
    stored = db.scalar(select(Alert).where(Alert.site_id == site.id))
    assert stored.kind == "job_failed"
    assert stored.payload == {"site_id": site.id}


def test_alert_delivery_failure_is_swallowed(site, monkeypatch, caplog):
    monkeypatch.setattr(settings, "alert_webhook_url", "https://alerts.example.test/hook")

    def handler(_request):
        raise httpx.ConnectError("webhook unavailable")

    _mock_client(monkeypatch, handler)

    with caplog.at_level(logging.ERROR):
        alerts.send_alert(
            "job failed",
            {"site_id": site.id},
            kind="job_failed",
            site_id=site.id,
        )

    assert "failed to deliver alert" in caplog.text


def test_alert_dedupe_acknowledge_and_new_occurrence(client, db, site, monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_url", "")
    subject = "LinkMesh ingestion job failed"
    payload = {"job_run_id": 1}

    for _ in range(2):
        alerts.send_alert(
            subject,
            payload,
            kind="job_failed",
            site_id=site.id,
        )

    db.expire_all()
    stored = db.scalars(select(Alert).where(Alert.site_id == site.id)).all()
    assert len(stored) == 1
    assert stored[0].occurrences == 2

    listed = client.get(
        "/api/v1/alerts",
        params={"site_id": site.id, "unacknowledged": True},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [stored[0].id]
    assert listed.json()[0]["site_name"] == site.name

    first_ack = client.post(f"/api/v1/alerts/{stored[0].id}/acknowledge")
    second_ack = client.post(f"/api/v1/alerts/{stored[0].id}/acknowledge")
    assert first_ack.status_code == second_ack.status_code == 200
    assert first_ack.json()["acknowledged_at"] == second_ack.json()["acknowledged_at"]

    alerts.send_alert(
        subject,
        payload,
        kind="job_failed",
        site_id=site.id,
    )
    db.expire_all()
    stored = db.scalars(select(Alert).where(Alert.site_id == site.id).order_by(Alert.id)).all()
    assert len(stored) == 2
    assert stored[0].acknowledged_at is not None
    assert stored[1].acknowledged_at is None
    assert stored[1].occurrences == 1
    assert client.post("/api/v1/alerts/999999999/acknowledge").status_code == 404


def test_record_alert_keeps_distinct_in_app_job_outcomes_without_webhook(db, site, monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_url", "https://alerts.example.test/hook")

    alerts.record_alert(
        "LinkMesh analysis job completed",
        {"site_id": site.id, "job_run_id": 1, "outcome": "succeeded"},
        kind="job_succeeded",
        site_id=site.id,
        dedupe=False,
    )
    alerts.record_alert(
        "LinkMesh analysis job completed",
        {"site_id": site.id, "job_run_id": 2, "outcome": "succeeded"},
        kind="job_succeeded",
        site_id=site.id,
        dedupe=False,
    )

    stored = db.scalars(select(Alert).where(Alert.site_id == site.id).order_by(Alert.id)).all()
    assert [item.payload["job_run_id"] for item in stored] == [1, 2]
