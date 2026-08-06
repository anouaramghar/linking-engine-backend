"""Approval and domain policy for external content-pool sources."""

from typing import Literal
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models.article import Article
from app.models.site import Site
from app.models.suggestion import Suggestion


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


def _same_property(left: str, right: str) -> bool:
    # ponytail: host equality plus parent/child, no public-suffix list. A client
    # at blog.example.com and a pool source at example.com are one property and
    # must collide; `co.uk` would over-block, which is the safe direction for
    # this rule. Add tldextract only if a real allowlist entry gets refused.
    return left == right or left.endswith(f".{right}") or right.endswith(f".{left}")


def require_no_pbn_conflict(db: Session, base_url: str, *, as_pool: bool) -> None:
    """A domain we manage for a client may never also be a link target (PBN rule).

    Google punishes a network of sites we control linking to each other, so the
    check runs in both directions: approving a pool source rejects a domain any
    client site holds, and creating a client site rejects a domain an approved
    pool source holds. One direction alone would leave the bad state reachable
    by doing the two steps in the other order.
    """
    domain = pool_source_domain(base_url)
    query = (
        select(Site.base_url).where(Site.platform != "pool")
        if as_pool
        else select(Site.base_url).where(
            Site.platform == "pool", Site.pool_source_approved.is_(True)
        )
    )
    for other_url in db.scalars(query):
        try:
            other_domain = pool_source_domain(other_url)
        except PoolSourcePolicyError:
            continue  # a malformed stored URL is not evidence of a conflict
        if _same_property(domain, other_domain):
            held_by = "a client site" if as_pool else "an approved content-pool source"
            raise PoolSourcePolicyError(
                f"domain {domain!r} is already {held_by} ({other_domain!r}); "
                "linking it from other clients would form a private blog network"
            )


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


def expire_pool_target_suggestions(
    db: Session, site_id: int, *, reason: Literal["revoked", "quarantined"]
) -> int:
    """Remove a disabled pool source from the editorial queues it still occupies.

    Revocation clears approved suggestions as well as pending ones. An operator
    has decided this source may not be linked to, and an approval that outlived
    that decision would publish exactly the link they just disallowed.

    Quarantine clears only pending ones. It is automatic and reversible — a
    threshold of consecutive fetch failures, undone by the reactivate endpoint —
    and an approved suggestion is finished editorial work. A feed that timed out
    three times says nothing about the target article, so discarding a decision
    an editor already made would cost more than it protects.
    """
    statuses = ("pending", "approved") if reason == "revoked" else ("pending",)
    target_ids = select(Article.id).where(Article.site_id == site_id)
    result = db.execute(
        update(Suggestion)
        .where(
            Suggestion.target_article_id.in_(target_ids),
            Suggestion.status.in_(statuses),
        )
        .values(status="expired")
    )
    return result.rowcount or 0
