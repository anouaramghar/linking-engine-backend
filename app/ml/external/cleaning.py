"""Canonical URL identity for content-pool and external-search candidates.

Only transformations that are safe for a stored link are applied here. The
normalizer keeps content-bearing path and query values, while removing URL
parts that identify a visit rather than a resource (fragments and known
tracking parameters). Every external ingestion path must use this function so
database uniqueness compares the same representation.
"""

from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_KEYS = frozenset(
    {
        "dclid",
        "fbclid",
        "gclid",
        "gbraid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "ref",
        "ref_src",
        "wbraid",
    }
)


def _canonical_host(hostname: str) -> str:
    """Return a lowercase ASCII host, preserving IPv6 URL syntax."""
    host = hostname.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("external URL contains an invalid hostname") from error
    return f"[{host}]" if ":" in host else host


def normalize_external_url(value: str) -> str:
    """Return the canonical stored representation of an external HTTP(S) URL.

    The result has a lowercase scheme and host, no default port, no fragment,
    no known tracking parameters, and a deterministic query order. It does not
    collapse HTTP into HTTPS, remove ``www``, change path case, or remove a
    trailing slash: those transformations can identify a different resource.
    """
    candidate = value.strip()
    if not candidate or any(character.isspace() for character in candidate):
        raise ValueError("external URL must not be empty or contain whitespace")

    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("external URL must use HTTP(S) and include a hostname")
    if parts.username or parts.password:
        raise ValueError("external URL must not contain credentials")

    host = _canonical_host(parts.hostname)
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("external URL contains an invalid port") from error
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"

    clean_query = sorted(
        (
            (key, item_value)
            for key, item_value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_QUERY_KEYS
            and not key.lower().startswith(_TRACKING_QUERY_PREFIXES)
        ),
        key=lambda item: (item[0], item[1]),
    )
    return urlunsplit(
        (
            scheme,
            host,
            parts.path or "/",
            urlencode(clean_query, doseq=True),
            "",
        )
    )


def deduplicate_external_urls(values: Iterable[str]) -> list[str]:
    """Normalize URLs and retain the first occurrence of each resource."""
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_external_url(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique
