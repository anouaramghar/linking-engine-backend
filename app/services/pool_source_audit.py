from typing import Literal

from sqlalchemy.orm import Session

from app.models import PoolSourceAuditEvent, Site


PoolAuditAction = Literal["approved", "revoked", "quarantined", "reactivated"]


def record_pool_source_audit_event(
    db: Session,
    site: Site,
    action: PoolAuditAction,
    operator_id: str,
    *,
    reason: str | None = None,
) -> PoolSourceAuditEvent:
    """Stage an immutable event in the caller's state-change transaction."""
    event = PoolSourceAuditEvent(
        site_id=site.id,
        site_name=site.name,
        site_base_url=site.base_url,
        action=action,
        operator_id=operator_id,
        reason=reason,
    )
    db.add(event)
    return event
