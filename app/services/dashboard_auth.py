"""Dashboard login: one-time Telegram codes, sessions, and admission.

The browser never creates a redeemable login link. Telegram gives the identified
operator a one-time code, and the operator carries that code back to their browser.
This direction matters: forwarding a bot link cannot give its creator somebody
else's session. Telegram is the identity provider; this module is the admission desk.

See ``docs/design/dashboard-authentication.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DashboardSession, DashboardUser, LoginNonce

logger = logging.getLogger(__name__)

#: Lives here rather than in the route module so `app.api.deps` can read the
#: cookie without importing a router that imports it back.
SESSION_COOKIE = "linkmesh_session"

_SESSION_TOKEN_BYTES = 32
_LOGIN_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_LOGIN_CODE_GROUPS = 3
_LOGIN_CODE_GROUP_SIZE = 4

#: How much of a session's life may elapse before a request extends it. The
#: slide is an UPDATE holding a row lock until commit, so extending on every
#: request would serialize all traffic sharing one session — the same trap
#: ``LAST_USED_REFRESH`` documents for API keys. One minute is enough for a
#: sliding window measured in hours.
SESSION_SLIDE_INTERVAL = timedelta(seconds=60)

_UNCONFIGURED_PEPPER = "linkmesh-unconfigured-pepper"


def _pepper() -> bytes:
    value = settings.api_key_pepper.strip()
    if not value:
        if settings.environment != "development":
            raise RuntimeError("API_KEY_PEPPER must be set outside development")
        value = _UNCONFIGURED_PEPPER
    return value.encode("utf-8")


def hash_session_token(raw_token: str) -> str:
    """Peppered, so a database leak alone cannot verify guessed tokens offline."""
    return hmac.new(_pepper(), raw_token.encode("utf-8"), hashlib.sha256).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def login_deep_link() -> str | None:
    """The t.me URL the browser shows, or None when login is not configured."""
    if not settings.telegram_bot_username:
        return None
    return f"https://t.me/{settings.telegram_bot_username}?start=login"


def dashboard_login_configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_bot_username)


# --------------------------------------------------------------------------
# One-time login codes
# --------------------------------------------------------------------------


def _normalize_login_code(code: str) -> str:
    return "".join(character for character in code.upper() if character.isalnum())


def hash_login_code(code: str) -> str:
    normalized = _normalize_login_code(code)
    return hmac.new(_pepper(), f"login:{normalized}".encode(), hashlib.sha256).hexdigest()


def create_login_code(
    db: Session,
    telegram_id: int,
    *,
    username: str | None = None,
    display_name: str | None = None,
) -> tuple[DashboardUser, str | None]:
    """Record the Telegram identity and issue a code only when it is approved.

    Caller commits. Any earlier unspent code for this account is invalidated so
    the bot's newest message is the only one worth entering.
    """
    now = _now()
    user = upsert_user_request(db, telegram_id, username=username, display_name=display_name)
    if user.status != "approved":
        return user, None

    db.execute(
        update(LoginNonce)
        .where(
            LoginNonce.telegram_id == telegram_id,
            LoginNonce.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    compact = "".join(
        secrets.choice(_LOGIN_CODE_ALPHABET)
        for _ in range(_LOGIN_CODE_GROUPS * _LOGIN_CODE_GROUP_SIZE)
    )
    raw_code = "-".join(
        compact[index : index + _LOGIN_CODE_GROUP_SIZE]
        for index in range(0, len(compact), _LOGIN_CODE_GROUP_SIZE)
    )
    db.add(
        LoginNonce(
            nonce=hash_login_code(raw_code),
            telegram_id=telegram_id,
            bound_at=now,
            expires_at=now + timedelta(seconds=settings.dashboard_login_nonce_ttl_seconds),
        )
    )
    db.flush()
    return user, raw_code


def upsert_user_request(
    db: Session,
    telegram_id: int,
    *,
    username: str | None = None,
    display_name: str | None = None,
) -> DashboardUser:
    """Find the user, or record a first-time access request as ``pending``.

    Display fields are refreshed on every login: Telegram lets people change
    both, and a stale handle in the approval queue is how an admin approves the
    wrong person.
    """
    user = db.scalar(select(DashboardUser).where(DashboardUser.telegram_id == telegram_id))
    if user is None:
        user = DashboardUser(
            telegram_id=telegram_id,
            username=username,
            display_name=display_name,
            status="pending",
        )
        db.add(user)
        db.flush()
        logger.info("dashboard_access_requested", extra={"telegram_id": telegram_id})
        return user
    if username is not None:
        user.username = username
    if display_name is not None:
        user.display_name = display_name
    return user


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    """What the browser should be told after submitting one code.

    Every state is final for one code.
    """

    state: str  # approved | revoked | invalid
    token: str | None = None
    user: DashboardUser | None = None


def redeem_login_code(db: Session, raw_code: str) -> LoginOutcome:
    """Trade a Telegram-issued code for a session. Commits on every outcome."""
    now = _now()
    normalized = _normalize_login_code(raw_code)
    if len(normalized) != _LOGIN_CODE_GROUPS * _LOGIN_CODE_GROUP_SIZE:
        return LoginOutcome(state="invalid")
    nonce = db.scalar(select(LoginNonce).where(LoginNonce.nonce == hash_login_code(normalized)))
    if nonce is None or nonce.consumed_at is not None or nonce.expires_at <= now:
        return LoginOutcome(state="invalid")
    user = db.scalar(select(DashboardUser).where(DashboardUser.telegram_id == nonce.telegram_id))
    if user is None:
        return LoginOutcome(state="invalid")

    # Spend the code before deciding the outcome so it cannot be replayed.
    spent = db.execute(
        update(LoginNonce)
        .where(LoginNonce.id == nonce.id, LoginNonce.consumed_at.is_(None))
        .values(consumed_at=now)
        .returning(LoginNonce.id)
    ).scalar_one_or_none()
    if spent is None:
        return LoginOutcome(state="invalid")

    if user.status != "approved":
        db.commit()
        return LoginOutcome(state="revoked", user=user)

    raw_token = issue_session(db, user)
    db.commit()
    return LoginOutcome(state="approved", token=raw_token, user=user)


def purge_expired_login_codes(db: Session) -> int:
    """Housekeeping. Spent and expired codes carry no value once past."""
    removed = (
        db.query(LoginNonce)
        .filter((LoginNonce.expires_at <= _now()) | LoginNonce.consumed_at.is_not(None))
        .delete(synchronize_session=False)
    )
    db.commit()
    return removed


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def issue_session(db: Session, user: DashboardUser) -> str:
    """Create a session and return its plaintext token, which is never stored."""
    raw_token = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
    now = _now()
    db.add(
        DashboardSession(
            user_id=user.id,
            token_hash=hash_session_token(raw_token),
            expires_at=now + timedelta(minutes=settings.dashboard_session_ttl_minutes),
            last_seen_at=now,
        )
    )
    user.last_seen_at = now
    db.flush()
    logger.info("dashboard_session_issued", extra={"telegram_id": user.telegram_id})
    return raw_token


def verify_session(db: Session, raw_token: str | None) -> DashboardUser | None:
    """Resolve a session cookie to its user, or None.

    Revocation takes effect here rather than at next login, so removing someone
    ends the session they are already holding.
    """
    if not raw_token:
        return None
    now = _now()
    session = db.scalar(
        select(DashboardSession).where(DashboardSession.token_hash == hash_session_token(raw_token))
    )
    if session is None or session.revoked_at is not None or session.expires_at <= now:
        return None

    user = db.get(DashboardUser, session.user_id)
    if user is None or user.status != "approved":
        return None

    _slide(db, session, user, now)
    return user


def _slide(db: Session, session: DashboardSession, user: DashboardUser, now: datetime) -> None:
    """Extend a session in its own transaction, at most once per interval.

    ``get_db`` never commits, so a write left to the request would be rolled
    back on close — this is what left ``api_keys.last_used_at`` permanently
    null. Failure to slide must not fail the request it rode in on.
    """
    last_seen = session.last_seen_at
    if last_seen is not None and now - last_seen < SESSION_SLIDE_INTERVAL:
        return
    try:
        db.execute(
            update(DashboardSession)
            .where(DashboardSession.id == session.id)
            .values(
                last_seen_at=now,
                expires_at=now + timedelta(minutes=settings.dashboard_session_ttl_minutes),
            )
        )
        db.execute(
            update(DashboardUser).where(DashboardUser.id == user.id).values(last_seen_at=now)
        )
        db.commit()
    except SQLAlchemyError:
        logger.warning("dashboard_session_slide_failed", exc_info=True)
        db.rollback()


def revoke_session(db: Session, raw_token: str | None) -> None:
    """Log out one browser. Idempotent."""
    if not raw_token:
        return
    db.execute(
        update(DashboardSession)
        .where(
            DashboardSession.token_hash == hash_session_token(raw_token),
            DashboardSession.revoked_at.is_(None),
        )
        .values(revoked_at=_now())
    )
    db.commit()


def revoke_sessions_for_user(db: Session, user: DashboardUser) -> None:
    db.execute(
        update(DashboardSession)
        .where(DashboardSession.user_id == user.id, DashboardSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------


def approve_user(db: Session, user: DashboardUser, approved_by: str) -> DashboardUser:
    """Admit a person. Caller commits."""
    user.status = "approved"
    user.approved_at = _now()
    user.approved_by = approved_by
    user.revoked_at = None
    logger.info(
        "dashboard_user_approved",
        extra={"telegram_id": user.telegram_id, "approved_by": approved_by},
    )
    return user


def set_admin(db: Session, user: DashboardUser, is_admin: bool) -> DashboardUser:
    """Move a person in or out of the privileged group. Caller commits.

    Admission is a separate axis: promoting somebody does not admit them, and an
    account that is not approved holds no power from this flag alone.
    """
    user.is_admin = is_admin
    logger.info(
        "dashboard_admin_changed",
        extra={"telegram_id": user.telegram_id, "is_admin": is_admin},
    )
    return user


def admin_telegram_ids(db: Session) -> list[int]:
    """Everyone who can admit a newcomer — the only people worth telling.

    Approval alone no longer carries that power, so an approved non-admin is not
    notified about a request they could not act on.
    """
    return list(
        db.scalars(
            select(DashboardUser.telegram_id).where(
                DashboardUser.status == "approved",
                DashboardUser.is_admin.is_(True),
            )
        )
    )


def describe_user(user: DashboardUser) -> str:
    """How a person is named in an approval notice. The handle first, because
    that is what an admin can recognise and check."""
    if user.username:
        return f"@{user.username}"
    return user.display_name or f"Telegram ID {user.telegram_id}"


def revoke_user(db: Session, user: DashboardUser) -> DashboardUser:
    """Remove access and end any session already open. Caller commits."""
    user.status = "revoked"
    user.revoked_at = _now()
    revoke_sessions_for_user(db, user)
    logger.info("dashboard_user_revoked", extra={"telegram_id": user.telegram_id})
    return user


def ensure_bootstrap_admin(db: Session) -> DashboardUser | None:
    """Pre-approve the configured Telegram ID and put it in the admin group.

    Without this the first login is a pending request with nobody able to
    approve it, and the dashboard is unreachable by design. Idempotent, and it
    will promote that ID if it already requested access. A revoked bootstrap
    account stays revoked; restarting the bot must never undo a deliberate ban.

    The admin flag is (re)set even for an account that is already approved: this
    is the documented way back into a deployment whose last admin was removed,
    and it is the only path that does not need a database console.
    """
    telegram_id = settings.dashboard_bootstrap_admin_id
    if telegram_id is None:
        return None
    user = db.scalar(select(DashboardUser).where(DashboardUser.telegram_id == telegram_id))
    if user is None:
        user = DashboardUser(telegram_id=telegram_id, status="pending")
        db.add(user)
        db.flush()
    if user.status == "pending":
        approve_user(db, user, "bootstrap")
    if user.status == "approved" and not user.is_admin:
        set_admin(db, user, True)
    db.commit()
    return user
