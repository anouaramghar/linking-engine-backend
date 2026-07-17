"""Crawl-target URL validation (Phase 0, finding #1 — SSRF).

Every URL the crawler touches — base URLs, redirect hops, child sitemaps, page
URLs — must be http(s), credential-free, and point at a public address.
`request_guard` installs the check as an httpx request event hook, which fires
for every request in a redirect chain, so redirect targets are validated
exactly like first-party requests.

DNS is checked at validation time. A host that swaps records between
validation and connect (DNS rebinding) is out of scope here and would need
connection-time IP pinning.
"""

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UnsafeURLError(Exception):
    """URL failed crawl-safety validation."""


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _addresses(host: str) -> list[_IPAddress]:
    if _is_ip_literal(host):
        return [ipaddress.ip_address(host)]
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURLError(f"cannot resolve host {host!r}") from e
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def validate_url(
    url: str,
    *,
    allow_private: bool = False,
    require_https: bool = False,
    same_host_as: str | None = None,
    resolve_dns: bool = True,
) -> None:
    """Raise UnsafeURLError unless `url` is a safe crawl target.

    resolve_dns=False limits the address check to IP literals — used at site
    creation, where the crawl-time hook re-validates with full resolution.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError(f"unsupported scheme {parts.scheme!r}: {url!r}")
    if require_https and parts.scheme != "https":
        raise UnsafeURLError(f"HTTPS required: {url!r}")
    if parts.username or parts.password:
        raise UnsafeURLError(f"credentials in URL: {url!r}")
    host = parts.hostname
    if not host:
        raise UnsafeURLError(f"no host: {url!r}")
    if same_host_as is not None:
        expected = urlsplit(same_host_as).hostname or ""
        if host.lower() != expected.lower():
            raise UnsafeURLError(f"{url!r} is not on host {expected!r}")
    if allow_private or (not resolve_dns and not _is_ip_literal(host)):
        return
    for addr in _addresses(host):
        # is_global is False for loopback, private, link-local (incl. cloud
        # metadata 169.254.169.254), CGNAT, multicast, reserved, unspecified
        if not addr.is_global:
            raise UnsafeURLError(f"{url!r} resolves to non-public address {addr}")


def request_guard(
    *, allow_private: bool = False, require_https: bool = False
) -> Callable[[httpx.Request], None]:
    """httpx request event hook — validates every request, including redirect hops."""

    def _check(request: httpx.Request) -> None:
        validate_url(str(request.url), allow_private=allow_private, require_https=require_https)

    return _check
