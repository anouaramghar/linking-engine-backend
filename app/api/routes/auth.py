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
    LoginPollOut,
    LoginStartOut,
    SessionOut,
)
from app.services import dashboard_auth

router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_COOKIE = "linkmesh_session"


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


@router.post("/login/start", response_model=LoginStartOut)
def start_login(db: Session = Depends(get_db)) -> LoginStartOut:
    if not dashboard_auth.dashboard_login_configured():
        raise HTTPException(
            status_code=503,
            detail="dashboard login is not configured; set TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_USERNAME",
        )
    nonce = dashboard_auth.create_login_nonce(db)
    db.commit()
    deep_link = dashboard_auth.login_deep_link(nonce.nonce)
    assert deep_link is not None  # configured check above guarantees a username
    return LoginStartOut(
        nonce=nonce.nonce,
        deep_link=deep_link,
        expires_in_seconds=settings.dashboard_login_nonce_ttl_seconds,
    )


@router.get("/login/{nonce}", response_model=LoginPollOut)
def poll_login(nonce: str, response: Response, db: Session = Depends(get_db)) -> LoginPollOut:
    """Ask whether the login finished. Sets the session cookie when it did.

    Always 200: every state here is a normal answer to a normal question, and
    the browser distinguishes them by `state`. A 401 would be indistinguishable
    from the proxy refusing the request.
    """
    outcome = dashboard_auth.redeem_nonce(db, nonce)
    if outcome.state == "approved" and outcome.token:
        _set_session_cookie(response, outcome.token)
    user = DashboardUserOut.model_validate(outcome.user) if outcome.user is not None else None
    return LoginPollOut(state=outcome.state, user=user)


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
# Admission. Every approved user may admit others: the team lead specified
# "full access once approved, no per-person scoping needed", so there is no
# narrower role to check against.
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
    approver: DashboardUser = Depends(require_dashboard_session),
    db: Session = Depends(get_db),
) -> DashboardUser:
    user = _target_user(db, user_id)
    dashboard_auth.approve_user(db, user, approved_by=str(approver.telegram_id))
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/revoke", response_model=DashboardUserOut)
def revoke_dashboard_user(
    user_id: int,
    approver: DashboardUser = Depends(require_dashboard_session),
    db: Session = Depends(get_db),
) -> DashboardUser:
    user = _target_user(db, user_id)
    if user.id == approver.id:
        # Locking yourself out is recoverable only by another approved user, and
        # possibly by nobody at all if you were the last one.
        raise HTTPException(status_code=409, detail="cannot revoke your own access")
    dashboard_auth.revoke_user(db, user)
    db.commit()
    db.refresh(user)
    return user
