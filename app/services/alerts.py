"""Best-effort operational alerts that never interrupt the calling job path."""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_ALERT_TIMEOUT_SECONDS = 5.0


def send_alert(subject: str, payload: dict) -> None:
    """Send an alert to the configured webhook, or log it when no webhook is set."""
    body = {"subject": subject, "payload": payload}
    if not settings.alert_webhook_url:
        logger.error("alert: %s payload=%s", subject, payload)
        return

    try:
        with httpx.Client(timeout=_ALERT_TIMEOUT_SECONDS, trust_env=False) as client:
            response = client.post(settings.alert_webhook_url, json=body)
            response.raise_for_status()
    except Exception:
        logger.exception("failed to deliver alert: %s", subject)
