"""The Bot API client itself.

These exist because the first version shipped a TypeError that every
handle_update test missed: the loop was tested against a fake client, so the
real one was never called until it hit Telegram for real. A mock transport
costs nothing and closes that gap.
"""

import httpx
import pytest

from app.services.telegram import TelegramClient, TelegramError


def _client(handler) -> TelegramClient:
    return TelegramClient("test-token", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_get_updates_sends_telegram_timeout_without_colliding_with_the_http_one():
    """The exact bug: `timeout` means two different things here."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"ok": True, "result": []})

    updates = _client(handler).get_updates(None, timeout=25)

    assert updates == []
    assert seen["url"].endswith("/bottest-token/getUpdates")
    assert '"timeout":25' in seen["body"].replace(" ", "")


def test_offset_is_forwarded_so_telegram_drops_acknowledged_updates():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read().decode().replace(" ", "")
        return httpx.Response(200, json={"ok": True, "result": []})

    _client(handler).get_updates(78)

    assert '"offset":78' in seen["body"]


def test_first_poll_omits_offset_entirely():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"ok": True, "result": []})

    _client(handler).get_updates(None)

    assert "offset" not in seen["body"]


def test_updates_are_returned_as_a_list():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 1}]})

    assert _client(handler).get_updates(None) == [{"update_id": 1}]


def test_send_message_posts_chat_and_text():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode().replace(" ", "")
        return httpx.Response(200, json={"ok": True, "result": {}})

    _client(handler).send_message(4242, "hello")

    assert seen["url"].endswith("/sendMessage")
    assert '"chat_id":4242' in seen["body"]
    assert '"text":"hello"' in seen["body"]


def test_a_refusal_from_telegram_raises_with_its_reason():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "Unauthorized"})

    with pytest.raises(TelegramError, match="Unauthorized"):
        _client(handler).get_me()


def test_the_bot_silences_httpx_request_logging():
    """The token is in the URL path, and httpx logs the request line at INFO.

    Leaving that on writes the credential to the container logs in plaintext,
    which is how this was found — in a live run, after the fact.
    """
    import logging

    from app import telegram_bot

    logging.getLogger("httpx").setLevel(logging.INFO)  # simulate a fresh process

    telegram_bot.configure_logging()

    assert logging.getLogger("httpx").level >= logging.WARNING


def test_an_http_error_is_not_swallowed():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    with pytest.raises(httpx.HTTPStatusError):
        _client(handler).get_updates(None)
