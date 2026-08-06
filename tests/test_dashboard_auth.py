"""Dashboard login: admission, nonce lifecycle, and session validity.

The security claim under test is that reaching the dashboard proves nothing.
Every path here should end in "no session" unless a human admitted the account.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.models import DashboardSession, DashboardUser, LoginNonce
from app.services import dashboard_auth


@pytest.fixture(autouse=True)
def clean_dashboard_tables(db):
    """These tables are global, not per-site, so a leaked row leaks into the
    next test's counts the way a stranded ApiKey row does."""

    def wipe() -> None:
        db.query(DashboardSession).delete()
        db.query(LoginNonce).delete()
        db.query(DashboardUser).delete()
        db.commit()

    wipe()
    yield
    wipe()


def _start_login(db) -> str:
    nonce = dashboard_auth.create_login_nonce(db)
    db.commit()
    return nonce.nonce


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------


def test_first_login_records_a_request_and_grants_nothing(db):
    nonce = _start_login(db)

    user = dashboard_auth.bind_nonce(db, nonce, telegram_id=4242, username="anouar")

    assert user is not None
    assert user.status == "pending"
    outcome = dashboard_auth.redeem_nonce(db, nonce)
    assert outcome.state == "pending"
    assert outcome.token is None


def test_approved_user_receives_a_session(db):
    nonce = _start_login(db)
    user = dashboard_auth.bind_nonce(db, nonce, telegram_id=4242)
    dashboard_auth.approve_user(db, user, approved_by="1")
    db.commit()

    outcome = dashboard_auth.redeem_nonce(db, nonce)

    assert outcome.state == "approved"
    assert outcome.token
    assert dashboard_auth.verify_session(db, outcome.token) is not None


def test_revoked_user_cannot_redeem(db):
    nonce = _start_login(db)
    user = dashboard_auth.bind_nonce(db, nonce, telegram_id=4242)
    dashboard_auth.revoke_user(db, user)
    db.commit()

    assert dashboard_auth.redeem_nonce(db, nonce).state == "revoked"


def test_revoking_ends_a_session_already_open(db):
    """Revocation takes effect now, not at next login."""
    nonce = _start_login(db)
    user = dashboard_auth.bind_nonce(db, nonce, telegram_id=4242)
    dashboard_auth.approve_user(db, user, approved_by="1")
    db.commit()
    token = dashboard_auth.redeem_nonce(db, nonce).token
    assert dashboard_auth.verify_session(db, token) is not None

    dashboard_auth.revoke_user(db, user)
    db.commit()

    assert dashboard_auth.verify_session(db, token) is None


def test_display_name_refreshes_so_approvals_name_the_right_person(db):
    first = _start_login(db)
    dashboard_auth.bind_nonce(db, first, telegram_id=4242, username="old_handle")
    second = _start_login(db)

    user = dashboard_auth.bind_nonce(db, second, telegram_id=4242, username="new_handle")

    assert user.username == "new_handle"
    assert db.query(DashboardUser).count() == 1


# --------------------------------------------------------------------------
# Nonces
# --------------------------------------------------------------------------


def test_nonce_is_single_use(db):
    nonce = _start_login(db)
    user = dashboard_auth.bind_nonce(db, nonce, telegram_id=4242)
    dashboard_auth.approve_user(db, user, approved_by="1")
    db.commit()

    assert dashboard_auth.redeem_nonce(db, nonce).state == "approved"
    assert dashboard_auth.redeem_nonce(db, nonce).state == "invalid"


def test_pending_redeem_also_spends_the_nonce(db):
    """Otherwise a pending browser polls forever and keeps the attempt alive."""
    nonce = _start_login(db)
    dashboard_auth.bind_nonce(db, nonce, telegram_id=4242)

    assert dashboard_auth.redeem_nonce(db, nonce).state == "pending"
    assert dashboard_auth.redeem_nonce(db, nonce).state == "invalid"


def test_unbound_nonce_tells_the_browser_to_keep_waiting(db):
    assert dashboard_auth.redeem_nonce(db, _start_login(db)).state == "waiting"


def test_unknown_nonce_is_invalid(db):
    assert dashboard_auth.redeem_nonce(db, "never-issued").state == "invalid"


