"""Tenant-scoped API authentication and site ownership checks."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import compare_digest

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ApiKey, Site, Tenant

logger = logging.getLogger(__name__)

DEFAULT_TENANT_SLUG = "default"
POOL_PLATFORM = "pool"
_KEY_PREFIX_BYTES = 4  # 8 hex chars
_KEY_SECRET_BYTES = 32

#: How stale ``last_used_at`` may get before a request refreshes it. Every
#: refresh is an UPDATE holding a row lock until commit, so writing on each
#: request would serialize all concurrent traffic sharing a single key. Minute
#: granularity is enough to answer "is this key still in use?".
LAST_USED_REFRESH = timedelta(seconds=60)

_UNCONFIGURED_PEPPER = "linkmesh-unconfigured-pepper"


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated caller for one request.

    Admin principals may cross tenants. Tenant principals may only touch sites
    whose tenant_id matches. Legacy env and operator keys are always admin.
    """

    is_admin: bool
    source: str  # legacy_env | operator | db
    tenant_id: int | None = None
    key_id: int | None = None
    operator_id: str | None = None


def _pepper() -> bytes:
    value = settings.api_key_pepper or _UNCONFIGURED_PEPPER
    return value.encode("utf-8")


def require_configured_pepper() -> None:
    """Refuse to mint a key whose hash a database leak alone could crack.

    The fallback pepper is a constant in published source, so hashes made with
    it are verifiable offline. Equally important: minting under the fallback and
    setting a real pepper later invalidates every existing key at once, with a
    401 and no explanation. Fail at mint time, where the operator can still act.
    """
    if settings.api_key_pepper:
        return
    if settings.environment == "development":
        logger.warning(
            "minting_api_key_without_pepper",
            extra={"environment": settings.environment},
        )
        return
    raise HTTPException(
        status_code=503,
        detail=(
            "API_KEY_PEPPER must be set before minting database API keys. "
            "Setting it later invalidates every key issued without it."
        ),
    )


def hash_api_key(raw_key: str) -> str:
    return hmac.new(_pepper(), raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_api_key(*, environment: str | None = None) -> tuple[str, str, str]:
    """Return (plaintext_once, prefix, secret_hash)."""
    env = (environment or settings.environment or "dev")[:12].replace("_", "-")
    prefix = secrets.token_hex(_KEY_PREFIX_BYTES)
    secret = secrets.token_urlsafe(_KEY_SECRET_BYTES)
    plaintext = f"lm_{env}_{prefix}_{secret}"
    return plaintext, prefix, hash_api_key(plaintext)


def parse_key_prefix(raw_key: str) -> str | None:
    parts = raw_key.split("_", 3)
    if len(parts) != 4 or parts[0] != "lm" or not parts[2]:
        return None
    return parts[2]


def ensure_default_tenant(db: Session) -> Tenant:
    tenant = db.scalar(select(Tenant).where(Tenant.slug == DEFAULT_TENANT_SLUG))
    if tenant is not None:
        return tenant
    tenant = Tenant(slug=DEFAULT_TENANT_SLUG, name="Default")
    db.add(tenant)
    db.flush()
    return tenant


def authenticate_api_key(db: Session, raw_key: str | None) -> Principal:
    """Resolve X-API-Key into a principal or raise 401/503."""
    if not settings.api_key and not settings.operator_api_keys:
        # Database keys alone are enough once issued.
        has_db_keys = db.scalar(select(ApiKey.id).limit(1)) is not None
        if not has_db_keys:
            raise HTTPException(
                status_code=503,
                detail="API authentication is not configured",
            )

    if raw_key is None:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key header")

    if settings.api_key and compare_digest(raw_key, settings.api_key):
        logger.warning(
            "legacy_env_api_key_used",
            extra={"auth_source": "legacy_env", "admin": True},
        )
        return Principal(is_admin=True, source="legacy_env")

    for operator_id, configured_key in settings.operator_api_keys.items():
        if compare_digest(raw_key, configured_key.get_secret_value()):
            return Principal(
                is_admin=True,
                source="operator",
                operator_id=operator_id,
            )

    prefix = parse_key_prefix(raw_key)
    if prefix is None:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key header")

    record = db.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    if record is None or record.revoked_at is not None:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key header")
    if record.expires_at is not None and record.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key header")
    if not compare_digest(record.secret_hash, hash_api_key(raw_key)):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key header")
    if record.is_admin:
        if record.tenant_id is not None:
            raise HTTPException(status_code=401, detail="invalid or missing X-API-Key header")
    elif record.tenant_id is None:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key header")

    principal = Principal(
        is_admin=record.is_admin,
        source="db",
        tenant_id=record.tenant_id,
        key_id=record.id,
    )
    _touch_last_used(db, record)
    return principal


def _touch_last_used(db: Session, record: ApiKey) -> None:
    """Record that this key was used, in its own committed transaction.

    A flush alone is not enough: ``get_db`` never commits, so a read-only route
    would roll the update back on close and leave ``last_used_at`` permanently
    null — exactly the keys an operator wants to find before revoking. Committing
    here is safe because authentication runs before any route body, so nothing
    else is pending in the session, and it releases the row lock immediately
    rather than holding it for the whole request.
    """
    now = datetime.now(UTC)
    previous = record.last_used_at
    if previous is not None:
        # Rows written before this column was timezone-aware may come back naive.
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=UTC)
        if now - previous < LAST_USED_REFRESH:
            return
    record.last_used_at = now
    db.add(record)
    try:
        db.commit()
    except SQLAlchemyError:
        # Usage telemetry must never fail a request that authenticated cleanly.
        logger.warning("api_key_last_used_update_failed", exc_info=True)
        db.rollback()


