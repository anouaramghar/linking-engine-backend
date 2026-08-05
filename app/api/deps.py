from collections.abc import Iterator
from secrets import compare_digest

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _operator_for_key(api_key: str | None) -> str | None:
    if api_key is None:
        return None
    for operator_id, configured_key in settings.operator_api_keys.items():
        if compare_digest(api_key, configured_key.get_secret_value()):
            return operator_id
    return None


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not settings.api_key and not settings.operator_api_keys:
        raise HTTPException(
            status_code=503,
            detail="API authentication is not configured",
        )
    service_key_matches = bool(
        settings.api_key
        and x_api_key is not None
        and compare_digest(x_api_key, settings.api_key)
    )
    if not service_key_matches and _operator_for_key(x_api_key) is None:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key header")


def require_operator_identity(x_api_key: str | None = Header(default=None)) -> str:
    operator_id = _operator_for_key(x_api_key)
    if operator_id is not None:
        return operator_id
    if (
        settings.environment == "development"
        and not settings.api_key
        and not settings.operator_api_keys
    ):
        return "local-development"
    raise HTTPException(
        status_code=401,
        detail="an operator-specific X-API-Key is required for this action",
    )


def get_audit_actor(x_api_key: str | None = Header(default=None)) -> str:
    """Trusted actor label for ordinary state-changing API operations.

    Operator keys retain their human identity. The service key remains valid for
    dashboard reviews, but is recorded plainly as automation rather than being
    mistaken for a named editor.
    """
    operator_id = _operator_for_key(x_api_key)
    if operator_id is not None:
        return operator_id[:255]
    if (
        settings.api_key
        and x_api_key is not None
        and compare_digest(x_api_key, settings.api_key)
    ):
        return "service-api"
    if (
        settings.environment == "development"
        and not settings.api_key
        and not settings.operator_api_keys
    ):
        return "local-development"
    raise HTTPException(status_code=401, detail="invalid or missing X-API-Key header")
