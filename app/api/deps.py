from collections.abc import Iterator
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Site
from app.services import dashboard_auth
from app.services.dashboard_auth import SESSION_COOKIE
from app.services.authorization import (
    Principal,
    authenticate_api_key,
    authorize_site,
    authorize_site_read,
    require_admin_principal,
)


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
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    db: Session = Depends(get_db),
) -> str:
    """Human identity for pool audit actions.

    A logged-in dashboard session is the primary path: it names a person rather
    than a shared credential, which is the entire point of an audit trail. It is
    also what lets the dashboard perform these actions at all — the proxy
    attaches the legacy service key, which is rejected below, so before login
    existed every pool approval from the UI failed with a 401.

    An operator-mapped env key (matching the raw header) is next, then database
    admin keys and an operator-key principal, so key-based deployments and
    scripts still work; the legacy service key and tenant keys cannot. With no
    authentication configured at all, the development box signs as
    ``local-development``.
    """
    user = dashboard_auth.verify_session(db, session_token)
    if user is not None:
        return f"telegram:{user.telegram_id}"

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
