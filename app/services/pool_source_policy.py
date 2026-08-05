"""Approval and domain policy for external content-pool sources."""

from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.models.site import Site


class PoolSourcePolicyError(ValueError):
    """A content-pool source is not authorized for network ingestion."""


class PoolSourceQuarantinedError(PoolSourcePolicyError):
    """A content-pool source is suspended after repeated failures."""


class PoolSourceFetchError(RuntimeError):
    """A content-pool source failed while fetching or parsing remote content."""


def _normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".").removeprefix("*.")
    if not domain or "/" in domain or "://" in domain:
        raise PoolSourcePolicyError(f"invalid pool allowlist domain: {value!r}")
    try:
        return domain.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise PoolSourcePolicyError(f"invalid pool allowlist domain: {value!r}") from error


def configured_pool_domains() -> tuple[str, ...]:
    return tuple(
        _normalize_domain(value)
        for value in settings.pool_allowed_domains.split(",")
        if value.strip()
    )


def pool_source_domain(url: str) -> str:
    host = urlsplit(url).hostname
    if not host:
        raise PoolSourcePolicyError(f"pool source URL has no domain: {url!r}")
    return _normalize_domain(host)


def is_pool_domain_allowed(url: str) -> bool:
    host = pool_source_domain(url)
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in configured_pool_domains()
    )


def require_allowed_pool_domain(url: str) -> None:
    if not is_pool_domain_allowed(url):
        raise PoolSourcePolicyError(
            f"pool source domain {pool_source_domain(url)!r} is not in POOL_ALLOWED_DOMAINS"
        )


def pool_request_guard(request: httpx.Request) -> None:
    require_allowed_pool_domain(str(request.url))


def require_approved_pool_source(site: Site) -> None:
    if site.platform != "pool":
        raise PoolSourcePolicyError(f"site {site.id} is not a content-pool source")
    if not site.pool_source_approved:
        raise PoolSourcePolicyError(f"pool source site {site.id} has not been approved")
    if site.pool_source_quarantined:
        raise PoolSourceQuarantinedError(
            f"pool source site {site.id} is quarantined: "
            f"{site.pool_source_quarantine_reason or 'repeated ingestion failures'}"
        )
    require_allowed_pool_domain(site.base_url)
