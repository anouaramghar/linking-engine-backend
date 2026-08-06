from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class PoolSourceAuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    site_name: str
    site_base_url: str
    action: Literal["approved", "revoked", "quarantined", "reactivated"]
    operator_id: str
    reason: str | None
    created_at: datetime
