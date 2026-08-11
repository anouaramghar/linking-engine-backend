"""Small, bounded client for Tavily's Search API."""

import math
from collections.abc import Sequence
from time import sleep
from typing import Any

import httpx

from app.config import settings
from app.ml.external.base import (
    ExternalSearchError,
    ExternalSearchNotConfigured,
    ExternalSearchQuotaExceeded,
    ExternalSearchRateLimited,
    ExternalSearchResponse,
    ExternalSearchResponseError,
    ExternalSearchResult,
    ExternalSearchTransientError,
)

TAVILY_API_MAX_RESULTS = 20
TAVILY_PROJECT_MAX_RESULTS = 5
TAVILY_MAX_QUERY_CHARS = 1_500
TAVILY_MAX_EXCLUDE_DOMAINS = 150
TAVILY_RETRY_DELAYS_SECONDS = (1.0, 2.0)


def is_configured() -> bool:
    """Return whether the process has credentials for Tavily."""

    return bool(settings.tavily_api_key.strip())


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _provider_score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    if not math.isfinite(score):
        return None
    return min(1.0, max(0.0, score))


def _response_time(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _credits_used(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    credits = value.get("credits")
    if isinstance(credits, bool) or not isinstance(credits, int) or credits < 0:
        return None
    return credits


def _excluded_domains(values: Sequence[str]) -> list[str]:
    return sorted(
        {value.strip().lower() for value in values if value.strip()}
    )[:TAVILY_MAX_EXCLUDE_DOMAINS]


class TavilySearchProvider:
    """Discover web candidates through Tavily without leaking its wire format.

    Search depth and optional response fields are intentionally fixed. This is
    a paid gap-filler, so every request uses the one-credit basic search and
    asks only for the fields the linking pipeline can consume.
    """

    name = "tavily"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        max_results_per_request: int | None = None,
        retry_delays_seconds: Sequence[float] = TAVILY_RETRY_DELAYS_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = settings.tavily_api_key if api_key is None else api_key
        self.base_url = (base_url or settings.tavily_base_url).rstrip("/")
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.tavily_timeout_seconds
        )
        self.max_results_per_request = (
            max_results_per_request
            if max_results_per_request is not None
            else settings.tavily_max_results_per_request
        )
        if not 1 <= self.max_results_per_request <= TAVILY_PROJECT_MAX_RESULTS:
            raise ValueError(
                "max_results_per_request must be between 1 and "
                f"{TAVILY_PROJECT_MAX_RESULTS}"
            )
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.retry_delays_seconds = tuple(float(delay) for delay in retry_delays_seconds)
        if any(delay < 0 for delay in self.retry_delays_seconds):
            raise ValueError("retry delays must not be negative")
        self.client = client

    def search(
        self,
        query: str,
        *,
        max_results: int,
        exclude_domains: Sequence[str] = (),
    ) -> ExternalSearchResponse:
        """Search Tavily and map its response to the provider-neutral contract."""

        normalized_query = query.strip()[:TAVILY_MAX_QUERY_CHARS]
        if not normalized_query:
            raise ValueError("query must not be empty")
        if not 1 <= max_results <= TAVILY_API_MAX_RESULTS:
            raise ValueError(f"max_results must be between 1 and {TAVILY_API_MAX_RESULTS}")
        if not self.api_key.strip():
            raise ExternalSearchNotConfigured("TAVILY_API_KEY is not set")

        effective_limit = min(max_results, self.max_results_per_request)
        payload = {
            "query": normalized_query,
            "search_depth": "basic",
            "topic": "general",
            "max_results": effective_limit,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "auto_parameters": False,
            "exclude_domains": _excluded_domains(exclude_domains),
            "include_usage": True,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        owned = self.client is None
        http = self.client or httpx.Client(timeout=self.timeout_seconds, trust_env=False)
        try:
            for attempt in range(1, len(self.retry_delays_seconds) + 2):
                transient_error: ExternalSearchError | None = None
                try:
                    response = http.post(
                        f"{self.base_url}/search",
                        json=payload,
                        headers=headers,
                    )
                    self._raise_for_status(response)
                except httpx.TransportError as exc:
                    transient_error = ExternalSearchTransientError(
                        f"Tavily transport failed: {exc}"
                    )
                except (ExternalSearchRateLimited, ExternalSearchTransientError) as exc:
                    transient_error = exc
                else:
                    return self._parse_response(
                        response,
                        query=normalized_query,
                        limit=effective_limit,
                        attempt_count=attempt,
                    )

                if attempt > len(self.retry_delays_seconds):
                    assert transient_error is not None
                    raise transient_error
                sleep(self.retry_delays_seconds[attempt - 1])
        finally:
            if owned:
                http.close()

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 429:
            raise ExternalSearchRateLimited("Tavily rate limit reached")
        if response.status_code in {432, 433}:
            raise ExternalSearchQuotaExceeded(
                f"Tavily quota or plan limit reached ({response.status_code})"
            )
        if response.status_code >= 500:
            raise ExternalSearchTransientError(
                f"Tavily returned temporary error {response.status_code}"
            )
        if response.status_code >= 400:
            raise ExternalSearchError(
                f"Tavily returned {response.status_code}: {response.text[:500]}"
            )

    def _parse_response(
        self,
        response: httpx.Response,
        *,
        query: str,
        limit: int,
        attempt_count: int,
    ) -> ExternalSearchResponse:
        try:
            body: Any = response.json()
        except ValueError as exc:
            raise ExternalSearchResponseError("Tavily did not return JSON") from exc
        if not isinstance(body, dict):
            raise ExternalSearchResponseError("Tavily response is not a JSON object")

        raw_results = body.get("results", [])
        if not isinstance(raw_results, list):
            raise ExternalSearchResponseError("Tavily response 'results' is not a list")

        results: list[ExternalSearchResult] = []
        for rank, item in enumerate(raw_results, start=1):
            if len(results) >= limit:
                break
            if not isinstance(item, dict):
                continue
            url = _optional_string(item.get("url"))
            if url is None:
                continue
            results.append(
                ExternalSearchResult(
                    title=_optional_string(item.get("title")) or url,
                    url=url,
                    snippet=_optional_string(item.get("content")) or "",
                    provider_score=_provider_score(item.get("score")),
                    provider_result_id=_optional_string(item.get("id")),
                    metadata={"rank": rank},
                )
            )

        return ExternalSearchResponse(
            provider=self.name,
            query=query,
            results=tuple(results),
            request_id=_optional_string(body.get("request_id")),
            response_time_seconds=_response_time(body.get("response_time")),
            credits_used=_credits_used(body.get("usage")),
            attempt_count=attempt_count,
        )
