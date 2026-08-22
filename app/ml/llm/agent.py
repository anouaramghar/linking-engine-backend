"""OpenRouter chat-completions with tool calling, for the operator assistant.

A second client next to ``openrouter.py``, for the same reason that module
exists in the singular: a thin, readable client is easier to reason about than
a general SDK. The shape it adds is exactly one — a multi-message conversation
that may carry tool definitions and may answer with tool calls. Streaming is
deliberately absent; the side panel renders complete turns.
"""

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.connectors.http_limits import ResponseTooLargeError, request_limited_http_response
from app.ml.llm.openrouter import OpenRouterError

logger = logging.getLogger(__name__)


#: Hosts whose terms permit development and evaluation only, and the sentence
#: that says so. Pointing the assistant at one is a deliberate development
#: convenience; this exists so it cannot quietly become how production runs.
DEVELOPMENT_ONLY_HOSTS = {
    "integrate.api.nvidia.com": (
        "NVIDIA's API Trial terms permit internal testing and evaluation only, and "
        "count any activity serving real end-users as production"
    ),
}


def provider() -> tuple[str, str]:
    """Where the assistant's model calls go, and what they authenticate with.

    Its own provider when one is set, otherwise the account placement uses.
    The two are separable because the assistant is the part somebody may want
    to run on a free development endpoint, and placement is not.
    """
    base = settings.agent_base_url.strip() or settings.openrouter_base_url
    key = settings.agent_api_key.strip() or settings.openrouter_api_key
    return base.rstrip("/"), key


def provider_host() -> str:
    """The hostname the assistant talks to, for /agent/status and the log."""
    return urlparse(provider()[0]).hostname or ""


def is_configured() -> bool:
    """Whether chat can run at all.

    Distinct from ``openrouter.is_configured``: that answers for placement's
    account, and the assistant may have its own. Asking the wrong one reported
    chat as unavailable on a deployment that had configured only the assistant.
    """
    return bool(provider()[1])


def log_provider_notice() -> None:
    """Say once, at startup, when the assistant runs on a restricted provider."""
    restriction = DEVELOPMENT_ONLY_HOSTS.get(provider_host())
    if restriction is not None:
        logger.warning(
            "assistant provider %s is for development only: %s",
            provider_host(),
            restriction,
        )


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
    base_url, api_key = provider()
    if not api_key:
        raise OpenRouterError("no API key is set for the assistant")

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
        "Authorization": f"Bearer {api_key}",
        "X-Title": "LinkMesh",
    }
    url = f"{base_url}/chat/completions"

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
            raise OpenRouterError(f"{provider_host()} request failed: {exc}") from exc

    if response.status_code >= 400:
        raise OpenRouterError(
            f"{provider_host()} returned {response.status_code}: {response.text[:500]}"
        )

    try:
        body = response.json()
        return body["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(f"unexpected response shape from {provider_host()}: {exc}") from exc
