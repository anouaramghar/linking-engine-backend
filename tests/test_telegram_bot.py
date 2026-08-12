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


def _texts(replies) -> list[str]:
    return [reply.text for reply in replies]


def _approve(db, telegram_id: int, **sender) -> None:
    """Put someone on the far side of the gate, the way a real admin would."""
    user, code = dashboard_auth.create_login_code(db, telegram_id, **sender)
    assert code is None
    dashboard_auth.approve_user(db, user, approved_by="bootstrap")
    db.commit()


def test_start_records_a_pending_request(db):
    replies = telegram_bot.handle_update(db, _update("/start login"))

    assert _texts(replies) == [telegram_bot.PENDING]
    user = db.query(DashboardUser).one()
    assert user.telegram_id == 4242
    assert user.status == "pending"


def test_a_new_request_alerts_everyone_who_could_admit_it(db):
    # The whole point of an approval queue is that somebody works it, and
    # nobody works a queue they are never told about.
    _approve(db, 111)
    _approve(db, 222)

    replies = telegram_bot.handle_update(
        db, _update("/start login", telegram_id=4242, username="anouar")
    )

    assert replies[0] == telegram_bot.Reply(4242, telegram_bot.PENDING)
    notice = telegram_bot.NEW_REQUEST.format(who="@anouar")
    assert sorted(replies[1:], key=lambda r: r.chat_id) == [
        telegram_bot.Reply(111, notice),
        telegram_bot.Reply(222, notice),
    ]


def test_an_approved_user_signing_in_again_alerts_nobody(db):
    _approve(db, 111)
    _approve(db, 4242)

    replies = telegram_bot.handle_update(db, _update("/start login", telegram_id=4242))

    assert len(replies) == 1
    assert "one-time code" in replies[0].text


def test_a_request_with_no_handle_is_still_named_in_the_alert(db):
    _approve(db, 111)

    replies = telegram_bot.handle_update(
        db, _update("/start login", telegram_id=4242, first_name="Anouar")
    )

    assert replies[1].text == telegram_bot.NEW_REQUEST.format(who="Anouar")


def test_approved_user_receives_a_one_time_code(db):
    _approve(db, 4242)

    replies = telegram_bot.handle_update(db, _update("/start login"))

    assert len(replies) == 1
    assert "one-time code" in replies[0].text


def test_revoked_user_is_told_so(db):
    user, code = dashboard_auth.create_login_code(db, 4242)
    assert code is None
    dashboard_auth.revoke_user(db, user)
    db.commit()

    replies = telegram_bot.handle_update(db, _update("/start login"))

    assert _texts(replies) == [telegram_bot.REVOKED]


def test_start_payload_carries_no_authority(db):
    replies = telegram_bot.handle_update(db, _update("/start attacker-controlled"))

    assert _texts(replies) == [telegram_bot.PENDING]
    assert db.query(DashboardUser).one().telegram_id == 4242


def test_bare_start_also_begins_the_identity_ceremony(db):
    replies = telegram_bot.handle_update(db, _update("/start"))

    assert _texts(replies) == [telegram_bot.PENDING]
    assert db.query(DashboardUser).count() == 1


def test_chatter_gets_the_welcome_not_a_login(db):
    replies = telegram_bot.handle_update(db, _update("hello?"))

    assert _texts(replies) == [telegram_bot.WELCOME]


def test_display_fields_are_captured_for_the_approval_queue(db):
    telegram_bot.handle_update(db, _update("/start login", username="anouar", first_name="Anouar"))

    user = db.query(DashboardUser).one()
    assert user.username == "anouar"
    assert user.display_name == "Anouar"


def test_non_message_updates_are_ignored(db):
    assert telegram_bot.handle_update(db, {"update_id": 1}) == []
    assert telegram_bot.handle_update(db, {"update_id": 1, "message": {}}) == []
    assert (
        telegram_bot.handle_update(db, {"update_id": 1, "edited_message": {"text": "/start x"}})
        == []
    )


def test_messages_from_other_bots_are_ignored(db):
    update = _update("/start whatever")
    update["message"]["from"]["is_bot"] = True

    assert telegram_bot.handle_update(db, update) == []


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
    client = _FakeClient([[_update("/start login", update_id=77)]])

    telegram_bot.run(client, max_cycles=2)

    assert client.offsets[0] is None  # first poll asks for everything pending
    assert client.offsets[1] == 78  # then acknowledges past the one it handled
    assert client.sent == [(4242, telegram_bot.PENDING)]


def test_loop_sends_every_message_one_update_produces(db, monkeypatch):
    monkeypatch.setattr(telegram_bot, "SessionLocal", lambda: db)
    _approve(db, 111)
    client = _FakeClient([[_update("/start login", username="anouar")]])

    telegram_bot.run(client, max_cycles=1)

    assert client.sent == [
        (4242, telegram_bot.PENDING),
        (111, telegram_bot.NEW_REQUEST.format(who="@anouar")),
    ]


def test_one_unreachable_approver_does_not_silence_the_rest(db, monkeypatch):
    monkeypatch.setattr(telegram_bot, "SessionLocal", lambda: db)
    _approve(db, 111)
    _approve(db, 222)
    client = _FakeClient([[_update("/start login")]])
    # Someone who never opened a chat with the bot: Telegram refuses that one
    # message, and the others must still go out.
    original = client.send_message

    def refuse_the_first_approver(chat_id, text, **kwargs):
        if chat_id == 111:
            raise RuntimeError("chat not found")
        original(chat_id, text, **kwargs)

    client.send_message = refuse_the_first_approver

    telegram_bot.run(client, max_cycles=1)

    assert [chat_id for chat_id, _ in client.sent] == [4242, 222]


def test_loop_survives_a_failed_poll(db, monkeypatch):
    monkeypatch.setattr(telegram_bot, "SessionLocal", lambda: db)
    monkeypatch.setattr(telegram_bot, "ERROR_BACKOFF_SECONDS", 0)
    client = _FakeClient([[_update("/start login")]], fail_first=True)

    telegram_bot.run(client, max_cycles=2)

    assert client.sent == [(4242, telegram_bot.PENDING)]


def test_loop_stops_when_signalled(db, monkeypatch):
    monkeypatch.setattr(telegram_bot, "SessionLocal", lambda: db)
    stopper = telegram_bot._Stopper()
    stopper(15, None)
    client = _FakeClient([[_update("/start x")]])

    telegram_bot.run(client, stopper)

    assert client.offsets == []  # stopped before the first poll
