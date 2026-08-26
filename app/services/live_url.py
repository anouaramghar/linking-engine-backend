"""Bounded, SSRF-safe liveness checks for external link targets.

Static URL policy answers whether LinkMesh *may* link to a domain.  This module
answers the separate question of whether the concrete target is reachable now.
Redirects are followed manually so every hop can pass both the network guard and
the caller's site policy before LinkMesh connects to it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Self
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import settings
from app.connectors.url_guard import (
    SSRFProtectedTransport,
    UnsafeURLError,
    network_deadline,
    validate_url,
)
from app.domain_policy import domain_from_url
from app.ml.external.cleaning import normalize_external_url

PolicyCheck = Callable[[str], tuple[bool, tuple[str, ...]]]
URLValidator = Callable[[str], None]

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_GET_FALLBACK_STATUSES = frozenset({403, 405, 501})
_USER_AGENT = "LinkMesh/0.1 (+live-url-check)"


@dataclass(frozen=True, slots=True)
class LiveURLCheck:
    """One immutable observation of a candidate URL."""

    original_url: str
    final_url: str | None
    eligible: bool
    status_code: int | None
    redirect_count: int
    checked_at: datetime
    reason_code: str | None = None
    reason: str | None = None

    def as_score_component(self) -> dict:
        observed_url = self.final_url or self.original_url
        try:
            domain = domain_from_url(observed_url)
        except ValueError:
            domain = "invalid"
        return {
            "domain": domain,
            "eligible": self.eligible,
            "reasons": [self.reason] if self.reason else [],
            "checks": {
                "reachable": self.eligible,
                "https": urlsplit(observed_url).scheme.lower() == "https",
                "http_status": self.status_code,
                "redirect_count": self.redirect_count,
                "final_url": self.final_url,
                "checked_at": self.checked_at.isoformat(),
            },
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class _ResponseHead:
    status_code: int
    location: str | None


class LiveURLChecker:
    """Reusable synchronous checker with a pooled, connect-time-safe client."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        validator: URLValidator | None = None,
        timeout_seconds: float | None = None,
        max_redirects: int | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if client is not None and validator is None:
            raise ValueError("a custom live-URL client requires an explicit safety validator")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=SSRFProtectedTransport(),
            follow_redirects=False,
            trust_env=False,
        )
        self._validator = validator or self._validate_public_https
        self._clock = clock
        self._timeout_seconds = (
            settings.live_url_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        self._max_redirects = (
            settings.live_url_max_redirects if max_redirects is None else max_redirects
        )

    @staticmethod
    def _validate_public_https(url: str) -> None:
        # The pinned transport resolves and validates every address at connect
        # time. Avoid a second unbounded DNS lookup in this shape-only preflight.
        validate_url(url, require_https=True, resolve_dns=False)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _request(self, method: str, url: str, *, timeout_seconds: float) -> _ResponseHead:
        # Streaming is deliberate: liveness needs response headers, not a PDF or
        # a multi-megabyte page body supplied by an untrusted candidate.
        with self._client.stream(
            method,
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
            timeout=timeout_seconds,
        ) as response:
            return _ResponseHead(
                status_code=response.status_code,
                location=response.headers.get("location"),
            )

    def _failure(
        self,
        original_url: str,
        *,
        final_url: str | None,
        status_code: int | None,
        redirect_count: int,
        checked_at: datetime,
        reason_code: str,
        reason: str,
    ) -> LiveURLCheck:
        return LiveURLCheck(
            original_url=original_url,
            final_url=final_url,
            eligible=False,
            status_code=status_code,
            redirect_count=redirect_count,
            checked_at=checked_at,
            reason_code=reason_code,
            reason=reason,
        )

    def check(self, url: str, *, policy_check: PolicyCheck | None = None) -> LiveURLCheck:
        """Check one URL without downloading its response body.

        ``policy_check`` runs before every request, including redirect targets.
        It keeps blocklists and owned-domain isolation at the same boundary as
        the SSRF guard instead of discovering a forbidden hop after connecting.
        """

        deadline = self._clock() + self._timeout_seconds
        with network_deadline(deadline, clock=self._clock):
            return self._check_until(url, policy_check=policy_check, deadline=deadline)

    def _check_until(
        self,
        url: str,
        *,
        policy_check: PolicyCheck | None,
        deadline: float,
    ) -> LiveURLCheck:
        checked_at = datetime.now(UTC)
        original_url = url
        current_url = url
        redirect_count = 0
        method = "HEAD"

        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return self._failure(
                    original_url,
                    final_url=current_url,
                    status_code=None,
                    redirect_count=redirect_count,
                    checked_at=checked_at,
                    reason_code="timeout",
                    reason="target did not complete before the total live-check deadline",
                )
            try:
                self._validator(current_url)
            except (UnsafeURLError, ValueError):
                return self._failure(
                    original_url,
                    final_url=current_url,
                    status_code=None,
                    redirect_count=redirect_count,
                    checked_at=checked_at,
                    reason_code="unsafe_url",
                    reason="URL failed the public HTTPS safety guard",
                )

            if policy_check is not None:
                allowed, reasons = policy_check(current_url)
                if not allowed:
                    detail = "; ".join(reasons) or "site policy rejected the URL"
                    return self._failure(
                        original_url,
                        final_url=current_url,
                        status_code=None,
                        redirect_count=redirect_count,
                        checked_at=checked_at,
                        reason_code="policy_blocked",
                        reason=detail,
                    )

            try:
                response = self._request(method, current_url, timeout_seconds=remaining)
                if method == "HEAD" and response.status_code in _GET_FALLBACK_STATUSES:
                    method = "GET"
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise httpx.ReadTimeout("total live-check deadline exceeded")
                    response = self._request(method, current_url, timeout_seconds=remaining)
            except UnsafeURLError:
                return self._failure(
                    original_url,
                    final_url=current_url,
                    status_code=None,
                    redirect_count=redirect_count,
                    checked_at=checked_at,
                    reason_code="unsafe_url",
                    reason="URL failed the public HTTPS safety guard",
                )
            except httpx.TimeoutException:
                return self._failure(
                    original_url,
                    final_url=current_url,
                    status_code=None,
                    redirect_count=redirect_count,
                    checked_at=checked_at,
                    reason_code="timeout",
                    reason="target did not respond before the live-check timeout",
                )
            except (httpx.InvalidURL, httpx.UnsupportedProtocol):
                return self._failure(
                    original_url,
                    final_url=current_url,
                    status_code=None,
                    redirect_count=redirect_count,
                    checked_at=checked_at,
                    reason_code="invalid_url",
                    reason="target URL is invalid",
                )
            except httpx.TransportError:
                return self._failure(
                    original_url,
                    final_url=current_url,
                    status_code=None,
                    redirect_count=redirect_count,
                    checked_at=checked_at,
                    reason_code="network_error",
                    reason="target could not be reached securely",
                )

            if self._clock() >= deadline:
                return self._failure(
                    original_url,
                    final_url=current_url,
                    status_code=None,
                    redirect_count=redirect_count,
                    checked_at=checked_at,
                    reason_code="timeout",
                    reason="target did not complete before the total live-check deadline",
                )

            if response.status_code in _REDIRECT_STATUSES:
                if not response.location:
                    return self._failure(
                        original_url,
                        final_url=current_url,
                        status_code=response.status_code,
                        redirect_count=redirect_count,
                        checked_at=checked_at,
                        reason_code="redirect_without_location",
                        reason="target returned a redirect without a Location header",
                    )
                if redirect_count >= self._max_redirects:
                    return self._failure(
                        original_url,
                        final_url=current_url,
                        status_code=response.status_code,
                        redirect_count=redirect_count,
                        checked_at=checked_at,
                        reason_code="too_many_redirects",
                        reason=f"target exceeded {self._max_redirects} safe redirects",
                    )
                current_url = urljoin(current_url, response.location)
                redirect_count += 1
                continue

            try:
                final_url = normalize_external_url(current_url)
            except ValueError:
                return self._failure(
                    original_url,
                    final_url=current_url,
                    status_code=response.status_code,
                    redirect_count=redirect_count,
                    checked_at=checked_at,
                    reason_code="invalid_final_url",
                    reason="final target URL is invalid",
                )

            if 200 <= response.status_code < 300:
                return LiveURLCheck(
                    original_url=original_url,
                    final_url=final_url,
                    eligible=True,
                    status_code=response.status_code,
                    redirect_count=redirect_count,
                    checked_at=checked_at,
                )
            return self._failure(
                original_url,
                final_url=final_url,
                status_code=response.status_code,
                redirect_count=redirect_count,
                checked_at=checked_at,
                reason_code="http_status",
                reason=f"target returned HTTP {response.status_code}",
            )
