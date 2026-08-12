"""Dashboard admission, Telegram code lifecycle, and session validity."""

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.models import DashboardSession, DashboardUser, LoginNonce
from app.services import dashboard_auth


@pytest.fixture(autouse=True)
def clean_dashboard_tables(db):
    def wipe() -> None:
        db.query(DashboardSession).delete()
        db.query(LoginNonce).delete()
        db.query(DashboardUser).delete()
        db.commit()

    wipe()
    yield
    wipe()


def _request(db, telegram_id: int = 4242, **identity) -> DashboardUser:
    user, code = dashboard_auth.create_login_code(db, telegram_id, **identity)
    db.commit()
    assert code is None
    return user


def _approved_code(db, telegram_id: int = 4242) -> tuple[DashboardUser, str]:
    user = _request(db, telegram_id)
    dashboard_auth.approve_user(db, user, approved_by="1")
    db.commit()
    user, code = dashboard_auth.create_login_code(db, telegram_id)
    db.commit()
    assert code is not None
    return user, code


def _approved_session(db, telegram_id: int = 4242) -> str:
    _user, code = _approved_code(db, telegram_id)
    token = dashboard_auth.redeem_login_code(db, code).token
    assert token is not None
    return token


def test_first_telegram_contact_records_a_request_and_grants_nothing(db):
    user, code = dashboard_auth.create_login_code(db, 4242, username="anouar")
    db.commit()

    assert user.status == "pending"
    assert code is None
    assert db.query(LoginNonce).count() == 0


def test_approved_user_receives_a_one_time_code_and_session(db):
    _user, code = _approved_code(db)

    outcome = dashboard_auth.redeem_login_code(db, code)

    assert outcome.state == "approved"
    assert outcome.token
    assert dashboard_auth.verify_session(db, outcome.token) is not None
    assert dashboard_auth.redeem_login_code(db, code).state == "invalid"


def test_forwarding_the_browser_bot_link_cannot_yield_somebody_elses_session(db, monkeypatch):
    """Regression for the old browser-nonce relay attack.

    The link is static and carries no credential. Only the code sent back to the
    Telegram account can be redeemed by a browser.
    """
    monkeypatch.setattr(settings, "telegram_bot_username", "linkmeshbot")
    link = dashboard_auth.login_deep_link()
    victim = _request(db, 9999)

    assert link == "https://t.me/linkmeshbot?start=login"
    assert victim.status == "pending"
    assert dashboard_auth.redeem_login_code(db, link or "").state == "invalid"


def test_revoked_user_receives_no_code(db):
    user = _request(db)
    dashboard_auth.revoke_user(db, user)
    db.commit()

    user, code = dashboard_auth.create_login_code(db, 4242)

    assert user.status == "revoked"
    assert code is None


def test_revoking_ends_a_session_already_open(db):
    token = _approved_session(db)
    user = db.query(DashboardUser).one()

    dashboard_auth.revoke_user(db, user)
    db.commit()

    assert dashboard_auth.verify_session(db, token) is None


def test_display_name_refreshes_so_approvals_name_the_right_person(db):
    _request(db, username="old_handle")
    user = _request(db, username="new_handle")

    assert user.username == "new_handle"
    assert db.query(DashboardUser).count() == 1


def test_plaintext_login_code_is_never_stored(db):
    _user, code = _approved_code(db)

    stored = db.query(LoginNonce).one()
    assert stored.nonce != code
    assert code.replace("-", "") not in stored.nonce


def test_latest_code_invalidates_an_earlier_unspent_code(db):
    _user, first = _approved_code(db)
    _user, second = dashboard_auth.create_login_code(db, 4242)
    db.commit()

    assert second is not None
    assert dashboard_auth.redeem_login_code(db, first).state == "invalid"
    assert dashboard_auth.redeem_login_code(db, second).state == "approved"


def test_unknown_or_malformed_code_is_invalid(db):
    assert dashboard_auth.redeem_login_code(db, "never-issued").state == "invalid"
    assert dashboard_auth.redeem_login_code(db, "").state == "invalid"


def test_expired_code_cannot_be_redeemed(db):
    _user, code = _approved_code(db)
    row = db.query(LoginNonce).one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    assert dashboard_auth.redeem_login_code(db, code).state == "invalid"


def test_purge_removes_expired_and_spent_codes(db):
    _user, expired = _approved_code(db)
    row = db.query(LoginNonce).one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    _user, live = dashboard_auth.create_login_code(db, 4242)
    db.commit()

    assert dashboard_auth.purge_expired_login_codes(db) == 1
    assert dashboard_auth.redeem_login_code(db, live or "").state == "approved"
    assert dashboard_auth.redeem_login_code(db, expired).state == "invalid"


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


def test_session_slide_is_throttled_and_extends_expiry(db, monkeypatch):
    token = _approved_session(db)
    stored = db.query(DashboardSession).one()
    first_seen = stored.last_seen_at
    original_expiry = stored.expires_at

    dashboard_auth.verify_session(db, token)
    db.refresh(stored)
    assert stored.last_seen_at == first_seen

    monkeypatch.setattr(dashboard_auth, "SESSION_SLIDE_INTERVAL", timedelta(seconds=0))
    dashboard_auth.verify_session(db, token)
    db.refresh(stored)
    assert stored.last_seen_at > first_seen
    assert stored.expires_at > original_expiry


def test_bootstrap_admin_is_pre_approved_and_idempotent(db, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_bootstrap_admin_id", 777)
    first = dashboard_auth.ensure_bootstrap_admin(db)
    second = dashboard_auth.ensure_bootstrap_admin(db)

    assert first is not None and first.status == "approved"
    assert first.approved_by == "bootstrap"
    assert first.is_admin is True
    assert second is not None and second.id == first.id
    assert db.query(DashboardUser).count() == 1


def test_bootstrap_restores_admin_rights_to_an_already_approved_account(db, monkeypatch):
    """The way back into a deployment whose last admin was demoted."""
    monkeypatch.setattr(settings, "dashboard_bootstrap_admin_id", 777)
    user = dashboard_auth.ensure_bootstrap_admin(db)
    dashboard_auth.set_admin(db, user, False)
    db.commit()

    assert dashboard_auth.ensure_bootstrap_admin(db).is_admin is True


def test_only_admins_are_told_about_a_new_request(db):
    approver = _request(db, 111)
    dashboard_auth.approve_user(db, approver, approved_by="bootstrap")
    dashboard_auth.set_admin(db, approver, True)
    bystander = _request(db, 222)
    dashboard_auth.approve_user(db, bystander, approved_by="111")
    db.commit()

    assert dashboard_auth.admin_telegram_ids(db) == [111]


def test_bootstrap_promotes_pending_but_never_revoked_user(db, monkeypatch):
    user = _request(db, 777)
    monkeypatch.setattr(settings, "dashboard_bootstrap_admin_id", 777)
    assert dashboard_auth.ensure_bootstrap_admin(db).status == "approved"

    dashboard_auth.revoke_user(db, user)
    db.commit()
    assert dashboard_auth.ensure_bootstrap_admin(db).status == "revoked"


def test_no_bootstrap_configured_admits_nobody(db, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_bootstrap_admin_id", None)
    assert dashboard_auth.ensure_bootstrap_admin(db) is None
    assert db.query(DashboardUser).count() == 0
