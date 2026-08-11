"""Per-site safety policy for outgoing external-link suggestions."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.domain_policy import domain_from_url, domain_matches
from app.models import Article, ExternalLinkPolicy, Site, Suggestion, SuggestionEvent


@dataclass(frozen=True)
class PolicyState:
    site_id: int
    external_links_enabled: bool = True
    require_https: bool = True
    min_trust_score: int = 60
    min_domain_age_days: int = 0
    trusted_tlds: tuple[str, ...] = ()
    allowlist_domains: tuple[str, ...] = ()
    blocklist_domains: tuple[str, ...] = ()
    competitor_domains: tuple[str, ...] = ()

    def as_payload(self) -> dict:
        payload = asdict(self)
        for key in (
            "trusted_tlds",
            "allowlist_domains",
            "blocklist_domains",
            "competitor_domains",
        ):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True)
class TrustEvaluation:
    domain: str
    trust_score: int
    eligible: bool
    reasons: tuple[str, ...]
    checks: dict[str, bool | int | str | None]

    def as_score_component(self) -> dict:
        return {
            "domain": self.domain,
            "score": self.trust_score,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "checks": self.checks,
        }


@dataclass(frozen=True)
class WebSearchSafetyEvaluation:
    """Hard URL/domain guards for a dynamic web-search candidate."""

    domain: str
    eligible: bool
    reasons: tuple[str, ...]
    checks: dict[str, bool]

    def as_score_component(self) -> dict:
        return {
            "domain": self.domain,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "checks": self.checks,
        }


def policy_state(db: Session, site_id: int) -> PolicyState:
    policy = db.get(ExternalLinkPolicy, site_id)
    if policy is None:
        return PolicyState(site_id=site_id)
    return PolicyState(
        site_id=site_id,
        external_links_enabled=policy.external_links_enabled,
        require_https=policy.require_https,
        min_trust_score=policy.min_trust_score,
        min_domain_age_days=policy.min_domain_age_days,
        trusted_tlds=tuple(policy.trusted_tlds or ()),
        allowlist_domains=tuple(policy.allowlist_domains or ()),
        blocklist_domains=tuple(policy.blocklist_domains or ()),
        competitor_domains=tuple(policy.competitor_domains or ()),
    )


def managed_domains(db: Session) -> dict[str, int]:
    domains: dict[str, int] = {}
    for site_id, base_url in db.execute(
        select(Site.id, Site.base_url).where(Site.platform != "pool")
    ):
        domains[domain_from_url(base_url)] = site_id
    return domains


def _matches_any(domain: str, rules: tuple[str, ...]) -> bool:
    return any(domain_matches(domain, rule) for rule in rules)


def evaluate_web_search_url(
    *,
    source_site: Site,
    target_url: str,
    policy: PolicyState,
    owned_domains: dict[str, int],
) -> WebSearchSafetyEvaluation:
    """Apply the approved hard guards without pretending Tavily is a pool source."""

    del source_site  # Reserved for future source-relative guards.
    try:
        domain = domain_from_url(target_url)
    except ValueError:
        return WebSearchSafetyEvaluation(
            domain="invalid",
            eligible=False,
            reasons=("target URL has an invalid domain",),
            checks={
                "https": False,
                "blocklisted": False,
                "competitor": False,
                "owned_domain": False,
            },
        )

    https = urlsplit(target_url).scheme.lower() == "https"
    blocklisted = _matches_any(domain, policy.blocklist_domains)
    competitor = _matches_any(domain, policy.competitor_domains)
    owned = any(domain_matches(domain, owned_domain) for owned_domain in owned_domains)
    reasons: list[str] = []
    if not policy.external_links_enabled:
        reasons.append("external links are disabled for this site")
    if not https:
        reasons.append("HTTPS is required for web-search targets")
    if blocklisted:
        reasons.append("domain is blocklisted")
    if competitor:
        reasons.append("domain is marked as a competitor")
    if owned:
        reasons.append("domain belongs to a managed site")

    return WebSearchSafetyEvaluation(
        domain=domain,
        eligible=not reasons,
        reasons=tuple(reasons),
        checks={
            "https": https,
            "blocklisted": blocklisted,
            "competitor": competitor,
            "owned_domain": owned,
        },
    )


def _domain_age_days(target_url: str, target_site: Site) -> int | None:
    registered = target_site.domain_registered_at
    if registered is None:
        return None
    target_domain = domain_from_url(target_url)
    source_domain = domain_from_url(target_site.base_url)
    if not (
        domain_matches(target_domain, source_domain)
        or domain_matches(source_domain, target_domain)
    ):
        return None
    return max(0, (datetime.now(UTC).date() - registered).days)


def evaluate_external_url(
    *,
    source_site: Site,
    target_site: Site,
    target_url: str,
    policy: PolicyState,
    owned_domains: dict[str, int],
) -> TrustEvaluation:
    """Score one external target and apply every hard safety guard."""
    try:
        domain = domain_from_url(target_url)
    except ValueError:
        return TrustEvaluation(
            domain="invalid",
            trust_score=0,
            eligible=False,
            reasons=("target URL has an invalid domain",),
            checks={
                "https": False,
                "trusted_tld": False,
                "domain_age_days": None,
                "allowlisted": False,
                "blocklisted": False,
                "competitor": False,
                "owned_domain": False,
                "approved_source": False,
            },
        )
    scheme = urlsplit(target_url).scheme.lower()
    tld = domain.rsplit(".", 1)[-1]
    age_days = _domain_age_days(target_url, target_site)
    https = scheme == "https"
    approved_source = (
        target_site.platform == "pool"
        and target_site.pool_source_approved
        and not target_site.pool_source_quarantined
    )
    allowlisted = _matches_any(domain, policy.allowlist_domains)
    blocklisted = _matches_any(domain, policy.blocklist_domains)
    competitor = _matches_any(domain, policy.competitor_domains)
    owned_site_id = next(
        (
            site_id
            for owned_domain, site_id in owned_domains.items()
            if domain_matches(domain, owned_domain)
        ),
        None,
    )
    owned = owned_site_id is not None
    trusted_tld = not policy.trusted_tlds or tld in policy.trusted_tlds

    score = 0
    if https:
        score += 30
    if trusted_tld:
        score += 15
    if approved_source:
        score += 15
    if allowlisted:
        score += 20
    elif not policy.allowlist_domains:
        score += 10
    if age_days is not None:
        if age_days >= 365:
            score += 20
        elif age_days >= 180:
            score += 15
        elif age_days >= 30:
            score += 10
        else:
            score += 5
    score = min(100, score)

    reasons: list[str] = []
    if not policy.external_links_enabled:
        reasons.append("external links are disabled for this site")
    if policy.require_https and not https:
        reasons.append("HTTPS is required")
    if not approved_source:
        reasons.append("content-pool source is not approved and active")
    if blocklisted:
        reasons.append("domain is blocklisted")
    if competitor:
        reasons.append("domain is marked as a competitor")
    if owned:
        reasons.append("domain belongs to a managed site")
    if policy.min_domain_age_days and (
        age_days is None or age_days < policy.min_domain_age_days
    ):
        reasons.append(
            "domain age is unknown"
            if age_days is None
            else f"domain age is below {policy.min_domain_age_days} days"
        )
    if score < policy.min_trust_score:
        reasons.append(f"trust score is below {policy.min_trust_score}")

    return TrustEvaluation(
        domain=domain,
        trust_score=score,
        eligible=not reasons,
        reasons=tuple(reasons),
        checks={
            "https": https,
            "trusted_tld": trusted_tld,
            "domain_age_days": age_days,
            "allowlisted": allowlisted,
            "blocklisted": blocklisted,
            "competitor": competitor,
            "owned_domain": owned,
            "approved_source": approved_source,
        },
    )


def external_target_context(
    db: Session, source_site: Site
) -> tuple[set[int], dict[int, TrustEvaluation]]:
    """Return every target the ranker may inspect and trust details by external id."""
    allowed_ids = set(
        db.scalars(
            select(Article.id).where(
                Article.site_id == source_site.id,
                Article.is_active.is_(True),
            )
        )
    )
    evaluations: dict[int, TrustEvaluation] = {}
    policy = policy_state(db, source_site.id)
    owned = managed_domains(db)
    rows = db.execute(
        select(Article, Site)
        .join(Site, Site.id == Article.site_id)
        .where(
            Site.platform == "pool",
            Article.is_active.is_(True),
        )
    ).all()
    for article, target_site in rows:
        evaluation = evaluate_external_url(
            source_site=source_site,
            target_site=target_site,
            target_url=article.url,
            policy=policy,
            owned_domains=owned,
        )
        evaluations[article.id] = evaluation
        if evaluation.eligible:
            allowed_ids.add(article.id)
    return allowed_ids, evaluations


def source_evaluations(db: Session, source_site: Site) -> list[dict]:
    """Summarize each pool source for the policy management UI."""
    policy = policy_state(db, source_site.id)
    owned = managed_domains(db)
    pool_sites = db.scalars(
        select(Site).where(Site.platform == "pool").order_by(Site.name, Site.id)
    ).all()
    article_rows = db.execute(
        select(Article.site_id, Article.url)
        .join(Site, Site.id == Article.site_id)
        .where(Article.is_active.is_(True), Site.platform == "pool")
    ).all()
    urls_by_site: dict[int, list[str]] = {}
    for site_id, url in article_rows:
        urls_by_site.setdefault(site_id, []).append(url)

    items: list[dict] = []
    for target_site in pool_sites:
        base = evaluate_external_url(
            source_site=source_site,
            target_site=target_site,
            target_url=target_site.base_url,
            policy=policy,
            owned_domains=owned,
        )
        article_evaluations = [
            evaluate_external_url(
                source_site=source_site,
                target_site=target_site,
                target_url=url,
                policy=policy,
                owned_domains=owned,
            )
            for url in urls_by_site.get(target_site.id, ())
        ]
        items.append(
            {
                "site_id": target_site.id,
                "site_name": target_site.name,
                "domain": base.domain,
                "trust_score": base.trust_score,
                "eligible": base.eligible,
                "eligible_articles": sum(item.eligible for item in article_evaluations),
                "blocked_articles": sum(not item.eligible for item in article_evaluations),
                "reasons": list(base.reasons),
                "checks": base.checks,
            }
        )
    return items


def expire_ineligible_external_suggestions(
    db: Session,
    source_site: Site,
    *,
    statuses: tuple[str, ...] = ("pending", "approved"),
    actor: str,
) -> int:
    """Expire old rows that no longer satisfy the current external policy."""
    policy = policy_state(db, source_site.id)
    owned = managed_domains(db)
    suggestions = db.scalars(
        select(Suggestion)
        .where(
            Suggestion.site_id == source_site.id,
            Suggestion.status.in_(statuses),
        )
        .options(joinedload(Suggestion.target_article))
    ).all()
    target_site_ids = {
        row.target_article.site_id
        for row in suggestions
        if row.target_article is not None
    }
    target_sites = {
        site.id: site
        for site in db.scalars(select(Site).where(Site.id.in_(target_site_ids))).all()
    }
    expired = 0
    now = datetime.now(UTC)
    for suggestion in suggestions:
        if suggestion.external_url is not None:
            evaluation = evaluate_web_search_url(
                source_site=source_site,
                target_url=suggestion.external_url,
                policy=policy,
                owned_domains=owned,
            )
            details_key = "external_safety"
        else:
            target = suggestion.target_article
            if target is None:
                continue
            target_site = target_sites[target.site_id]
            if target_site.id == source_site.id:
                continue
            evaluation = evaluate_external_url(
                source_site=source_site,
                target_site=target_site,
                target_url=target.url,
                policy=policy,
                owned_domains=owned,
            )
            details_key = "external_trust"
        if evaluation.eligible:
            continue
        previous = suggestion.status
        suggestion.status = "expired"
        db.add(
            SuggestionEvent(
                suggestion_id=suggestion.id,
                event_type="policy_expired",
                actor=actor[:255],
                details={
                    "from_status": previous,
                    "to_status": "expired",
                    details_key: evaluation.as_score_component(),
                },
                created_at=now,
            )
        )
        expired += 1
    return expired
