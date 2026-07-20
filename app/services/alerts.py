"""Best-effort operational alerts that never interrupt the calling job path."""

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Alert

logger = logging.getLogger(__name__)

_ALERT_TIMEOUT_SECONDS = 5.0


def _store_alert(subject: str, payload: dict, kind: str, site_id: int | None) -> None:
    try:
        with SessionLocal() as db:
            site_filter = Alert.site_id.is_(None) if site_id is None else Alert.site_id == site_id
            existing = db.scalars(
                select(Alert)
                .where(
                    site_filter,
                    Alert.kind == kind,
                    Alert.subject == subject,
                    Alert.acknowledged_at.is_(None),
                )
                .order_by(Alert.id.desc())
                .with_for_update()
            ).first()
            now = datetime.now(timezone.utc)
            if existing is None:
                db.add(
                    Alert(
                        site_id=site_id,
                        kind=kind,
                        subject=subject,
                        payload=payload,
                        last_seen_at=now,
                    )
                )
            else:
                existing.occurrences += 1
                existing.last_seen_at = now
            db.commit()
    except Exception:
        logger.exception("failed to persist alert: %s", subject)


def send_alert(
    subject: str,
    payload: dict,
    *,
    kind: str,
    site_id: int | None,
) -> None:
    """Persist an alert and optionally send it to the configured webhook."""
    _store_alert(subject, payload, kind, site_id)
    body = {
        "kind": kind,
        "site_id": site_id,
        "subject": subject,
        "payload": payload,
    }
    if not settings.alert_webhook_url:
        logger.error("alert: %s payload=%s", subject, payload)
        return

    try:
        with httpx.Client(timeout=_ALERT_TIMEOUT_SECONDS, trust_env=False) as client:
            response = client.post(settings.alert_webhook_url, json=body)
            response.raise_for_status()
    except Exception:
        logger.exception("failed to deliver alert: %s", subject)
