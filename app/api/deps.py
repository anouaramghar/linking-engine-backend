from collections.abc import Iterator
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Site
from app.services.authorization import (
    Principal,
    authenticate_api_key,
    authorize_site,
    authorize_site_read,
    require_admin_principal,
)


def _operator_for_key(api_key: str | None) -> str | None:
    if api_key is None:
        return None
    for operator_id, configured_key in settings.operator_api_keys.items():
        if compare_digest(api_key, configured_key.get_secret_value()):
            return operator_id
    return None


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_api_key(
    x_api_key: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> Principal:
    """Authenticate every protected route. Returns the request principal."""
    return authenticate_api_key(db, x_api_key)


def require_admin(
    principal: Annotated[Principal, Depends(require_api_key)],
) -> Principal:
    return require_admin_principal(principal)


def require_site_access(
    site_id: int,
    principal: Annotated[Principal, Depends(require_api_key)],
    db: Session = Depends(get_db),
) -> Site:
    """Ownership for mutating and operational routes. Pool sources are admin-only."""
    return authorize_site(db, principal, site_id)


def require_site_read(
    site_id: int,
    principal: Annotated[Principal, Depends(require_api_key)],
    db: Session = Depends(get_db),
) -> Site:
    """Read access, which additionally covers the shared content pool."""
    return authorize_site_read(db, principal, site_id)


def require_operator_identity(
    principal: Annotated[Principal, Depends(require_api_key)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    """Human identity for pool audit actions.

    An operator-mapped env key (matching the raw header) is the primary path.
    Database admin keys and an operator-key principal are accepted as fallbacks
    so key-based deployments can still approve pool sources; the legacy service
    key and tenant keys cannot. With no authentication configured at all, the
    development box signs as ``local-development``.
    """
    operator_id = _operator_for_key(x_api_key)
    if operator_id is not None:
        return operator_id
    if principal.is_admin and principal.source == "operator" and principal.operator_id is not None:
        return principal.operator_id
    if principal.is_admin and principal.source == "db" and principal.key_id is not None:
        return f"admin-key:{principal.key_id}"
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