def test_expired_nonce_cannot_bind_or_redeem(db):
    nonce_row = dashboard_auth.create_login_nonce(db)
    nonce_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    assert dashboard_auth.bind_nonce(db, nonce_row.nonce, telegram_id=4242) is None
    assert dashboard_auth.redeem_nonce(db, nonce_row.nonce).state == "invalid"


def test_second_start_cannot_rebind_a_claimed_nonce(db):
    """Two `/start` messages racing must not let the loser take the session."""
    nonce = _start_login(db)
    assert dashboard_auth.bind_nonce(db, nonce, telegram_id=4242) is not None

    assert dashboard_auth.bind_nonce(db, nonce, telegram_id=9999) is None


def test_purge_removes_expired_nonces_only(db):
    live = dashboard_auth.create_login_nonce(db)
    stale = dashboard_auth.create_login_nonce(db)
    stale.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    assert dashboard_auth.purge_expired_nonces(db) == 1
    assert {row.nonce for row in db.query(LoginNonce).all()} == {live.nonce}


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def _approved_session(db, telegram_id: int = 4242) -> str:
    nonce = _start_login(db)
    user = dashboard_auth.bind_nonce(db, nonce, telegram_id=telegram_id)
    dashboard_auth.approve_user(db, user, approved_by="1")
    db.commit()
    token = dashboard_auth.redeem_nonce(db, nonce).token
    assert token is not None
    return token


def test_session_token_is_never_stored_in_plaintext(db):
    token = _approved_session(db)

    stored = db.query(DashboardSession).one()
    assert stored.token_hash != token
    assert token not in stored.token_hash


def test_expired_session_is_refused(db):
    token = _approved_session(db)
    stored = db.query(DashboardSession).one()
    stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    assert dashboard_auth.verify_session(db, token) is None


def test_logout_refuses_the_same_token_afterwards(db):
    token = _approved_session(db)

    dashboard_auth.revoke_session(db, token)

    assert dashboard_auth.verify_session(db, token) is None


def test_absent_or_unknown_token_is_refused(db):
    assert dashboard_auth.verify_session(db, None) is None
    assert dashboard_auth.verify_session(db, "") is None
    assert dashboard_auth.verify_session(db, "not-a-real-token") is None


def test_session_slide_is_throttled(db, monkeypatch):
    """Extending on every request would serialize all traffic on one session."""
    token = _approved_session(db)
    stored = db.query(DashboardSession).one()
    first_seen = stored.last_seen_at

    dashboard_auth.verify_session(db, token)
    db.refresh(stored)

    assert stored.last_seen_at == first_seen  # inside the interval, so untouched

    monkeypatch.setattr(dashboard_auth, "SESSION_SLIDE_INTERVAL", timedelta(seconds=0))
    dashboard_auth.verify_session(db, token)
    db.refresh(stored)

    assert stored.last_seen_at > first_seen


def test_slide_extends_expiry_not_just_last_seen(db, monkeypatch):
    token = _approved_session(db)
    stored = db.query(DashboardSession).one()
    original_expiry = stored.expires_at

    monkeypatch.setattr(dashboard_auth, "SESSION_SLIDE_INTERVAL", timedelta(seconds=0))
    dashboard_auth.verify_session(db, token)
    db.refresh(stored)

    assert stored.expires_at > original_expiry


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------


def test_bootstrap_admin_is_pre_approved_and_idempotent(db, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_bootstrap_admin_id", 777)

    first = dashboard_auth.ensure_bootstrap_admin(db)
    second = dashboard_auth.ensure_bootstrap_admin(db)

    assert first is not None and first.status == "approved"
    assert first.approved_by == "bootstrap"
    assert second is not None and second.id == first.id
    assert db.query(DashboardUser).count() == 1


def test_bootstrap_promotes_an_existing_pending_request(db, monkeypatch):
    """The admin may well have tried to log in before the ID was configured."""
    nonce = _start_login(db)
    dashboard_auth.bind_nonce(db, nonce, telegram_id=777)
    monkeypatch.setattr(settings, "dashboard_bootstrap_admin_id", 777)

    user = dashboard_auth.ensure_bootstrap_admin(db)

    assert user is not None and user.status == "approved"
    assert db.query(DashboardUser).count() == 1


def test_no_bootstrap_configured_admits_nobody(db, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_bootstrap_admin_id", None)

    assert dashboard_auth.ensure_bootstrap_admin(db) is None
    assert db.query(DashboardUser).count() == 0
