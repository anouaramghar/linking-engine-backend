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
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from queue import Empty, Queue
from threading import BoundedSemaphore, Thread
from time import monotonic
from typing import Any, cast
from urllib.parse import urlsplit

import httpcore
import httpx

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
_IPV4_COMPATIBLE_PREFIX = ipaddress.ip_network("::/96")
_IPV4_TRANSLATED_PREFIX = ipaddress.ip_network("::ffff:0:0:0/96")
_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class UnsafeURLError(Exception):
    """URL failed crawl-safety validation."""


class _NetworkDeadlineExceeded(TimeoutError):
    """The caller's wall-clock network budget has been consumed."""


@dataclass(frozen=True, slots=True)
class _NetworkDeadline:
    expires_at: float
    clock: Callable[[], float]

    def remaining(self) -> float:
        remaining = self.expires_at - self.clock()
        if remaining <= 0:
            raise _NetworkDeadlineExceeded("total network deadline exceeded")
        return remaining


_CURRENT_NETWORK_DEADLINE: ContextVar[_NetworkDeadline | None] = ContextVar(
    "linkmesh_network_deadline",
    default=None,
)
_RESOLVER_SLOTS = BoundedSemaphore(4)


@contextmanager
def network_deadline(
    expires_at: float,
    *,
    clock: Callable[[], float] = monotonic,
) -> Iterator[None]:
    """Apply one wall-clock budget across DNS, connects, TLS and socket I/O.

    HTTPX timeouts are inactivity limits for individual operations. A hostile
    peer can otherwise send one small header fragment before every timeout and
    keep a synchronous worker occupied indefinitely. The context-local absolute
    deadline makes every later operation inherit only the time still remaining.
    """

    token = _CURRENT_NETWORK_DEADLINE.set(_NetworkDeadline(expires_at, clock))
    try:
        yield
    finally:
        _CURRENT_NETWORK_DEADLINE.reset(token)


def _bounded_timeout(timeout: float | None) -> float | None:
    deadline = _CURRENT_NETWORK_DEADLINE.get()
    if deadline is None:
        return timeout
    remaining = deadline.remaining()
    return remaining if timeout is None else min(timeout, remaining)


def _bounded_resolve(
    resolver: Callable[..., list[tuple]],
    host: str,
    port: int,
    *,
    timeout: float | None,
) -> list[tuple]:
    """Bound blocking system DNS without allowing unbounded stuck threads."""

    kwargs = {"type": socket.SOCK_STREAM, "proto": socket.IPPROTO_TCP}
    if timeout is None:
        return resolver(host, port, **kwargs)
    if not _RESOLVER_SLOTS.acquire(blocking=False):
        raise _NetworkDeadlineExceeded("DNS resolver capacity is exhausted")

    result: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def resolve() -> None:
        try:
            result.put((True, resolver(host, port, **kwargs)))
        except Exception as error:  # noqa: BLE001 - transfer resolver errors to caller
            result.put((False, error))
        finally:
            _RESOLVER_SLOTS.release()

    Thread(target=resolve, name="linkmesh-live-url-dns", daemon=True).start()
    try:
        succeeded, value = result.get(timeout=timeout)
    except Empty as error:
        raise _NetworkDeadlineExceeded("DNS resolution exceeded total network deadline") from error
    if succeeded:
        return cast(list[tuple], value)
    if isinstance(value, Exception):
        raise value
    raise RuntimeError("DNS resolver returned an invalid result")


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
            self._sock.settimeout(_bounded_timeout(timeout))
            result = self._sock.recv(max_bytes)
            _bounded_timeout(timeout)
            return result
        except _NetworkDeadlineExceeded as error:
            raise httpcore.ReadTimeout(str(error)) from error
        except TimeoutError as error:
            raise httpcore.ReadTimeout(str(error)) from error
        except OSError as error:
            raise httpcore.ReadError(str(error)) from error

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        try:
            self._sock.settimeout(_bounded_timeout(timeout))
            self._sock.sendall(buffer)
            _bounded_timeout(timeout)
        except _NetworkDeadlineExceeded as error:
            raise httpcore.WriteTimeout(str(error)) from error
        except TimeoutError as error:
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
        wrapped: ssl.SSLSocket | None = None
        try:
            self._sock.settimeout(_bounded_timeout(timeout))
            wrapped = ssl_context.wrap_socket(self._sock, server_hostname=server_hostname)
            _bounded_timeout(timeout)
        except _NetworkDeadlineExceeded as error:
            (wrapped or self._sock).close()
            raise httpcore.ConnectTimeout(str(error)) from error
        except TimeoutError as error:
            (wrapped or self._sock).close()
            raise httpcore.ConnectTimeout(str(error)) from error
        except OSError as error:
            (wrapped or self._sock).close()
            raise httpcore.ConnectError(str(error)) from error
        except Exception:
            (wrapped or self._sock).close()
            raise
        if wrapped is None:  # pragma: no cover - ssl.wrap_socket either returns or raises
            raise httpcore.ConnectError("TLS wrapping returned no socket")
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
            infos = _bounded_resolve(
                self._resolver,
                host,
                port,
                timeout=_bounded_timeout(None),
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
                raise UnsafeURLError(f"host {host!r} resolves to non-public address {address}")
        return infos

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple] | None = None,
    ) -> httpcore.NetworkStream:
        try:
            _bounded_timeout(timeout)
            infos = self._resolve(host, port)
            _bounded_timeout(timeout)
        except _NetworkDeadlineExceeded as error:
            raise httpcore.ConnectTimeout(str(error)) from error
        last_error: Exception | None = None
        for family, sock_type, proto, _canonname, sockaddr in infos:
            sock: socket.socket | None = None
            try:
                sock = self._socket_factory(family, sock_type, proto)
                sock.settimeout(_bounded_timeout(timeout))
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
                _bounded_timeout(timeout)
                return _SocketStream(sock)
            except _NetworkDeadlineExceeded as error:
                last_error = httpcore.ConnectTimeout(str(error))
            except TimeoutError as error:
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
