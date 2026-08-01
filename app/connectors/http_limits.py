"""Bounded HTTP response reads for untrusted content-pool sources."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx


class ResponseTooLargeError(ValueError):
    """A remote response exceeded the configured decoded-body limit."""


@dataclass(frozen=True)
class LimitedResponse:
    content: bytes
    content_type: str | None


def get_limited_response(
    client: httpx.Client,
    url: str,
    *,
    max_bytes: int,
    params: Mapping[str, Any] | None = None,
) -> LimitedResponse:
    """GET a response without allowing an untrusted body to grow without bound."""
    with client.stream("GET", url, params=params) as response:
        response.raise_for_status()
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = None
            if declared_size is not None and declared_size > max_bytes:
                raise ResponseTooLargeError(
                    f"remote response declares {declared_size} bytes; limit is {max_bytes}"
                )

        body = bytearray()
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > max_bytes:
                raise ResponseTooLargeError(
                    f"remote response exceeds the {max_bytes}-byte decoded-body limit"
                )
            body.extend(chunk)
        return LimitedResponse(
            content=bytes(body),
            content_type=response.headers.get("content-type"),
        )
