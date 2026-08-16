"""Collect, validate, and promote one coherent crawl snapshot.

Connectors discover and fetch content. This module owns the meaning of one
ingestion run after content has crossed that adapter seam: comparison identity,
forward-link resolution, diagnostic evidence, completeness checks, and the
success-gated promotion of staged rows.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlparse

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors.base import ArticleData, DiscoveryObservation, OutboundLink
from app.models import (
    Article,
    ArticleTaxonomy,
    IngestionDiagnostic,
    IngestionRun,
    InternalLink,
    Suggestion,
)


def normalize_url(url: str) -> str:
    """Return the conservative comparison key used inside one snapshot.

    The stored URL remains untouched. The key only makes scheme, query,
    fragment, and trailing-slash variants comparable while resolving links.
    """
    parsed = urlparse(url)
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def _validate_snapshot_completeness(
    db: Session, site_id: int, run_id: int, article_count: int
) -> None:
    if article_count == 0:
        raise ValueError(
            f"crawl returned zero articles for site {site_id}; snapshot not reconciled"
        )

    previous_count = db.scalar(
        select(IngestionRun.articles_upserted)
        .where(
            IngestionRun.site_id == site_id,
            IngestionRun.status == "succeeded",
            IngestionRun.id != run_id,
        )
        .order_by(IngestionRun.id.desc())
        .limit(1)
    )
    minimum_ratio = settings.ingestion_min_previous_ratio
    if previous_count and article_count < previous_count * minimum_ratio:
        raise ValueError(
            f"crawl returned {article_count} articles for site {site_id}, below "
            f"{minimum_ratio:.0%} of the previous successful crawl ({previous_count}); "
            "snapshot not reconciled"
        )


def _persist_diagnostics(
    db: Session,
    run: IngestionRun,
    observations: Sequence[DiscoveryObservation],
) -> None:
    """Attach discovery evidence to the run before its transaction is committed."""
    summary = Counter(observation.reason_code for observation in observations)
    unique_urls = {normalize_url(observation.url) for observation in observations}
    accepted_urls = {
        normalize_url(observation.url)
        for observation in observations
        if observation.state == "accepted"
    }
    skipped_urls = {
        normalize_url(observation.url)
        for observation in observations
        if observation.state in ("skipped", "failed")
    }
    run.discovered_urls = len(unique_urls)
    run.accepted_urls = len(accepted_urls)
    run.skipped_urls = len(skipped_urls)
    run.diagnostic_summary = dict(sorted(summary.items()))
    db.add_all(
        IngestionDiagnostic(
            site_id=run.site_id,
            ingestion_run_id=run.id,
            url=observation.url,
            state=observation.state,
            reason_code=observation.reason_code,
            reason_detail=(observation.reason_detail or "")[:2000] or None,
            discovered_from=observation.discovered_from,
            depth=observation.depth,
            http_status=observation.http_status,
            content_type=observation.content_type,
            final_url=observation.final_url,
            canonical_url=observation.canonical_url,
        )
        for observation in observations
    )


def _complete_accepted_observations(
    connector, accepted: Sequence[DiscoveryObservation]
) -> list[DiscoveryObservation]:
    observations = connector.drain_discovery_observations()
    accepted_keys = {
        (normalize_url(observation.url), observation.state)
        for observation in observations
    }
    for observation in accepted:
        key = (normalize_url(observation.url), observation.state)
        if key not in accepted_keys:
            observations.append(observation)
            accepted_keys.add(key)
    return observations


def _upsert_link(
    db: Session,
    source_id: int,
    target_id: int,
    run_id: int,
    anchor_text: str | None = None,
) -> None:
    db.execute(
        pg_insert(InternalLink)
        .values(
            source_article_id=source_id,
            target_article_id=target_id,
            anchor_text=anchor_text,
            is_active=True,
            last_seen_run_id=run_id,
        )
        .on_conflict_do_update(
            index_elements=["source_article_id", "target_article_id"],
            set_={
                "anchor_text": anchor_text,
                "is_active": True,
                "last_seen_at": func.now(),
                "last_seen_run_id": run_id,
            },
        )
    )


def _reconcile_snapshot(db: Session, site_id: int, run_id: int) -> None:
    """Promote rows seen by a successful run and retire stale relationships."""
    site_article_ids = select(Article.id).where(Article.site_id == site_id)
    inactive_article_ids = select(Article.id).where(
        Article.site_id == site_id,
        Article.last_seen_run_id.is_distinct_from(run_id),
    )
    taxonomy_reporter_ids = select(ArticleTaxonomy.article_id).where(
        ArticleTaxonomy.last_seen_run_id == run_id
    )
    db.execute(
        update(Article)
        .where(
            Article.site_id == site_id,
            Article.last_seen_run_id == run_id,
            Article.is_active.is_(False),
        )
        .values(is_active=True)
    )
    db.execute(
        update(Article)
        .where(
            Article.site_id == site_id,
            Article.last_seen_run_id.is_distinct_from(run_id),
            Article.is_active.is_(True),
        )
        .values(is_active=False)
    )
    db.execute(
        update(Suggestion)
        .where(
            Suggestion.status.in_(("pending", "approved")),
            or_(
                Suggestion.source_article_id.in_(inactive_article_ids),
                Suggestion.target_article_id.in_(inactive_article_ids),
            ),
        )
        .values(status="expired")
    )
    db.execute(
        update(InternalLink)
        .where(
            InternalLink.source_article_id.in_(site_article_ids),
            InternalLink.last_seen_run_id.is_distinct_from(run_id),
            InternalLink.is_active.is_(True),
        )
        .values(is_active=False)
    )
    db.execute(
        delete(ArticleTaxonomy).where(
            ArticleTaxonomy.article_id.in_(site_article_ids),
            ArticleTaxonomy.last_seen_run_id.is_distinct_from(run_id),
            or_(
                ArticleTaxonomy.article_id.in_(inactive_article_ids),
                ArticleTaxonomy.article_id.in_(taxonomy_reporter_ids),
            ),
        )
    )


@dataclass
class CrawlSnapshot:
    """The staged facts and promotion rules for one ingestion run.

    Article rows can be written inside the caller's transaction before this
    object promotes anything. If the crawl fails or is incomplete, the caller
    rolls back and this snapshot never becomes the active site view.
    """

    site_id: int
    run_id: int
    _article_ids: set[int] = field(default_factory=set)
    _url_to_id: dict[str, int] = field(default_factory=dict)
    _outbound: list[tuple[int, list[OutboundLink]]] = field(default_factory=list)
    _accepted_observations: list[DiscoveryObservation] = field(default_factory=list)

    @property
    def article_count(self) -> int:
        return len(self._article_ids)

    @property
    def accepted_observations(self) -> tuple[DiscoveryObservation, ...]:
        return tuple(self._accepted_observations)

    def stage_article(self, article: ArticleData, article_id: int) -> None:
        """Add one persisted article to this run's staged snapshot."""
        self._article_ids.add(article_id)
        self._url_to_id[normalize_url(article.url)] = article_id
        self._outbound.append((article_id, list(article.outbound_internal_links)))
        self._accepted_observations.append(
            DiscoveryObservation(
                url=article.url,
                state="accepted",
                reason_code="accepted",
                discovered_from=article.discovered_from,
                depth=article.discovery_depth,
                canonical_url=article.canonical_url,
            )
        )

    def validate_completeness(self, db: Session) -> None:
        """Refuse to replace the active view with an empty or truncated crawl."""
        _validate_snapshot_completeness(db, self.site_id, self.run_id, self.article_count)

    def resolve_links(
        self,
        db: Session,
        *,
        resolve_internal_url: Callable[[str], str] | None = None,
        check_deadline: Callable[[], None] | None = None,
    ) -> int:
        """Resolve forward references after all staged article identities exist."""
        check = check_deadline or (lambda: None)
        links = 0
        seen: set[tuple[int, int]] = set()
        for source_id, outbound_links in self._outbound:
            check()
            for link in outbound_links:
                check()
                target_id = self._url_to_id.get(normalize_url(link.url))
                if target_id is None and resolve_internal_url is not None:
                    resolved_url = resolve_internal_url(link.url)
                    if resolved_url:
                        target_id = self._url_to_id.get(normalize_url(resolved_url))
                if target_id and target_id != source_id and (source_id, target_id) not in seen:
                    _upsert_link(db, source_id, target_id, self.run_id, link.anchor_text)
                    seen.add((source_id, target_id))
                    links += 1
        return links

    def promote(self, db: Session) -> None:
        """Make this complete snapshot the active site view."""
        _reconcile_snapshot(db, self.site_id, self.run_id)

    def persist_diagnostics(self, db: Session, run: IngestionRun, connector) -> None:
        """Persist connector evidence plus accepted article observations."""
        observations = _complete_accepted_observations(
            connector, self._accepted_observations
        )
        _persist_diagnostics(db, run, observations)


__all__ = [
    "CrawlSnapshot",
    "_complete_accepted_observations",
    "_persist_diagnostics",
    "_reconcile_snapshot",
    "_upsert_link",
    "_validate_snapshot_completeness",
    "normalize_url",
]
