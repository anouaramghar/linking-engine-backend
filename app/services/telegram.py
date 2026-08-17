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

#: Ceiling on one downloaded file. Avatars are the only thing this client
#: fetches and Telegram keeps a profile photo far below this, so the bound costs
#: nothing in normal use. It stops a malformed or hostile response from being
#: read into memory in full.
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024


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

    def get_user_profile_photos(self, user_id: int, *, limit: int = 1) -> dict:
        result = self._call(
            "getUserProfilePhotos", {"user_id": user_id, "limit": limit}, http_timeout=10.0
        )
        return result if isinstance(result, dict) else {}

    def get_file(self, file_id: str) -> dict:
        result = self._call("getFile", {"file_id": file_id}, http_timeout=10.0)
        return result if isinstance(result, dict) else {}

    def download_file(self, file_path: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
        """Read at most ``max_bytes``.

        Streamed rather than read whole, so the ceiling applies as the body
        arrives instead of after the process has already spent the memory.
        """
        url = f"{self._base_url}/file/bot{self._token}/{file_path}"
        chunks: list[bytes] = []
        total = 0
        with self._http.stream("GET", url, timeout=10.0) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise TelegramError(f"file is larger than the {max_bytes} byte limit")
                chunks.append(chunk)
        return b"".join(chunks)


def client_from_settings() -> TelegramClient | None:
    """The configured client, or None when dashboard login is switched off."""
    token = settings.telegram_bot_token
    if token is None or not token.get_secret_value():
        return None
    return TelegramClient(token.get_secret_value())


def get_user_profile_photo_bytes(
    client: TelegramClient, telegram_id: int
) -> tuple[bytes, str] | None:
    """Fetch profile photo bytes and media type for a Telegram user, if available."""
    try:
        photos_data = client.get_user_profile_photos(telegram_id, limit=1)
        photos = photos_data.get("photos")
        if not photos or not isinstance(photos, list) or len(photos) == 0:
            return None
        sizes = photos[0]
        if not sizes or not isinstance(sizes, list) or len(sizes) == 0:
            return None
        last_item = sizes[-1]
        file_id = last_item.get("file_id") if isinstance(last_item, dict) else None
        if not file_id:
            return None
        file_info = client.get_file(file_id)
        file_path = file_info.get("file_path")
        if not file_path or not isinstance(file_path, str):
            return None
        data = client.download_file(file_path)
        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
        media_type = f"image/{ext}" if ext in ("png", "webp", "jpeg", "jpg") else "image/jpeg"
        if media_type == "image/jpg":
            media_type = "image/jpeg"
        return data, media_type
    except Exception as error:
        # httpx exception strings include the request URL, whose path contains
        # the bot token. Keep the useful class without serialising the secret.
        logger.warning(
            "failed_to_fetch_telegram_user_avatar",
            extra={"telegram_id": telegram_id, "error_type": type(error).__name__},
        )
        return None


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
    except Exception as error:
        logger.warning("telegram_notify_failed", extra={"error_type": type(error).__name__})
