"""Dashboard login endpoints.

Registered *outside* the API-key-protected router loop in ``app.main``: the
login routes must work before the caller holds anything, and the whole point of
this router is that the proxy stops handing out authority to anonymous callers.
Everything here is gated on a dashboard session instead.

See ``docs/design/dashboard-authentication.md``.
"""

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import settings
from app.models import DashboardUser
from app.schemas.dashboard import (
    DashboardUserOut,
    LoginCompleteIn,
    LoginCompleteOut,
    LoginStartOut,
    SessionOut,
)
from app.services import dashboard_auth, telegram
from app.services.dashboard_auth import SESSION_COOKIE

router = APIRouter(prefix="/auth", tags=["auth"])

#: Closes the loop the pending screen opens. Without it the only way to discover
#: an approval is to keep trying the login until one works.
ADMITTED_NOTICE = (
    "You have been admitted to the LinkMesh dashboard.\n\n"
    "Go back to it and press Sign in with Telegram."
)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.dashboard_session_ttl_minutes * 60,
        httponly=True,  # the SPA never needs to read it; XSS should not either
        # Lax still sends the cookie on top-level navigation back from Telegram.
        # Unsafe methods are separately covered by the proxy's X-LinkMesh-Client
        # marker, which a cross-site form cannot set.
        samesite="lax",
        # Plain HTTP is the documented local path; requiring Secure there would
        # silently drop the cookie and make login look broken.
        secure=settings.environment != "development",
        path="/",
    )


def require_dashboard_session(
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    db: Session = Depends(get_db),
) -> DashboardUser:
    """An approved, unexpired, unrevoked dashboard user, or 401."""
    user = dashboard_auth.verify_session(db, session_token)
    if user is None:
        raise HTTPException(status_code=401, detail="dashboard session required")
    return user


