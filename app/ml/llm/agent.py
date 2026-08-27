"""OpenRouter chat-completions with tool calling, for the operator assistant.

A second client next to ``openrouter.py``, for the same reason that module
exists in the singular: a thin, readable client is easier to reason about than
a general SDK. The shape it adds is exactly one — a multi-message conversation
that may carry tool definitions and may answer with tool calls, in a blocking
form and a streamed one. The two send the same request and end with the same
assistant message; the streamed one only makes the text available while it is
still being written, which is what lets the panel show a sentence rather than a
spinner for the half-minute a turn can take.
"""

import json
import logging
from collections.abc import Generator
from dataclasses import dataclass
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


def _request(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    """The body both clients send. One turn, one shape."""
    payload: dict[str, Any] = {
        "model": settings.agent_model,
        "messages": messages,
        # The assistant reports operational counts and ids. Keep sampling off so
        # a free routed model is less likely to rewrite a number after a tool call.
        "temperature": 0.0,
        "max_tokens": settings.agent_max_output_tokens,
    }
    if tools:
        payload["tools"] = tools
    return payload


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "X-Title": "LinkMesh"}


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

    payload = _request(messages, tools)
    url = f"{base_url}/chat/completions"

    with httpx.Client(timeout=settings.agent_timeout_seconds) as http:
        try:
            response = request_limited_http_response(
                http,
                "POST",
                url,
                max_bytes=1_000_000,
                json=payload,
                headers=_headers(api_key),
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


#: How much of one streamed turn will be read before it is treated as a runaway
#: response. The same cap the blocking client puts on a whole body, applied to
#: the pieces — a stream is not a reason to accept an unbounded one.
STREAM_BYTE_BUDGET = 1_000_000


@dataclass(frozen=True)
class ReasoningText:
    """A fragment of the model's thinking, wrapped so it cannot pass for reply.

    A reasoning model writes for a long time before its first word of answer —
    measured at 19s of thinking on this deployment's provider — and sends that
    thinking in its own delta field. Read plainly it looks exactly like text
    arriving, which is why it is wrapped: the type is what stops it being
    appended to ``content``, replayed to the provider as something the
    assistant said, or shown to the operator as the answer.
    """

    text: str


def stream_chat_with_tools(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> Generator[str | ReasoningText | None, None, dict[str, Any]]:
    """Run one model turn over SSE. Yields text as it arrives; returns the message.

    The return value is the same assistant message `chat_with_tools` hands back
    — ``tool_calls`` to execute, or ``content`` as the final answer — so the
    loop above does not care which client produced its turn. What the yields add
    is timing: the panel can render a sentence while the rest of it is still
    being written.

    Three kinds of yield, and the type tells them apart: a ``str`` is reply
    text, a `ReasoningText` is the model thinking out loud, and ``None`` is a
    transport keep-alive that is not text at all.

    The generator owns the connection for as long as it is iterated, and closing
    it early closes the response. Raises `OpenRouterError` for transport
    failures, non-2xx responses, and frames that cannot be read.
    """
    base_url, api_key = provider()
    if not api_key:
        raise OpenRouterError("no API key is set for the assistant")

    payload = _request(messages, tools)
    payload["stream"] = True
    url = f"{base_url}/chat/completions"

    content: list[str] = []
    #: Tool calls arrive in fragments addressed by ``index``; this assembles them.
    calls: dict[int, dict[str, Any]] = {}
    read = 0

    with httpx.Client(timeout=settings.agent_timeout_seconds) as http:
        try:
            with http.stream(
                "POST",
                url,
                json=payload,
                headers={**_headers(api_key), "Accept": "text/event-stream"},
            ) as response:
                if response.status_code >= 400:
                    # The body is the useful half of a provider error and it is
                    # small, but it has not been read yet on a streamed request.
                    detail = response.read().decode("utf-8", "replace")[:500]
                    raise OpenRouterError(
                        f"{provider_host()} returned {response.status_code}: {detail}"
                    )
                for line in response.iter_lines():
                    read += len(line)
                    if read > STREAM_BYTE_BUDGET:
                        raise OpenRouterError(
                            f"{provider_host()} streamed past the {STREAM_BYTE_BUDGET}-byte limit"
                        )
                    # Blank separators are punctuation. Comment lines are
                    # provider keep-alives (OpenRouter's
                    # ": OPENROUTER PROCESSING"): preserve those as a
                    # transport-only heartbeat so the dashboard can reset its
                    # idle timer while the model is warming up.
                    if line.startswith(":"):
                        yield None
                        continue
                    if not line.startswith("data:"):
                        continue
                    frame = line[len("data:") :].strip()
                    if frame == "[DONE]":
                        break
                    folded = _fold_frame(_parse_frame(frame), content, calls)
                    if folded is not None:
                        yield folded
        except (httpx.HTTPError, ResponseTooLargeError) as exc:
            raise OpenRouterError(f"{provider_host()} request failed: {exc}") from exc

    # ``content`` stays absent rather than empty when the turn was only tool
    # calls: that is the shape the blocking client returns, and the shape the
    # provider expects to see again in the next request's transcript.
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content) or None}
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    return message


