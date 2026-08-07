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

# httpx logs every request line at INFO and the bot token sits in the URL path,
# which is how the token reached the container logs once already. The guard
# lives here rather than in one process's logging setup so that *any* process
# able to reach Telegram gets it by importing this module.
logging.getLogger("httpx").setLevel(logging.WARNING)

API_BASE = "https://api.telegram.org"

#: Telegram holds the request open until an update arrives. Longer polls mean
#: fewer round trips; the HTTP read timeout has to clear it, see `_HTTP_MARGIN`.
POLL_TIMEOUT_SECONDS = 25

#: How much longer the HTTP read may take than the poll it carries. Without the
#: margin every long poll aborts on the client side exactly as it succeeds.
_HTTP_MARGIN = 10


class TelegramError(RuntimeError):
    """The API answered, and said no."""


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_BASE,
        http: httpx.Client | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url
        # One client, so consecutive long polls reuse the connection. Tests
        # inject one carrying a mock transport.
        self._http = http or httpx.Client()

    def _url(self, method: str) -> str:
        return f"{self._base_url}/bot{self._token}/{method}"

    def _call(self, method: str, payload: dict, *, http_timeout: float) -> object:
        """Payload is passed as a dict, not **kwargs.

        Telegram's own ``timeout`` field is part of the payload for getUpdates,
        and as a keyword it collided with this function's HTTP timeout.
        """
        response = self._http.post(self._url(method), json=payload, timeout=http_timeout)
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
        result = self._call("getUpdates", payload, http_timeout=timeout + _HTTP_MARGIN)
        return list(result) if isinstance(result, list) else []

    def send_message(self, chat_id: int, text: str, *, http_timeout: float = 10.0) -> None:
        self._call("sendMessage", {"chat_id": chat_id, "text": text}, http_timeout=http_timeout)

    def get_me(self) -> dict:
        result = self._call("getMe", {}, http_timeout=10.0)
        return result if isinstance(result, dict) else {}


def client_from_settings() -> TelegramClient | None:
    """The configured client, or None when dashboard login is switched off."""
    token = settings.telegram_bot_token
    if token is None or not token.get_secret_value():
        return None
    return TelegramClient(token.get_secret_value())


def notify(telegram_id: int, text: str) -> None:
    """Tell one person something, best effort, from outside the bot process.

    Never raises: the caller has already committed whatever it is reporting, and
    a message Telegram refuses must not undo an approval. The cost of a failure
    is that someone waits for a page refresh instead of a ping.
    """
    client = client_from_settings()
    if client is None:
        return
    try:
        client.send_message(telegram_id, text)
    except Exception:
        logger.warning("telegram_notify_failed", exc_info=True)
