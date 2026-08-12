"""Minimal OpenRouter chat-completions client.

Only the one call shape the placement service needs: a single-turn completion
that must come back as JSON. Everything else OpenRouter offers is deliberately
absent — a thin, readable client is easier to reason about than a general SDK
when exactly one caller exists.
"""

import json
import logging
from typing import Any

import httpx

from app.config import settings
from app.connectors.http_limits import ResponseTooLargeError, request_limited_http_response

logger = logging.getLogger(__name__)


class OpenRouterError(RuntimeError):
    """The model could not be reached, or did not answer usefully."""


class OpenRouterNotConfigured(OpenRouterError):
    """No API key is set, so the feature is off rather than broken."""


def is_configured() -> bool:
    return bool(settings.openrouter_api_key)


def _strip_code_fence(text: str) -> str:
    """Remove a ```json fence if the model wrapped its answer in one.

    Instruction-tuned models add fences even when told not to, and a fenced but
    otherwise perfect answer is not a failure worth discarding.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped[3:]
    if body.lower().startswith("json"):
        body = body[4:]
    fence_end = body.rfind("```")
    return (body[:fence_end] if fence_end != -1 else body).strip()


def complete_json(
    *,
    system_prompt: str,
    user_prompt: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Run one completion and return the JSON object the model produced.

    Raises `OpenRouterError` for transport failures, non-2xx responses, and
    answers that are not a JSON object. The caller treats every one of those the
    same way, so they share an exception type; the message keeps them apart in
    logs.
    """
    if not is_configured():
        raise OpenRouterNotConfigured("OPENROUTER_API_KEY is not set")

    payload = {
        "model": settings.placement_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # The task is extraction, not composition: the passage and the anchor
        # both have to be copied out of the article verbatim, and sampling is
        # what makes a model paraphrase instead.
        "temperature": 0.0,
        "max_tokens": settings.placement_max_output_tokens,
        # Honoured where the model supports it. The prompt asks for JSON too, and
        # the parser tolerates a fence, so a model that ignores this still works.
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "X-Title": "LinkMesh",
    }
    url = f"{settings.openrouter_base_url.rstrip('/')}/chat/completions"

    owned = client is None
    http = client or httpx.Client(timeout=settings.placement_timeout_seconds)
    try:
        response = request_limited_http_response(
            http,
            "POST",
            url,
            max_bytes=1_000_000,
            json=payload,
            headers=headers,
        )
    except (httpx.HTTPError, ResponseTooLargeError) as exc:
        raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc
    finally:
        if owned:
            http.close()

    if response.status_code >= 400:
        # The body carries OpenRouter's own error message (bad model slug, no
        # credit, rate limit); truncated because it is going into a log line.
        raise OpenRouterError(f"OpenRouter returned {response.status_code}: {response.text[:500]}")

    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(f"unexpected OpenRouter response shape: {exc}") from exc

    try:
        parsed = json.loads(_strip_code_fence(content or ""))
    except ValueError as exc:
        raise OpenRouterError(f"model did not return JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise OpenRouterError(f"model returned {type(parsed).__name__}, not a JSON object")
    return parsed
