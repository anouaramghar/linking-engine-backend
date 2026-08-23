from datetime import datetime

from pydantic import BaseModel, Field


class AgentActionEnvelopeIn(BaseModel):
    envelope: str = Field(min_length=20, max_length=200_000)


class AgentActionReceiptIn(AgentActionEnvelopeIn):
    expected_proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentActionPreviewOut(BaseModel):
    proposal: dict
    proposal_hash: str
    envelope_expires_at: datetime
    originating_scope: str
    requires_admin: bool


class AgentActionReceiptOut(BaseModel):
    receipt: str
    expires_at: datetime
    proposal_hash: str