def require_dashboard_admin(
    user: DashboardUser = Depends(require_dashboard_session),
) -> DashboardUser:
    """A dashboard user who is also in the admin group, or 403.

    The gate lives here rather than in the browser. Hiding the buttons from a
    non-admin is a courtesy; this is the part that decides.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="dashboard admin required")
    return user


@router.post("/login/start", response_model=LoginStartOut)
def start_login(db: Session = Depends(get_db)) -> LoginStartOut:
    if not dashboard_auth.dashboard_login_configured():
        raise HTTPException(
            status_code=503,
            detail="dashboard login is not configured; set TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_USERNAME",
        )
    # Housekeeping rides along with the operation that creates the garbage.
    # Logins are rare and expired codes are worthless, so a handful of deleted
    # rows here is cheaper than owning a scheduler for it.
    dashboard_auth.purge_expired_login_codes(db)
    deep_link = dashboard_auth.login_deep_link()
    assert deep_link is not None  # configured check above guarantees a username
    return LoginStartOut(
        deep_link=deep_link,
        expires_in_seconds=settings.dashboard_login_nonce_ttl_seconds,
    )


@router.post("/login/complete", response_model=LoginCompleteOut)
def complete_login(
    payload: LoginCompleteIn,
    response: Response,
    db: Session = Depends(get_db),
) -> LoginCompleteOut:
    """Redeem the one-time code Telegram delivered to this operator."""
    outcome = dashboard_auth.redeem_login_code(db, payload.code)
    if outcome.state == "approved" and outcome.token:
        _set_session_cookie(response, outcome.token)
    user = DashboardUserOut.model_validate(outcome.user) if outcome.user is not None else None
    return LoginCompleteOut(state=outcome.state, user=user)


@router.get("/session", response_model=SessionOut)
def current_session(user: DashboardUser = Depends(require_dashboard_session)) -> SessionOut:
    """Who am I. Doubles as the proxy's `auth_request` target, which reads only
    the status code."""
    return SessionOut(user=DashboardUserOut.model_validate(user))


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    db: Session = Depends(get_db),
) -> None:
    """Idempotent, and deliberately not gated: logging out an already-invalid
    session should clear the cookie rather than fail."""
    dashboard_auth.revoke_session(db, session_token)
    response.delete_cookie(SESSION_COOKIE, path="/")


# --------------------------------------------------------------------------
# Admission. Approval still grants the whole dashboard — there is no per-page
# scoping — but admitting and removing people belongs to the admin group alone.
# Listing stays open to any approved user so the roster is readable; only the
# controls that change it are privileged.
# --------------------------------------------------------------------------


@router.get("/users", response_model=list[DashboardUserOut])
def list_dashboard_users(
    _: DashboardUser = Depends(require_dashboard_session),
    db: Session = Depends(get_db),
) -> list[DashboardUser]:
    # Pending first: the list exists to get people admitted, and an approval
    # queue buried under approved accounts is a queue nobody works.
    return list(
        db.scalars(
            select(DashboardUser).order_by(
                (DashboardUser.status != "pending"), DashboardUser.requested_at.desc()
            )
        )
    )


def _target_user(db: Session, user_id: int) -> DashboardUser:
    user = db.get(DashboardUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"dashboard user {user_id} not found")
    return user


@router.post("/users/{user_id}/approve", response_model=DashboardUserOut)
def approve_dashboard_user(
    user_id: int,
    approver: DashboardUser = Depends(require_dashboard_admin),
    db: Session = Depends(get_db),
) -> DashboardUser:
    user = _target_user(db, user_id)
    was_waiting = user.status != "approved"
    dashboard_auth.approve_user(db, user, approved_by=str(approver.telegram_id))
    db.commit()
    db.refresh(user)
    # After the commit, and only when something changed: the admission is what
    # matters and must not be rolled back by a message Telegram refuses, and
    # re-approving an approved account is a no-op nobody needs telling about.
    if was_waiting:
        telegram.notify(user.telegram_id, ADMITTED_NOTICE)
    return user


@router.post("/users/{user_id}/revoke", response_model=DashboardUserOut)
def revoke_dashboard_user(
    user_id: int,
    approver: DashboardUser = Depends(require_dashboard_admin),
    db: Session = Depends(get_db),
) -> DashboardUser:
    user = _target_user(db, user_id)
    if user.id == approver.id:
        # Locking yourself out is recoverable only by another admin, and
        # possibly by nobody at all if you were the last one.
        raise HTTPException(status_code=409, detail="cannot revoke your own access")
    dashboard_auth.revoke_user(db, user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/admin", response_model=DashboardUserOut)
def grant_dashboard_admin(
    user_id: int,
    _: DashboardUser = Depends(require_dashboard_admin),
    db: Session = Depends(get_db),
) -> DashboardUser:
    """Add somebody to the admin group. Only an admin may.

    An account has to be inside before it can hold the keys, so this refuses a
    pending or revoked user rather than silently arming a flag that takes effect
    at some later approval nobody connects to this action.
    """
    user = _target_user(db, user_id)
    if user.status != "approved":
        raise HTTPException(status_code=409, detail="approve this account before making it admin")
    dashboard_auth.set_admin(db, user, True)
    db.commit()
    db.refresh(user)
    return user


@router.delete("/users/{user_id}/admin", response_model=DashboardUserOut)
def revoke_dashboard_admin(
    user_id: int,
    approver: DashboardUser = Depends(require_dashboard_admin),
    db: Session = Depends(get_db),
) -> DashboardUser:
    """Remove somebody from the admin group, leaving their access untouched."""
    user = _target_user(db, user_id)
    if user.id == approver.id:
        # Same rule as revoking yourself, and for the same reason: the last
        # admin demoting themselves leaves a dashboard nobody can admit into.
        raise HTTPException(status_code=409, detail="cannot remove your own admin rights")
    dashboard_auth.set_admin(db, user, False)
    db.commit()
    db.refresh(user)
    return user


@router.get("/users/{user_id}/avatar")
def get_user_avatar(
    user_id: int,
    db: Session = Depends(get_db),
    _: DashboardUser = Depends(require_dashboard_session),
) -> Response:
    user = _target_user(db, user_id)
    client = telegram.client_from_settings()
    if client is None:
        raise HTTPException(status_code=404, detail="Telegram bot not configured")

    result = telegram.get_user_profile_photo_bytes(client, user.telegram_id)
    if not result:
        raise HTTPException(status_code=404, detail="User profile picture not found")

    avatar_bytes, media_type = result
    return Response(
        content=avatar_bytes,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
