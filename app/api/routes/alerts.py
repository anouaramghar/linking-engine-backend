from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.pagination import MAX_PAGE_SIZE
from app.models import Alert
from app.schemas.alert import AlertOut

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertOut])
def list_alerts(
    site_id: int | None = None,
    unacknowledged: bool | None = None,
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[Alert]:
    query = select(Alert)
    if site_id is not None:
        query = query.where(Alert.site_id == site_id)
    if unacknowledged is True:
        query = query.where(Alert.acknowledged_at.is_(None))
    elif unacknowledged is False:
        query = query.where(Alert.acknowledged_at.is_not(None))
    return db.scalars(
        query.order_by(Alert.last_seen_at.desc(), Alert.id.desc()).limit(limit).offset(offset)
    ).all()


@router.post("/{alert_id}/acknowledge", response_model=AlertOut)
def acknowledge_alert(alert_id: int, db: Session = Depends(get_db)) -> Alert:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(404, f"alert {alert_id} not found")
    if alert.acknowledged_at is None:
        alert.acknowledged_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(alert)
    return alert
