import json

import httpx
import pytest

from app.ml.external.base import (
    ExternalSearchError,
    ExternalSearchNotConfigured,
    ExternalSearchProvider,
    ExternalSearchQuotaExceeded,
    ExternalSearchRateLimited,
    ExternalSearchResponseError,
)
from app.ml.external.tavily import TavilySearchProvider


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_search_uses_bounded_basic_request_and_maps_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://api.tavily.com/search"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {
            "query": "how internal links help SEO",
            "search_depth": "basic",
            "topic": "general",
            "max_results": 3,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "auto_parameters": False,
            "exclude_domains": ["blocked.example", "competitor.example"],
            "include_usage": True,
        }
        return httpx.Response(
            200,
            json={
                "query": "how internal links help SEO",
                "request_id": "req-123",
                "response_time": 0.42,
                "usage": {"credits": 1},
                "results": [
                    {
                        "id": "result-1",
                        "title": "Internal linking guide",
                        "url": "https://example.com/internal-links",
                        "content": "A practical guide to internal link structure.",
                        "score": 0.91,
                    },
                    {
                        "title": "  ",
                        "url": "https://example.org/seo",
                        "content": None,
                        "score": 3.0,
                    },
                    {"title": "Missing URL"},
                ],
            },
        )

    with _client(handler) as client:
        provider = TavilySearchProvider(
            api_key="test-key",
            max_results_per_request=3,
            client=client,
        )
        response = provider.search(
            "  how internal links help SEO  ",
            max_results=10,
            exclude_domains=(
                "competitor.example",
                "BLOCKED.example",
                "blocked.example",
            ),
        )

    assert isinstance(provider, ExternalSearchProvider)
    assert response.provider == "tavily"
    assert response.query == "how internal links help SEO"
    assert response.request_id == "req-123"
    assert response.response_time_seconds == 0.42
    assert response.credits_used == 1
    assert response.attempt_count == 1
    assert len(response.results) == 2
    assert response.results[0].title == "Internal linking guide"
    assert response.results[0].provider_score == 0.91
    assert response.results[0].provider_result_id == "result-1"
    assert response.results[0].metadata == {"rank": 1}
    assert response.results[1].title == "https://example.org/seo"
    assert response.results[1].snippet == ""
    assert response.results[1].provider_score == 1.0


def test_missing_api_key_fails_before_network_call() -> None:
    provider = TavilySearchProvider(api_key="")

    with pytest.raises(ExternalSearchNotConfigured, match="TAVILY_API_KEY"):
        provider.search("internal linking", max_results=3)


def test_transport_failure_is_wrapped() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _client(handler) as client:
        provider = TavilySearchProvider(api_key="test-key", retry_delays_seconds=(), client=client)
        with pytest.raises(ExternalSearchError, match="Tavily transport failed"):
            provider.search("internal linking", max_results=3)


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (429, ExternalSearchRateLimited),
        (432, ExternalSearchQuotaExceeded),
        (433, ExternalSearchQuotaExceeded),
        (500, ExternalSearchError),
    ],
)
def test_provider_errors_are_classified(status_code: int, error_type: type[Exception]) -> None:
    with _client(lambda _request: httpx.Response(status_code, text="provider error")) as client:
        provider = TavilySearchProvider(api_key="test-key", retry_delays_seconds=(), client=client)
        with pytest.raises(error_type):
            provider.search("internal linking", max_results=3)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"results": {}}),
    ],
)
def test_malformed_provider_responses_are_rejected(response: httpx.Response) -> None:
    with _client(lambda _request: response) as client:
        provider = TavilySearchProvider(api_key="test-key", client=client)
        with pytest.raises(ExternalSearchResponseError):
            provider.search("internal linking", max_results=3)


@pytest.mark.parametrize("query", ["", "   "])
def test_empty_query_is_rejected(query: str) -> None:
    provider = TavilySearchProvider(api_key="test-key")

    with pytest.raises(ValueError, match="query must not be empty"):
        provider.search(query, max_results=3)


@pytest.mark.parametrize("max_results", [0, 21])
def test_api_result_limit_is_enforced(max_results: int) -> None:
    provider = TavilySearchProvider(api_key="test-key")

    with pytest.raises(ValueError, match="max_results must be between"):
        provider.search("internal linking", max_results=max_results)


def test_non_positive_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be greater than zero"):
        TavilySearchProvider(api_key="test-key", timeout_seconds=0)


def test_project_result_cap_cannot_exceed_five() -> None:
    with pytest.raises(ValueError, match="between 1 and 5"):
        TavilySearchProvider(api_key="test-key", max_results_per_request=6)


def test_transient_failures_retry_with_bounded_backoff() -> None:
    statuses = iter((429, 500, 200))

    def handler(_request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return (
            httpx.Response(status, text="temporary")
            if status != 200
            else httpx.Response(200, json={"results": [], "usage": {"credits": 1}})
        )

    with _client(handler) as client:
        provider = TavilySearchProvider(
            api_key="test-key",
            retry_delays_seconds=(0, 0),
            client=client,
        )
        response = provider.search("internal linking", max_results=3)

    assert response.attempt_count == 3
    assert response.credits_used == 1


def test_transport_failure_is_retried() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"results": []})

    with _client(handler) as client:
        provider = TavilySearchProvider(
            api_key="test-key",
            retry_delays_seconds=(0,),
            client=client,
        )
        response = provider.search("internal linking", max_results=3)

    assert calls == 2
    assert response.attempt_count == 2


def test_query_and_excluded_domains_are_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert len(payload["query"]) == 1_500
        assert len(payload["exclude_domains"]) == 150
        return httpx.Response(200, json={"results": []})

    with _client(handler) as client:
        provider = TavilySearchProvider(api_key="test-key", client=client)
        response = provider.search(
            "x" * 2_000,
            max_results=3,
            exclude_domains=[f"domain-{index}.example" for index in range(200)],
        )

    assert len(response.query) == 1_500
