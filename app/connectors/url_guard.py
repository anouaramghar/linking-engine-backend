"""Crawl-target validation and connect-time IP pinning (Phase 0 SSRF safety).

Every URL the crawler touches — base URLs, redirect hops, child sitemaps, and
page URLs — must be HTTP(S), credential-free, and point at a permitted address.
`request_guard` cheaply validates URL shape on every redirect hop. The custom
network backend resolves hostnames once at connection time, validates every
answer, and opens the socket directly to an approved address. This mitigates
DNS rebinding while preserving the original hostname for TLS SNI and certificate
verification.
"""

import ipaddress
import select
import socket
import ssl
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
_IPV4_COMPATIBLE_PREFIX = ipaddress.ip_network("::/96")
_IPV4_TRANSLATED_PREFIX = ipaddress.ip_network("::ffff:0:0:0/96")
_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class UnsafeURLError(Exception):
    """URL failed crawl-safety validation."""


def _is_public_address(address: _IPAddress) -> bool:
    """Accept only public unicast, including safe IPv4-embedding forms."""
    if address.is_multicast or (
        isinstance(address, ipaddress.IPv6Address) and address.is_site_local
    ):
        return False

    embedded_ipv4 = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    if (
        isinstance(address, ipaddress.IPv6Address)
        and embedded_ipv4 is None
        and address in _IPV4_COMPATIBLE_PREFIX
    ):
        embedded_ipv4 = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if isinstance(address, ipaddress.IPv6Address) and address in _IPV4_TRANSLATED_PREFIX:
        embedded_ipv4 = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if isinstance(address, ipaddress.IPv6Address) and address in _NAT64_WELL_KNOWN_PREFIX:
        embedded_ipv4 = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    candidate: _IPAddress = embedded_ipv4 if embedded_ipv4 is not None else address
    return candidate.is_global and not candidate.is_multicast


class _SocketStream(httpcore.NetworkStream):
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            self._sock.settimeout(timeout)
            return self._sock.recv(max_bytes)
        except socket.timeout as error:
            raise httpcore.ReadTimeout(str(error)) from error
        except OSError as error:
            raise httpcore.ReadError(str(error)) from error

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        try:
            self._sock.settimeout(timeout)
            self._sock.sendall(buffer)
        except socket.timeout as error:
            raise httpcore.WriteTimeout(str(error)) from error
        except OSError as error:
            raise httpcore.WriteError(str(error)) from error

    def close(self) -> None:
        self._sock.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        try:
            self._sock.settimeout(timeout)
            wrapped = ssl_context.wrap_socket(self._sock, server_hostname=server_hostname)
        except socket.timeout as error:
            self.close()
            raise httpcore.ConnectTimeout(str(error)) from error
        except OSError as error:
            self.close()
            raise httpcore.ConnectError(str(error)) from error
        except Exception:
            self.close()
            raise
        return _SocketStream(wrapped)

    def get_extra_info(self, info: str) -> Any:
        if info == "ssl_object" and isinstance(self._sock, ssl.SSLSocket):
            return self._sock._sslobj  # type: ignore[attr-defined]
        if info == "client_addr":
            return self._sock.getsockname()
        if info == "server_addr":
            return self._sock.getpeername()
        if info == "socket":
            return self._sock
        if info == "is_readable":
            if isinstance(self._sock, ssl.SSLSocket) and self._sock.pending():
                return True
            try:
                return bool(select.select([self._sock], [], [], 0)[0])
            except (OSError, TypeError, ValueError):
                return True
        return None


class ValidatingNetworkBackend(httpcore.NetworkBackend):
    """Resolve, validate, and connect to the exact approved socket address."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        resolver: Callable[..., list[tuple]] | None = None,
        socket_factory: Callable[..., socket.socket] | None = None,
    ) -> None:
        self._allow_private = allow_private
        self._resolver = resolver or socket.getaddrinfo
        self._socket_factory = socket_factory or socket.socket

    def _resolve(self, host: str, port: int) -> list[tuple]:
        try:
            infos = self._resolver(
                host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as error:
            raise UnsafeURLError(f"cannot resolve host {host!r}") from error
        if not infos:
            raise UnsafeURLError(f"cannot resolve host {host!r}")

        for _family, _sock_type, _proto, _canonname, sockaddr in infos:
            address_text = str(sockaddr[0]).split("%", 1)[0]
            try:
                address = ipaddress.ip_address(address_text)
            except ValueError as error:
                raise UnsafeURLError(
                    f"host {host!r} resolved to invalid address {sockaddr[0]!r}"
                ) from error
            if not self._allow_private and not _is_public_address(address):
                raise UnsafeURLError(
                    f"host {host!r} resolves to non-public address {address}"
                )
        return infos

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple] | None = None,
    ) -> httpcore.NetworkStream:
        infos = self._resolve(host, port)
        last_error: Exception | None = None
        for family, sock_type, proto, _canonname, sockaddr in infos:
            sock: socket.socket | None = None
            try:
                sock = self._socket_factory(family, sock_type, proto)
                sock.settimeout(timeout)
                if local_address is not None:
                    bind_address = (
                        (local_address, 0, 0, 0)
                        if family == socket.AF_INET6
                        else (local_address, 0)
                    )
                    sock.bind(bind_address)
                for option in socket_options or ():
                    sock.setsockopt(*option)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.connect(sockaddr)
                return _SocketStream(sock)
            except socket.timeout as error:
                last_error = httpcore.ConnectTimeout(str(error))
            except OSError as error:
                last_error = httpcore.ConnectError(str(error))
            if sock is not None:
                sock.close()
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError(f"no usable address for host {host!r}")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[tuple] | None = None,
    ) -> httpcore.NetworkStream:
        raise RuntimeError("UNIX sockets are not supported by the crawl transport")


class SSRFProtectedTransport(httpx.HTTPTransport):
    """Synchronous HTTP transport backed by connect-time address validation."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        network_backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            network_backend=(
                network_backend or ValidatingNetworkBackend(allow_private=allow_private)
            ),
        )


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

    resolve_dns=False limits the address check to IP literals. It is used when
    the pinned crawl transport will resolve and validate the hostname at connect time.
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
        # The classifier excludes multicast explicitly because ipaddress treats
        # multicast as globally scoped even though it is not public unicast.
        if not _is_public_address(addr):
            raise UnsafeURLError(f"{url!r} resolves to non-public address {addr}")


def request_guard(
    *, allow_private: bool = False, require_https: bool = False
) -> Callable[[httpx.Request], None]:
    """httpx request event hook — validates every request, including redirect hops."""

    def _check(request: httpx.Request) -> None:
        validate_url(
            str(request.url),
            allow_private=allow_private,
            require_https=require_https,
            resolve_dns=False,
        )

    return _check
