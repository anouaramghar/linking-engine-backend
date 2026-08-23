"""Signed MCP preview envelopes and opaque one-time action receipts."""

import base64
import hashlib
import hmac
import json
import secrets
import zlib
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from app.config import settings
from app.services.authorization import Principal


def _key(purpose: str) -> bytes:
    secret = settings.api_key_pepper or settings.api_key
    if not secret:
        # Authentication already fails closed without a key. Keep this explicit
        # so a development process cannot accidentally mint unsigned authority.
        raise RuntimeError("API_KEY_PEPPER or API_KEY is required for agent action receipts")
    return hmac.new(secret.encode(), purpose.encode(), hashlib.sha256).digest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def principal_binding(principal: Principal) -> dict[str, Any]:
    return {
        "source": principal.source,
        "is_admin": principal.is_admin,
        "tenant_id": principal.tenant_id,
        "key_id": principal.key_id,
        "operator_id": principal.operator_id,
    }


def principal_from_binding(binding: dict[str, Any]) -> Principal:
    try:
        return Principal(
            source=str(binding["source"]),
            is_admin=bool(binding["is_admin"]),
            tenant_id=binding.get("tenant_id"),
            key_id=binding.get("key_id"),
            operator_id=binding.get("operator_id"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(400, "invalid MCP action identity binding") from error


def sign_preview_envelope(tool: str, arguments: dict, principal: Principal) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.agent_action_envelope_ttl_seconds)
    body = {
        "v": 1,
        "tool": tool,
        "arguments": arguments,
        "principal": principal_binding(principal),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "nonce": secrets.token_urlsafe(12),
    }
    compressed = zlib.compress(_canonical(body), level=9)
    encoded = _b64(compressed)
    signature = _b64(
        hmac.new(_key("agent-action-envelope:v1"), encoded.encode(), hashlib.sha256).digest()
    )
    return f"{encoded}.{signature}", expires_at


def verify_preview_envelope(token: str) -> dict:
    try:
        encoded, supplied = token.split(".", 1)
        expected = _b64(
            hmac.new(_key("agent-action-envelope:v1"), encoded.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("signature")
        body = json.loads(zlib.decompress(_unb64(encoded)))
        if body.get("v") != 1 or not isinstance(body.get("arguments"), dict):
            raise ValueError("version")
        if datetime.now(UTC).timestamp() >= int(body["exp"]):
            raise HTTPException(
                410, "this MCP action link has expired; ask the agent to preview it again"
            )
        principal_from_binding(body["principal"])
        return body
    except HTTPException:
        raise
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, zlib.error) as error:
        raise HTTPException(400, "invalid MCP action link") from error


def proposal_hash(proposal: dict) -> str:
    return hashlib.sha256(_canonical(proposal)).hexdigest()


def new_receipt() -> tuple[str, str]:
    plaintext = "lmar_" + secrets.token_urlsafe(32)
    return plaintext, hash_receipt(plaintext)


def hash_receipt(receipt: str) -> str:
    return hmac.new(_key("agent-action-receipt:v1"), receipt.encode(), hashlib.sha256).hexdigest()
