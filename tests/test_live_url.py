from __future__ import annotations

import httpx
import pytest

from app.connectors.url_guard import UnsafeURLError
from app.services.live_url import LiveURLChecker


def _checker(
    handler,
    *,
    validator=None,
    max_redirects=5,
    timeout_seconds=1,
    clock=None,
) -> LiveURLChecker:
    kwargs = {"clock": clock} if clock is not None else {}
    return LiveURLChecker(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validator=validator or (lambda _url: None),
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
        **kwargs,
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_live_https_target_passes_without_reading_a_body() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, content=b"body that the checker must not need")

    result = _checker(handler).check("https://Example.com/report?utm_source=test#part")

    assert result.eligible is True
    assert result.final_url == "https://example.com/report"
    assert result.status_code == 200
    assert result.redirect_count == 0
    assert methods == ["HEAD"]


def test_head_rejection_falls_back_to_a_streaming_get() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(405 if request.method == "HEAD" else 204)

    result = _checker(handler).check("https://example.com/report")

    assert result.eligible is True
    assert result.status_code == 204
    assert methods == ["HEAD", "GET"]


def test_dead_target_is_blocked_with_its_status() -> None:
    result = _checker(lambda _request: httpx.Response(404)).check("https://example.com/missing")

    assert result.eligible is False
    assert result.reason_code == "http_status"
    assert result.reason == "target returned HTTP 404"
    assert result.status_code == 404


def test_timeout_is_a_bounded_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    result = _checker(handler).check("https://example.com/slow")

    assert result.eligible is False
    assert result.reason_code == "timeout"
    assert result.status_code is None


def test_custom_client_cannot_silently_disable_url_safety() -> None:
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))) as client,
        pytest.raises(ValueError, match="explicit safety validator"),
    ):
        LiveURLChecker(client=client)


def test_redirects_share_one_total_wall_clock_deadline() -> None:
    clock = _Clock()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        clock.advance(0.6)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(204)

    result = _checker(handler, timeout_seconds=1, clock=clock).check("https://example.com/start")

    assert result.eligible is False
    assert result.reason_code == "timeout"
    assert result.redirect_count == 1
    assert requested == ["https://example.com/start", "https://example.com/final"]


def test_safe_redirect_is_followed_and_final_url_is_normalized() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new?fbclid=tracking"})
        return httpx.Response(200)

    result = _checker(handler).check("https://example.com/old")

    assert result.eligible is True
    assert result.final_url == "https://example.com/new"
    assert result.redirect_count == 1
    assert seen == ["https://example.com/old", "https://example.com/new?fbclid=tracking"]


def test_redirect_hop_passes_the_network_guard_before_request() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://127.0.0.1/admin"})

    def validator(url: str) -> None:
        if "127.0.0.1" in url:
            raise UnsafeURLError("private")

    result = _checker(handler, validator=validator).check("https://example.com/start")

    assert result.eligible is False
    assert result.reason_code == "unsafe_url"
    assert result.redirect_count == 1
    assert requested == ["https://example.com/start"]


def test_policy_is_applied_to_every_redirect_hop_before_request() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://blocked.example/final"})

    def policy(url: str) -> tuple[bool, tuple[str, ...]]:
        if "blocked.example" in url:
            return False, ("domain is blocklisted",)
        return True, ()

    result = _checker(handler).check("https://example.com/start", policy_check=policy)

    assert result.eligible is False
    assert result.reason_code == "policy_blocked"
    assert result.reason == "domain is blocklisted"
    assert requested == ["https://example.com/start"]


def test_redirect_chain_has_a_hard_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        step = int(request.url.path.removeprefix("/step/") or "0")
        return httpx.Response(302, headers={"location": f"/step/{step + 1}"})

    result = _checker(handler, max_redirects=2).check("https://example.com/step/0")

    assert result.eligible is False
    assert result.reason_code == "too_many_redirects"
    assert result.redirect_count == 2


def test_score_component_is_json_ready_and_explains_the_verdict() -> None:
    result = _checker(lambda _request: httpx.Response(410)).check("https://example.com/retired")

    component = result.as_score_component()

    assert component["domain"] == "example.com"
    assert component["eligible"] is False
    assert component["reasons"] == ["target returned HTTP 410"]
    assert component["checks"]["reachable"] is False
    assert component["checks"]["http_status"] == 410
    assert component["checks"]["checked_at"].endswith("+00:00")
