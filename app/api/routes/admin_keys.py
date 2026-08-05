"""Admin-only tenant and API key management."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.models import ApiKey, Site, Tenant
from app.schemas.tenant import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    TenantCreate,
    TenantOut,
)
from app.services.authorization import (
    DEFAULT_TENANT_SLUG,
    Principal,
    generate_api_key,
    require_configured_pepper,
)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/tenants", status_code=201, response_model=TenantOut)
def create_tenant(
    payload: TenantCreate,
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Tenant:
    if db.scalar(select(Tenant.id).where(Tenant.slug == payload.slug)):
        raise HTTPException(409, f"tenant slug {payload.slug!r} already exists")
    tenant = Tenant(slug=payload.slug, name=payload.name)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[Tenant]:
    return list(db.scalars(select(Tenant).order_by(Tenant.id)).all())


@router.delete("/tenants/{tenant_id}", status_code=204)
def delete_tenant(
    tenant_id: int,
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> None:
    """Remove a tenant that owns nothing. Its API keys cascade away with it.

    Sites hold the tenant with ON DELETE RESTRICT, so this refuses rather than
    orphaning or silently reassigning a live customer's inventory. Delete or
    move the sites first — that has to be a deliberate act, not a side effect.
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, f"tenant {tenant_id} not found")
    if tenant.slug == DEFAULT_TENANT_SLUG:
        raise HTTPException(409, "the default tenant cannot be deleted")
    owned = db.scalar(select(func.count()).select_from(Site).where(Site.tenant_id == tenant_id)) or 0
    if owned:
        raise HTTPException(
            409,
            f"tenant {tenant_id} still owns {owned} site(s); delete or reassign them first",
        )
    db.delete(tenant)
    db.commit()


@router.post("/api-keys", status_code=201, response_model=ApiKeyCreated)
def create_api_key(
    payload: ApiKeyCreate,
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> ApiKeyCreated:
    if payload.is_admin:
        if payload.tenant_id is not None:
            raise HTTPException(422, "admin keys must not belong to a tenant")
        tenant_id = None
    else:
        if payload.tenant_id is None:
            raise HTTPException(422, "tenant keys require tenant_id")
        if db.get(Tenant, payload.tenant_id) is None:
            raise HTTPException(404, f"tenant {payload.tenant_id} not found")
        tenant_id = payload.tenant_id

    require_configured_pepper()

    plaintext, prefix, secret_hash = generate_api_key()
    if db.scalar(select(ApiKey.id).where(ApiKey.prefix == prefix)):
        # Astronomically unlikely; retry once rather than 500.
        plaintext, prefix, secret_hash = generate_api_key()

    record = ApiKey(
        tenant_id=tenant_id,
        prefix=prefix,
        secret_hash=secret_hash,
        name=payload.name,
        is_admin=payload.is_admin,
        expires_at=payload.expires_at,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return ApiKeyCreated(
        id=record.id,
        name=record.name,
        prefix=record.prefix,
        is_admin=record.is_admin,
        tenant_id=record.tenant_id,
        api_key=plaintext,
        created_at=record.created_at,
        expires_at=record.expires_at,
    )


@router.get("/api-keys", response_model=list[ApiKeyOut])
def list_api_keys(
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[ApiKey]:
    return list(db.scalars(select(ApiKey).order_by(ApiKey.id)).all())


@router.post("/api-keys/{key_id}/revoke", response_model=ApiKeyOut)
def revoke_api_key(
    key_id: int,
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> ApiKey:
    record = db.get(ApiKey, key_id)
    if record is None:
        raise HTTPException(404, f"api key {key_id} not found")
    if record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        db.commit()
        db.refresh(record)
    return record
