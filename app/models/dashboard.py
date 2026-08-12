"""Dashboard operator identity: who may open the UI, and as whom.

Separate from ``api_keys`` on purpose. A key is a service credential scoped to a
tenant; a dashboard user is a person, admitted once by an admin and thereafter
holding full access. See ``docs/design/dashboard-authentication.md``.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

#: A first login is a *request*, not an admission. Approval is a deliberate act
#: by someone already inside; revocation is reversible without losing history.
DashboardUserStatus = Enum(
    "pending",
    "approved",
    "revoked",
    name="dashboard_user_status",
    native_enum=False,
    length=16,
)


class DashboardUser(Base):
    __tablename__ = "dashboard_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Telegram documents IDs as fitting in 52 bits, so a 32-bit column would
    # silently overflow for accounts created later. Never reused by Telegram.
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    # Both are display-only and may change under us; identity is telegram_id.
    username: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        DashboardUserStatus, default="pending", server_default="pending", index=True
    )
    #: Whether this person may admit and remove other people.
    #:
    #: Approval used to be the only gate, which made every approved account an
    #: approver. The team lead asked for one privileged group instead: everyone
    #: approved still sees the whole dashboard, but only an admin may approve,
    #: revoke, or promote. Deliberately a flag rather than a role table — one
    #: group is the whole hierarchy that was asked for.
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The approver's telegram_id, kept as text so the audit line survives the
    # approver's own row being deleted.
    approved_by: Mapped[str | None] = mapped_column(String(255))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginNonce(Base):
    """One hashed, short-lived code delivered to an approved Telegram user.

    The historical table/column names stay in place to avoid a data migration;
    neither the plaintext code nor a browser-created credential is stored.
    """

    __tablename__ = "dashboard_login_nonces"

    id: Mapped[int] = mapped_column(primary_key=True)
    nonce: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # The Telegram account the bot delivered this code to.
    telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Set the moment it is traded for a session, or when a bound nonce belongs
    #: to a user who is not approved. Either way it is spent.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DashboardSession(Base):
    """A logged-in browser. The plaintext token is never stored."""

    __tablename__ = "dashboard_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("dashboard_users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
