"""External suggestion pipeline (v3): search per article title -> clean -> trust filter
-> pending suggestions. Only articles with no external suggestions yet are processed,
so re-runs are idempotent and API quota isn't burned twice."""

from urllib.parse import urlparse

from sqlalchemy import exists, select

from app.config import settings
from app.db import SessionLocal
from app.ml.external.brave import BraveProvider
from app.ml.external.cleaning import clean
from app.ml.external.tavily import TavilyProvider
from app.ml.external.trust_score import trust_score
from app.models import Article, Site, Suggestion

PROVIDERS = {"tavily": TavilyProvider, "brave": BraveProvider}


def generate_external_suggestions(site_id: int) -> dict:
    """RQ task body."""
    db = SessionLocal()
    try:
        provider = PROVIDERS[settings.external_provider]()
        # External must stay external: never point at any site of the fleet
        own_hosts = {urlparse(u).netloc for u in db.scalars(select(Site.base_url))}

        pending = db.execute(
            select(Article.id, Article.title).where(
                Article.site_id == site_id,
                ~exists().where(
                    Suggestion.source_article_id == Article.id,
                    Suggestion.method == "external_search",
                ),
            )
        ).all()

        created = 0
        for article_id, title in pending:
            results = clean(provider.search(title, k=10), own_hosts)
            scored = sorted(
                ((trust_score(r), r) for r in results), key=lambda p: p[0], reverse=True
            )
            for ts, r in scored[: settings.max_external_per_article]:
                if ts < settings.external_trust_threshold:
                    continue
                db.add(
                    Suggestion(
                        site_id=site_id,
                        source_article_id=article_id,
                        target_article_id=None,
                        method="external_search",
                        score=r.provider_score,
                        trust_score=ts,
                        external_url=r.url,
                        external_title=r.title,
                        status="pending",
                    )
                )
                created += 1
            db.commit()  # per-article commit — a crash never redoes paid searches
        return {"articles_searched": len(pending), "suggestions_created": created}
    finally:
        db.close()
