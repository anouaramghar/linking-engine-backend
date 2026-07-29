from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int | None
    kind: str
    subject: str
    payload: dict
    occurrences: int
    created_at: datetime
    last_seen_at: datetime
    acknowledged_at: datetime | None