def require_admin_principal(principal: Principal) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin credentials required")
    return principal


def authorize_site(db: Session, principal: Principal, site_id: int) -> Site:
    """Authorize a mutating or operational action. Pool sources are admin-only."""
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail=f"site {site_id} not found")
    if principal.is_admin:
        return site
    if principal.tenant_id is None or site.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=403, detail="access denied for this site")
    return site


def authorize_site_read(db: Session, principal: Principal, site_id: int) -> Site:
    """Authorize reading a site's own description and articles.

    Content-pool sources are shared infrastructure: once approved they are link
    targets in *every* tenant's queue, so a tenant must be able to inspect the
    source behind a suggestion it is being asked to approve. Read is therefore
    wider than write — mutating a pool source stays admin-only through
    ``authorize_site`` and ``require_operator_identity``.
    """
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail=f"site {site_id} not found")
    if principal.is_admin or site.platform == POOL_PLATFORM:
        return site
    if principal.tenant_id is None or site.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=403, detail="access denied for this site")
    return site


def require_creatable_platform(principal: Principal, platform: str | None) -> None:
    """Only admins may introduce content-pool sources.

    A pool source approved by anyone becomes a link target for every tenant, so
    creating one is a fleet-wide act even though it looks like an ordinary site
    insert. Approval is already operator-gated; this closes the earlier half of
    the same path so a tenant cannot stage a source for approval either.
    """
    if platform != POOL_PLATFORM or principal.is_admin:
        return
    raise HTTPException(
        status_code=403,
        detail="content-pool sources are shared infrastructure and require an admin key",
    )


def check_site_access(principal: Principal, tenant_id: int) -> None:
    """Ownership check against a tenant_id already fetched — no DB round trip.

    For batched routes the owning tenant is read in the same statement as the
    rows, so per-row authorization must not open a fresh query.
    """
    if principal.is_admin:
        return
    if principal.tenant_id is None or tenant_id != principal.tenant_id:
        raise HTTPException(status_code=403, detail="access denied for this site")


def tenant_site_filter(principal: Principal):
    """SQLAlchemy predicate limiting Site rows to the caller's tenant, or None for admin.

    Strict ownership — use for operational surfaces (jobs, alerts, publication)
    where another tenant's activity must stay invisible.
    """
    if principal.is_admin:
        return None
    if principal.tenant_id is None:
        raise HTTPException(status_code=403, detail="tenant credentials required")
    return Site.tenant_id == principal.tenant_id


def readable_site_filter(principal: Principal):
    """Like :func:`tenant_site_filter`, plus the shared content pool.

    Keeps the sites listing consistent with the review queue: a tenant whose
    suggestions point at pool articles can see the sources those articles came
    from, instead of a queue full of targets attributed to sites that appear not
    to exist.
    """
    owned = tenant_site_filter(principal)
    if owned is None:
        return None
    return or_(owned, Site.platform == POOL_PLATFORM)


def resolve_create_tenant_id(
    db: Session,
    principal: Principal,
    *,
    tenant_id: int | None = None,
) -> int:
    """Tenant that owns a newly created site."""
    if principal.is_admin:
        if tenant_id is not None:
            if db.get(Tenant, tenant_id) is None:
                raise HTTPException(status_code=404, detail=f"tenant {tenant_id} not found")
            return tenant_id
        return ensure_default_tenant(db).id
    if principal.tenant_id is None:
        raise HTTPException(status_code=403, detail="tenant credentials required")
    if tenant_id is not None and tenant_id != principal.tenant_id:
        raise HTTPException(status_code=403, detail="cannot create sites for another tenant")
    return principal.tenant_id
