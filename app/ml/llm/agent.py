"""OpenRouter chat-completions with tool calling, for the operator assistant.

A second client next to ``openrouter.py``, for the same reason that module
exists in the singular: a thin, readable client is easier to reason about than
a general SDK. The shape it adds is exactly one — a multi-message conversation
that may carry tool definitions and may answer with tool calls. Streaming is
deliberately absent; the side panel renders complete turns.
"""

import logging
from typing import Any

import httpx

from app.config import settings
from app.connectors.http_limits import ResponseTooLargeError, request_limited_http_response
from app.ml.llm.openrouter import OpenRouterError, is_configured

logger = logging.getLogger(__name__)


def chat_with_tools(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run one model turn. Returns the assistant message dict verbatim.

    The caller inspects ``message["tool_calls"]`` and either executes them or
    treats ``message["content"]`` as the final answer. Raises `OpenRouterError`
    for transport failures, non-2xx responses, and unusable bodies.
    """
    if not is_configured():
        raise OpenRouterError("OPENROUTER_API_KEY is not set")

    payload = {
        "model": settings.agent_model,
        "messages": messages,
        # The assistant reports operational counts and ids. Keep sampling off so
        # a free routed model is less likely to rewrite a number after a tool call.
        "temperature": 0.0,
        "max_tokens": settings.agent_max_output_tokens,
    }
    if tools:
        payload["tools"] = tools

    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "X-Title": "LinkMesh",
    }
    url = f"{settings.openrouter_base_url.rstrip('/')}/chat/completions"

    with httpx.Client(timeout=settings.agent_timeout_seconds) as http:
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

    if response.status_code >= 400:
        raise OpenRouterError(f"OpenRouter returned {response.status_code}: {response.text[:500]}")

    try:
        body = response.json()
        return body["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(f"unexpected OpenRouter response shape: {exc}") from exc