def _parse_frame(frame: str) -> dict[str, Any]:
    try:
        chunk = json.loads(frame)
    except ValueError as exc:
        raise OpenRouterError(f"unreadable stream frame from {provider_host()}: {exc}") from exc
    if not isinstance(chunk, dict):
        raise OpenRouterError(f"unexpected stream frame from {provider_host()}: {frame[:200]}")
    # A provider may fail *inside* a 200 stream — a rate limit reached mid-turn
    # arrives as a frame, not as a status code. Treat it as the failure it is
    # rather than ending the reply wherever the error interrupted it.
    error = chunk.get("error")
    if isinstance(error, dict):
        raise OpenRouterError(f"{provider_host()} stream error: {error.get('message', error)}")
    return chunk


#: Where a reasoning model puts its thinking. ``reasoning_content`` is what
#: NVIDIA NIM, vLLM, and DeepSeek send; ``reasoning`` is OpenRouter's name for
#: the same thing. Neither is part of the OpenAI schema this client otherwise
#: follows, so both are read and neither is required.
_REASONING_FIELDS = ("reasoning_content", "reasoning")


def _fold_frame(
    chunk: dict[str, Any],
    content: list[str],
    calls: dict[int, dict[str, Any]],
) -> str | ReasoningText | None:
    """Fold one frame into the message being assembled; return what it added.

    ``None`` for a frame that added nothing the caller can show — metadata, or
    a tool-call fragment, which is reported only once the whole call is
    assembled. Reply text is returned as ``str`` and accumulates into
    ``content``; thinking is returned wrapped and accumulates nowhere, because
    it is not part of the message the provider will be sent back.

    Tool-call fragments are merged by index rather than appended as they come:
    the id and the function name ride the first fragment for a call, and its
    JSON arguments accumulate across every following one, so a call is only
    complete once the stream is.
    """
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        # Usage-only and metadata frames carry no choices at all.
        return None
    delta = choices[0].get("delta") or {}

    for fragment in delta.get("tool_calls") or []:
        index = fragment.get("index", 0)
        call = calls.setdefault(
            index,
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if fragment.get("id"):
            call["id"] = fragment["id"]
        function = fragment.get("function") or {}
        if function.get("name"):
            call["function"]["name"] = function["name"]
        if function.get("arguments"):
            call["function"]["arguments"] += function["arguments"]

    text = delta.get("content")
    if isinstance(text, str) and text:
        content.append(text)
        return text

    # Checked after content, never before: a frame carrying both is a frame
    # whose answer text has started, and the answer is the thing to show.
    for field in _REASONING_FIELDS:
        thinking = delta.get(field)
        if isinstance(thinking, str) and thinking:
            return ReasoningText(thinking)
    return None
