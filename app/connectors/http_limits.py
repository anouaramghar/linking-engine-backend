"""Bounded HTTP response reads for untrusted content-pool sources."""

from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx

from app.config import settings


class ResponseTooLargeError(ValueError):
    """A remote response exceeded the configured decoded-body limit."""


@dataclass(frozen=True)
class LimitedResponse:
    content: bytes
    content_type: str | None


def check_crawl_deadline(started_at: float) -> None:
    if monotonic() - started_at > settings.crawl_max_duration_seconds:
        raise ValueError(f"crawl exceeded {settings.crawl_max_duration_seconds} seconds")


def _read_limited_body(
    response: httpx.Response,
    max_bytes: int,
    *,
    raise_for_status: bool,
    crawl_started_at: float | None = None,
) -> bytes:
    if raise_for_status:
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
        if crawl_started_at is not None:
            check_crawl_deadline(crawl_started_at)
        if len(body) + len(chunk) > max_bytes:
            raise ResponseTooLargeError(
                f"remote response exceeds the {max_bytes}-byte decoded-body limit"
            )
        body.extend(chunk)
    return bytes(body)


def get_limited_response(
    client: httpx.Client,
    url: str,
    *,
    max_bytes: int,
    params: Mapping[str, Any] | None = None,
) -> LimitedResponse:
    """GET a response without allowing an untrusted body to grow without bound."""
    with client.stream("GET", url, params=params) as response:
        body = _read_limited_body(response, max_bytes, raise_for_status=True)
        return LimitedResponse(
            content=body,
            content_type=response.headers.get("content-type"),
        )


def get_limited_http_response(
    client: httpx.Client,
    url: str,
    *,
    max_bytes: int,
    params: Mapping[str, Any] | None = None,
    crawl_started_at: float | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Return a bounded response while preserving status and headers for callers."""
    return request_limited_http_response(
        client,
        "GET",
        url,
        max_bytes=max_bytes,
        params=params,
        crawl_started_at=crawl_started_at,
        **kwargs,
    )


def request_limited_http_response(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_bytes: int,
    params: Mapping[str, Any] | None = None,
    crawl_started_at: float | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Make a request without buffering an unbounded decoded response body."""
    with client.stream(method, url, params=params, **kwargs) as response:
        content = _read_limited_body(
            response,
            max_bytes,
            raise_for_status=False,
            crawl_started_at=crawl_started_at,
        )
        headers = dict(response.headers)
        for header in ("content-encoding", "content-length", "transfer-encoding"):
            headers.pop(header, None)
        # httpx re-encodes header values as ASCII when constructing a Response,
        # and a CDN can send non-ASCII bytes (wordpress.org's `x-olaf: ☄`).
        # Losing such a header costs nothing here; failing the whole crawl costs
        # a site and a job retry.
        headers = {k: v for k, v in headers.items() if v.isascii()}
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=content,
            request=response.request,
        )
