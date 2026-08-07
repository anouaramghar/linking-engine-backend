from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator


class DashboardUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_id: int
    username: str | None
    display_name: str | None
    photo_url: str | None = None
    status: str
    requested_at: datetime
    approved_at: datetime | None
    approved_by: str | None
    last_seen_at: datetime | None

    @model_validator(mode="after")
    def populate_photo_url(self) -> "DashboardUserOut":
        if not self.photo_url:
            self.photo_url = f"/api/v1/auth/users/{self.id}/avatar"
        return self


class LoginStartOut(BaseModel):
    """What the browser needs to show a login prompt.

    The nonce is not a credential: holding it lets you *ask* whether the login
    finished, and the answer is a session only for an approved account.
    """

    nonce: str
    deep_link: str
    expires_in_seconds: int


class LoginPollOut(BaseModel):
    #: waiting | approved | pending | revoked | invalid
    state: str
    user: DashboardUserOut | None = None


class SessionOut(BaseModel):
    user: DashboardUserOut
