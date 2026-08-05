from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_api_key
from app.api.pagination import MAX_PAGE_SIZE
from app.models import Alert, Site
from app.schemas.alert import AlertOut
from app.services.authorization import Principal, authorize_site, tenant_site_filter

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertOut])
def list_alerts(
    site_id: int | None = None,
    unacknowledged: bool | None = None,
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = 0,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> list[Alert]:
    if site_id is not None:
        authorize_site(db, principal, site_id)
    query = select(Alert)
    if site_id is not None:
        query = query.where(Alert.site_id == site_id)
    else:
        owned = tenant_site_filter(principal)
        if owned is not None:
            # Fleet-wide alerts (site_id NULL) stay admin-only: their text names
            # other tenants' sites, and acknowledge already refuses them, so
            # listing them here only offered a read a tenant could not act on.
            query = query.join(Site, Site.id == Alert.site_id).where(owned)
    if unacknowledged is True:
        query = query.where(Alert.acknowledged_at.is_(None))
    elif unacknowledged is False:
        query = query.where(Alert.acknowledged_at.is_not(None))
    return db.scalars(
        query.order_by(Alert.last_seen_at.desc(), Alert.id.desc()).limit(limit).offset(offset)
    ).all()


@router.post("/{alert_id}/acknowledge", response_model=AlertOut)
def acknowledge_alert(
    alert_id: int,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> Alert:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(404, f"alert {alert_id} not found")
    if alert.site_id is not None:
        authorize_site(db, principal, alert.site_id)
    elif not principal.is_admin:
        raise HTTPException(403, detail="access denied for this alert")
    if alert.acknowledged_at is None:
        alert.acknowledged_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(alert)
    return alert
