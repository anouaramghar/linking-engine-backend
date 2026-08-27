from datetime import datetime

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    site_name: str | None = None


class AlertAcknowledgeGuard(BaseModel):
    """Optional expected state carried only by staged agent confirmations.

    Ordinary dashboard acknowledgements still send no body. Unknown legacy
    fields remain ignored so an old client cannot spoof operator identity but
    also does not become incompatible merely because the route gained a guard.
    """

    model_config = ConfigDict(extra="ignore")

    expected_unacknowledged: Literal[True] | None = None
    expected_occurrences: int | None = Field(default=None, ge=1)
    expected_last_seen_at: datetime | None = None
