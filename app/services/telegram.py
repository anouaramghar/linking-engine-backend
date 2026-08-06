"""Minimal Telegram Bot API client.

Only the two calls the login flow needs. Long polling rather than a webhook is
deliberate: the deployment sits behind an IP restriction with no inbound path
from Telegram, and outbound access to api.telegram.org is all this requires.
See ``docs/design/dashboard-authentication.md``.
"""

from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

#: Telegram holds the request open until an update arrives. Longer polls mean
#: fewer round trips; the ceiling is the read timeout below.
POLL_TIMEOUT_SECONDS = 25


class TelegramError(RuntimeError):
    """The API answered, and said no."""


class TelegramClient:
    def __init__(self, token: str, *, base_url: str = API_BASE) -> None:
        self._token = token
        self._base_url = base_url

    def _url(self, method: str) -> str:
        return f"{self._base_url}/bot{self._token}/{method}"

    def _call(self, method: str, *, timeout: float, **payload) -> object:
        response = httpx.post(self._url(method), json=payload, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            # `description` is Telegram's human-readable reason; the token is
            # never in it, so this is safe to log.
            raise TelegramError(f"{method} failed: {body.get('description')}")
        return body.get("result")

    def get_updates(self, offset: int | None, *, timeout: int = POLL_TIMEOUT_SECONDS) -> list[dict]:
        """Long-poll for updates.

        Passing ``offset`` acknowledges everything before it, so Telegram drops
        those server-side and a restart cannot replay them.
        """
        payload: dict[str, object] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        # Read timeout must outlast the long poll itself or every poll aborts.
        result = self._call("getUpdates", timeout=timeout + 10, **payload)
        return list(result) if isinstance(result, list) else []

    def send_message(self, chat_id: int, text: str, *, timeout: float = 10.0) -> None:
        self._call("sendMessage", timeout=timeout, chat_id=chat_id, text=text)


def client_from_settings() -> TelegramClient | None:
    """The configured client, or None when dashboard login is switched off."""
    token = settings.telegram_bot_token
    if token is None or not token.get_secret_value():
        return None
    return TelegramClient(token.get_secret_value())
