"""Dashboard login: nonces, sessions, and who is admitted.

The browser proves nothing itself. It shows a nonce, the operator carries that
nonce to the bot inside Telegram, the bot binds it to whoever sent it, and the
browser then trades the nonce for a session cookie. Telegram is the identity
provider; this module is the admission desk.

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

_NONCE_BYTES = 24
_SESSION_TOKEN_BYTES = 32

#: How much of a session's life may elapse before a request extends it. The
#: slide is an UPDATE holding a row lock until commit, so extending on every
#: request would serialize all traffic sharing one session — the same trap
#: ``LAST_USED_REFRESH`` documents for API keys. One minute is enough for a
#: sliding window measured in hours.
SESSION_SLIDE_INTERVAL = timedelta(seconds=60)

_UNCONFIGURED_PEPPER = "linkmesh-unconfigured-pepper"


def _pepper() -> bytes:
    return (settings.api_key_pepper or _UNCONFIGURED_PEPPER).encode("utf-8")


def hash_session_token(raw_token: str) -> str:
    """Peppered, so a database leak alone cannot verify guessed tokens offline."""
    return hmac.new(_pepper(), raw_token.encode("utf-8"), hashlib.sha256).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def login_deep_link(nonce: str) -> str | None:
    """The t.me URL the browser shows, or None when login is not configured."""
    if not settings.telegram_bot_username:
        return None
    return f"https://t.me/{settings.telegram_bot_username}?start={nonce}"


def dashboard_login_configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_bot_username)


# --------------------------------------------------------------------------
# Nonces
# --------------------------------------------------------------------------


def create_login_nonce(db: Session) -> LoginNonce:
    """Start one login attempt. Caller commits."""
    nonce = LoginNonce(
        nonce=secrets.token_urlsafe(_NONCE_BYTES),
        expires_at=_now() + timedelta(seconds=settings.dashboard_login_nonce_ttl_seconds),
    )
    db.add(nonce)
    db.flush()
    return nonce


def bind_nonce(
    db: Session,
    nonce_value: str,
    telegram_id: int,
    *,
    username: str | None = None,
    display_name: str | None = None,
) -> DashboardUser | None:
    """Attach a Telegram identity to a pending nonce. Called by the bot.

    Returns the user the nonce now belongs to, or None when the nonce is
    unknown, expired, already bound, or already spent. Binding an unknown
    Telegram ID *records a request*; it does not grant anything.
    """
    now = _now()
    # Conditional update, so two `/start` messages racing on one nonce cannot
    # both bind it to different accounts.
    bound = db.execute(
        update(LoginNonce)
        .where(
            LoginNonce.nonce == nonce_value,
            LoginNonce.consumed_at.is_(None),
            LoginNonce.telegram_id.is_(None),
            LoginNonce.expires_at > now,
        )
        .values(telegram_id=telegram_id, bound_at=now)
        .returning(LoginNonce.id)
    ).scalar_one_or_none()
    if bound is None:
        return None

    user = upsert_user_request(db, telegram_id, username=username, display_name=display_name)
    db.commit()
    return user


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
    """What the polling browser should be told.

    ``waiting`` is the only state worth polling again; every other state is
    final for this nonce.
    """

    state: str  # waiting | approved | pending | revoked | invalid
    token: str | None = None
    user: DashboardUser | None = None


def redeem_nonce(db: Session, nonce_value: str) -> LoginOutcome:
    """Trade a bound nonce for a session. Commits on any terminal outcome."""
    now = _now()
    nonce = db.scalar(select(LoginNonce).where(LoginNonce.nonce == nonce_value))
    if nonce is None or nonce.consumed_at is not None or nonce.expires_at <= now:
        return LoginOutcome(state="invalid")
    if nonce.telegram_id is None:
        return LoginOutcome(state="waiting")

    user = db.scalar(select(DashboardUser).where(DashboardUser.telegram_id == nonce.telegram_id))
    if user is None:
        return LoginOutcome(state="invalid")

    # Spend the nonce whatever the answer: a pending user re-polling forever
    # would keep one login attempt alive indefinitely.
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
        return LoginOutcome(state=user.status, user=user)

    raw_token = issue_session(db, user)
    db.commit()
    return LoginOutcome(state="approved", token=raw_token, user=user)


def purge_expired_nonces(db: Session) -> int:
    """Housekeeping. Spent and expired nonces carry no value once past."""
    removed = db.query(LoginNonce).filter(LoginNonce.expires_at <= _now()).delete()
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


def revoke_user(db: Session, user: DashboardUser) -> DashboardUser:
    """Remove access and end any session already open. Caller commits."""
    user.status = "revoked"
    user.revoked_at = _now()
    revoke_sessions_for_user(db, user)
    logger.info("dashboard_user_revoked", extra={"telegram_id": user.telegram_id})
    return user


def ensure_bootstrap_admin(db: Session) -> DashboardUser | None:
    """Pre-approve the configured Telegram ID.

    Without this the first login is a pending request with nobody able to
    approve it, and the dashboard is unreachable by design. Idempotent, and it
    will promote that ID if it already requested access.
    """
    telegram_id = settings.dashboard_bootstrap_admin_id
    if telegram_id is None:
        return None
    user = db.scalar(select(DashboardUser).where(DashboardUser.telegram_id == telegram_id))
    if user is None:
        user = DashboardUser(telegram_id=telegram_id, status="pending")
        db.add(user)
        db.flush()
    if user.status != "approved":
        approve_user(db, user, "bootstrap")
    db.commit()
    return user
