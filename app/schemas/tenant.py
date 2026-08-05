from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TenantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=255)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    created_at: datetime


class ApiKeyCreate(BaseModel):
    """Mint request.

    ``extra="forbid"`` is load-bearing: an ignored field here means an operator
    asks for a bounded credential, gets 201, and receives a permanent one. Any
    lifetime control we do not yet implement must fail loudly instead.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    is_admin: bool = False
    tenant_id: int | None = None
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _reject_past_expiry(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        # A naive datetime is ambiguous about the caller's intent; require the offset.
        if value.tzinfo is None:
            raise ValueError("expires_at must include a timezone offset")
        if value <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return value


class ApiKeyCreated(BaseModel):
    """Plaintext is returned exactly once at mint time."""

    id: int
    name: str
    prefix: str
    is_admin: bool
    tenant_id: int | None
    api_key: str
    created_at: datetime
    expires_at: datetime | None


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    prefix: str
    is_admin: bool
    tenant_id: int | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
