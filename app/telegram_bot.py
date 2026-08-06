"""The login bot: binds a `/start <nonce>` to whoever sent it.

Run as its own process, alongside the API and the RQ worker:

    python -m app.telegram_bot

Telegram authenticates the sender, so the ``from.id`` on an update is trusted
identity — that is the whole reason this exists. Everything the bot says back
is deliberately vague about *why* a login failed, since the sender is not yet
anybody in particular.
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from types import FrameType

from app.config import settings
from app.db import SessionLocal
from app.services import dashboard_auth
from app.services.telegram import TelegramClient, client_from_settings

logger = logging.getLogger(__name__)

#: Backoff after a failed poll. Long enough not to hammer a broken network,
#: short enough that a blip does not stall logins for a noticeable time.
ERROR_BACKOFF_SECONDS = 5

WELCOME = (
    "This bot signs you in to the LinkMesh dashboard.\n\n"
    "Open the dashboard, press Log in, and follow the link it gives you."
)
EXPIRED = (
    "That login link has expired or was already used.\n\n"
    "Go back to the dashboard and press Log in again."
)
SIGNED_IN = "You are signed in. Return to the dashboard — it should be logging you in now."
PENDING = (
    "Access requested.\n\n"
    "An approved user has to admit you before you can open the dashboard. "
    "You will not be let in automatically."
)
REVOKED = "Your access to the dashboard has been removed. Ask an approved user to restore it."


@dataclass(frozen=True, slots=True)
class Reply:
    chat_id: int
    text: str


def handle_update(db, update: dict) -> Reply | None:
    """Decide what to say to one update. Pure enough to test without a network.

    Returns None for anything that is not a private text message, which covers
    edits, joins, and every other update type Telegram may add later.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    sender = message.get("from")
    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(sender, dict) or not isinstance(chat, dict) or not isinstance(text, str):
        return None
    if sender.get("is_bot"):
        return None

    telegram_id = sender.get("id")
    chat_id = chat.get("id")
    if not isinstance(telegram_id, int) or not isinstance(chat_id, int):
        return None

    if not text.startswith("/start"):
        return Reply(chat_id, WELCOME)

    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        # A bare /start — someone opened the bot directly rather than from a
        # login link. There is nothing to bind.
        return Reply(chat_id, WELCOME)

    user = dashboard_auth.bind_nonce(
        db,
        parts[1].strip(),
        telegram_id,
        username=sender.get("username"),
        display_name=sender.get("first_name"),
    )
    if user is None:
        return Reply(chat_id, EXPIRED)
    if user.status == "approved":
        return Reply(chat_id, SIGNED_IN)
    if user.status == "revoked":
        return Reply(chat_id, REVOKED)
    return Reply(chat_id, PENDING)


class _Stopper:
    """SIGTERM/SIGINT flag. A poll in flight finishes before the loop exits."""

    def __init__(self) -> None:
        self.stopped = False

    def __call__(self, signum: int, _frame: FrameType | None) -> None:
        logger.info("telegram_bot_stopping", extra={"signal": signum})
        self.stopped = True


def run(
    client: TelegramClient, stopper: _Stopper | None = None, *, max_cycles: int | None = None
) -> None:
    """Long-poll until told to stop.

    ``max_cycles`` exists for tests; production passes None and runs forever.
    """
    stopper = stopper or _Stopper()
    offset: int | None = None
    cycles = 0
    logger.info("telegram_bot_started")

    while not stopper.stopped:
        if max_cycles is not None and cycles >= max_cycles:
            return
        cycles += 1
        try:
            updates = client.get_updates(offset)
        except Exception as error:
            # A poll failure must never end the process: the dashboard would
            # keep offering a login that silently never completes.
            logger.warning("telegram_poll_failed", exc_info=error)
            time.sleep(ERROR_BACKOFF_SECONDS)
            continue

        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                # Advance first: a message this bot cannot process must not be
                # redelivered forever.
                offset = update_id + 1
            db = SessionLocal()
            try:
                reply = handle_update(db, update)
            except Exception:
                logger.exception("telegram_update_failed", extra={"update_id": update_id})
                reply = None
            finally:
                db.close()
            if reply is not None:
                try:
                    client.send_message(reply.chat_id, reply.text)
                except Exception:
                    # The binding already happened; a failed reply only costs
                    # the human their confirmation message.
                    logger.warning("telegram_reply_failed", exc_info=True)

    logger.info("telegram_bot_stopped")


def configure_logging() -> None:
    """Separate from ``main`` so it is testable without starting a poll loop."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request line at INFO, and the bot token sits in the URL
    # path — that puts the credential in plaintext in the container logs. The
    # request line tells us nothing the bot does not already log itself.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> int:
    configure_logging()
    client = client_from_settings()
    if client is None:
        logger.error(
            "telegram_bot_not_configured: set TELEGRAM_BOT_TOKEN (scripts/setup-telegram-login.sh)"
        )
        return 1
    if not settings.telegram_bot_username:
        logger.warning("telegram_bot_username_unset: the dashboard cannot build a login link")

    with SessionLocal() as db:
        # Idempotent, and it runs here rather than in the API so a fresh
        # deployment has an approvable admin before anyone tries to log in.
        admin = dashboard_auth.ensure_bootstrap_admin(db)
    if admin is None:
        logger.warning(
            "dashboard_bootstrap_admin_unset: the first login will have nobody able to approve it"
        )

    stopper = _Stopper()
    signal.signal(signal.SIGTERM, stopper)
    signal.signal(signal.SIGINT, stopper)
    run(client, stopper)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
