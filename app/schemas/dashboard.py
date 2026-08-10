from datetime import datetime

from pydantic import BaseModel, ConfigDict, computed_field


class DashboardUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_id: int
    username: str | None
    display_name: str | None
    status: str
    requested_at: datetime
    approved_at: datetime | None
    approved_by: str | None
    last_seen_at: datetime | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def photo_url(self) -> str:
        """Where the browser can fetch this account's Telegram picture.

        Derived rather than stored. Telegram hands out a photo URL only through
        the *login widget*, and this dashboard signs in over a bot deep link, so
        there has never been a value to persist — the route below re-fetches the
        current photo from Telegram on request, which also means an account that
        changes its picture is not stuck showing last year's.
        """
        return f"/api/v1/auth/users/{self.id}/avatar"


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
