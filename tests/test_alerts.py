import json
import logging

import httpx

from app.config import settings
from app.services import alerts


def _mock_client(monkeypatch, handler):
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        alerts.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )


def test_alert_webhook_receives_structured_payload(monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_url", "https://alerts.example.test/hook")
    received = []

    def handler(request):
        received.append(request)
        return httpx.Response(204)

    _mock_client(monkeypatch, handler)
    alerts.send_alert("job failed", {"site_id": 7})

    assert len(received) == 1
    assert received[0].url == "https://alerts.example.test/hook"
    assert json.loads(received[0].content) == {
        "subject": "job failed",
        "payload": {"site_id": 7},
    }


def test_alert_delivery_failure_is_swallowed(monkeypatch, caplog):
    monkeypatch.setattr(settings, "alert_webhook_url", "https://alerts.example.test/hook")

    def handler(_request):
        raise httpx.ConnectError("webhook unavailable")

    _mock_client(monkeypatch, handler)

    with caplog.at_level(logging.ERROR):
        alerts.send_alert("job failed", {"site_id": 7})

    assert "failed to deliver alert" in caplog.text
