"""The login bot's half of the flow.

`handle_update` takes a raw Telegram update and returns what to say, so these
run against the real login service with no network and no mocked HTTP.
"""

import pytest

from app import telegram_bot
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


def _update(text: str, *, telegram_id: int = 4242, update_id: int = 1, **sender) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "from": {"id": telegram_id, "is_bot": False, "first_name": "Anouar", **sender},
            "chat": {"id": telegram_id, "type": "private"},
            "text": text,
        },
    }


def _nonce(db) -> str:
    row = dashboard_auth.create_login_nonce(db)
    db.commit()
    return row.nonce


def test_start_with_a_nonce_records_a_pending_request(db):
    reply = telegram_bot.handle_update(db, _update(f"/start {_nonce(db)}"))

    assert reply is not None
    assert reply.text == telegram_bot.PENDING
    user = db.query(DashboardUser).one()
    assert user.telegram_id == 4242
    assert user.status == "pending"


def test_approved_user_is_told_they_are_signed_in(db):
    first = _nonce(db)
    user = dashboard_auth.bind_nonce(db, first, telegram_id=4242)
    dashboard_auth.approve_user(db, user, approved_by="bootstrap")
    db.commit()

    reply = telegram_bot.handle_update(db, _update(f"/start {_nonce(db)}"))

    assert reply is not None and reply.text == telegram_bot.SIGNED_IN


def test_revoked_user_is_told_so(db):
    first = _nonce(db)
    user = dashboard_auth.bind_nonce(db, first, telegram_id=4242)
    dashboard_auth.revoke_user(db, user)
    db.commit()

    reply = telegram_bot.handle_update(db, _update(f"/start {_nonce(db)}"))

    assert reply is not None and reply.text == telegram_bot.REVOKED


def test_expired_or_reused_nonce_says_so(db):
    nonce = _nonce(db)
    telegram_bot.handle_update(db, _update(f"/start {nonce}"))

    reply = telegram_bot.handle_update(db, _update(f"/start {nonce}", telegram_id=9999))

    assert reply is not None and reply.text == telegram_bot.EXPIRED


def test_unknown_nonce_says_expired_rather_than_admitting_anything(db):
    reply = telegram_bot.handle_update(db, _update("/start never-issued"))

    assert reply is not None and reply.text == telegram_bot.EXPIRED
    assert db.query(DashboardUser).count() == 0


def test_bare_start_explains_where_to_get_a_link(db):
    reply = telegram_bot.handle_update(db, _update("/start"))

    assert reply is not None and reply.text == telegram_bot.WELCOME
    assert db.query(DashboardUser).count() == 0


def test_chatter_gets_the_welcome_not_a_login(db):
    reply = telegram_bot.handle_update(db, _update("hello?"))

    assert reply is not None and reply.text == telegram_bot.WELCOME


def test_display_fields_are_captured_for_the_approval_queue(db):
    telegram_bot.handle_update(
        db, _update(f"/start {_nonce(db)}", username="anouar", first_name="Anouar")
    )

    user = db.query(DashboardUser).one()
    assert user.username == "anouar"
    assert user.display_name == "Anouar"


def test_non_message_updates_are_ignored(db):
    assert telegram_bot.handle_update(db, {"update_id": 1}) is None
    assert telegram_bot.handle_update(db, {"update_id": 1, "message": {}}) is None
    assert (
        telegram_bot.handle_update(db, {"update_id": 1, "edited_message": {"text": "/start x"}})
        is None
    )


def test_messages_from_other_bots_are_ignored(db):
    update = _update("/start whatever")
    update["message"]["from"]["is_bot"] = True

    assert telegram_bot.handle_update(db, update) is None


class _FakeClient:
    """Records what the loop sends, and can fail on demand."""

    def __init__(self, batches, fail_first=False):
        self.batches = list(batches)
        self.sent = []
        self.offsets = []
        self.fail_first = fail_first

    def get_updates(self, offset, **_kwargs):
        self.offsets.append(offset)
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("network down")
        return self.batches.pop(0) if self.batches else []

    def send_message(self, chat_id, text, **_kwargs):
        self.sent.append((chat_id, text))


def test_loop_acknowledges_updates_so_a_restart_cannot_replay_them(db, monkeypatch):
    monkeypatch.setattr(telegram_bot, "SessionLocal", lambda: db)
    client = _FakeClient([[_update(f"/start {_nonce(db)}", update_id=77)]])

    telegram_bot.run(client, max_cycles=2)

    assert client.offsets[0] is None  # first poll asks for everything pending
    assert client.offsets[1] == 78  # then acknowledges past the one it handled
    assert client.sent == [(4242, telegram_bot.PENDING)]


def test_loop_survives_a_failed_poll(db, monkeypatch):
    monkeypatch.setattr(telegram_bot, "SessionLocal", lambda: db)
    monkeypatch.setattr(telegram_bot, "ERROR_BACKOFF_SECONDS", 0)
    client = _FakeClient([[_update(f"/start {_nonce(db)}")]], fail_first=True)

    telegram_bot.run(client, max_cycles=2)

    assert client.sent == [(4242, telegram_bot.PENDING)]


def test_loop_stops_when_signalled(db, monkeypatch):
    monkeypatch.setattr(telegram_bot, "SessionLocal", lambda: db)
    stopper = telegram_bot._Stopper()
    stopper(15, None)
    client = _FakeClient([[_update("/start x")]])

    telegram_bot.run(client, stopper)

    assert client.offsets == []  # stopped before the first poll
