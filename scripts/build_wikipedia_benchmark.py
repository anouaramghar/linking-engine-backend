"""Build a Wikipedia benchmark corpus for offline ranking evaluation.

    uv run --no-sync python scripts/build_wikipedia_benchmark.py \
        --seed-url https://en.wikipedia.org/wiki/Sourdough --name "Bench: sourdough"

Creates one content-pool site, ingests it, and reports whether the result can
support a measurement. Then hand the site to the existing harness:

    uv run --no-sync python scripts/run_evaluation.py \
        --cutoff 2015-01-01T00:00:00+00:00 --ground-truth observed --site-id <id>

Why Wikipedia. Ranking quality cannot be measured without knowing which target
was the right one, and LinkMesh's reviewer-label set is far too small to say
(four `reviewed` events as of 2026-08-19). Wikipedia's inter-article links are
link decisions real editors made, in bulk, under a licence that permits reuse,
reachable through the official MediaWiki API. `wikipedia.org` is already the
only host in `pool_allowed_domains`, so no crawl-target rule is relaxed to read
it.

What this measures, and what it does not. An encyclopedia links far more
densely than an editorial site, and it links to define terms rather than to move
a reader onward. A ranker that recovers Wikipedia's links is demonstrably
matching meaning; it is not thereby shown to have editorial judgment. Treat the
result as a floor and a regression guard, never as evidence that a change is
good for a client site.

This script writes: one `Site` row, its articles, their taxonomies, and their
internal links. It touches no existing site.
"""

import argparse
from collections import Counter

from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models import Article, InternalLink, Site
from app.services.ingestion_service import run_ingestion
from app.services.suggestion_service import _embed_missing


def _summarize(db, site_id: int) -> dict:
    articles = db.scalars(select(Article).where(Article.site_id == site_id)).all()
    link_count = db.scalar(
        select(func.count())
        .select_from(InternalLink)
        .join(Article, Article.id == InternalLink.source_article_id)
        .where(Article.site_id == site_id)
    )
    years = sorted(article.published_at.year for article in articles if article.published_at)
    sources_with_links = db.scalars(
        select(func.count(func.distinct(InternalLink.source_article_id)))
        .select_from(InternalLink)
        .join(Article, Article.id == InternalLink.source_article_id)
        .where(Article.site_id == site_id)
    ).all()
    return {
        "articles": len(articles),
        "links": int(link_count or 0),
        "linked_sources": int(sources_with_links[0]) if sources_with_links else 0,
        "years": years,
        "undated": sum(1 for article in articles if article.published_at is None),
    }


def _report(summary: dict) -> None:
    years = summary["years"]
    print(f"  articles           {summary['articles']}")
    print(f"  internal links     {summary['links']}")
    print(f"  linked sources     {summary['linked_sources']}")
    print(f"  undated articles   {summary['undated']}")
    if years:
        print(f"  creation dates     {years[0]}-{years[-1]} ({years[-1] - years[0]} years)")
        print(f"  per-year spread    {dict(sorted(Counter(years).items()))}")

    print()
    if summary["links"] == 0:
        print("  UNUSABLE: no link resolved inside the corpus. Every link this source")
        print("  reports points at an article the source did not also return, so there")
        print("  is nothing to hold out. Use a denser seed, or add more sources on the")
        print("  same topic so their link targets fall inside the corpus.")
    elif len(set(years)) < 2:
        print("  UNUSABLE: every article shares one creation year, so a temporal split")
        print("  cannot separate old targets from new sources.")
    elif summary["links"] < 30:
        print(f"  THIN: {summary['links']} links is enough to smoke-test the harness and")
        print("  too few to compare two rankers. Confidence intervals will swamp any")
        print("  difference. Add sources on the same topic before drawing a conclusion.")
    else:
        print("  Usable. Run scripts/run_evaluation.py against this site id.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-url", required=True, help="a wikipedia.org /wiki/ article URL")
    parser.add_argument("--name", required=True, help="site name, for the dashboard")
    parser.add_argument(
        "--articles",
        type=int,
        default=settings.pool_max_articles_per_source,
        help="articles to request; may exceed the production default of 50",
    )
    parser.add_argument(
        "--reuse-site-id",
        type=int,
        help="re-ingest an existing benchmark site instead of creating one",
    )
    parser.add_argument(
        "--tenant-id",
        type=int,
        default=1,
        help="tenant the benchmark site belongs to (default: 1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be created and write nothing",
    )
    args = parser.parse_args()

    if args.dry_run:
        print(f"Would create pool site {args.name!r} for {args.seed_url}")
        print(f"Would request up to {args.articles} articles, then ingest them.")
        return

    with SessionLocal() as db:
        if args.reuse_site_id:
            site = db.get(Site, args.reuse_site_id)
            if site is None:
                raise SystemExit(f"site {args.reuse_site_id} does not exist")
            if site.platform != "pool":
                raise SystemExit(f"site {site.id} is {site.platform!r}, not a content pool")
        else:
            site = Site(
                tenant_id=args.tenant_id,
                name=args.name,
                base_url=args.seed_url,
                platform="pool",
                pool_source_approved=True,
                # Benchmark corpora are rebuilt on purpose, never on a timer.
                crawl_frequency="manual",
            )
            db.add(site)
            db.commit()
            db.refresh(site)
            print(f"Created pool site id={site.id}")
        site_id = site.id

    ceiling = type(settings).model_fields["pool_max_articles_per_source"].metadata[-1].le
    requested = min(args.articles, ceiling)
    settings.pool_max_articles_per_source = requested
    print(f"Ingesting up to {requested} articles. One request per article, so this is slow.")
    result = run_ingestion(site_id)
    print(f"Ingestion finished: {result}")

    # The evaluation ranker reads embeddings, and ingestion does not create
    # them: they are made during suggestion generation, which never runs for a
    # content pool. Encoding here is what makes the corpus rankable at all.
    print(f"\nEncoding embeddings with {settings.embedding_model}.")
    with SessionLocal() as db:
        encoded = _embed_missing(db, site_id, settings.embedding_model)
        db.commit()
    print(f"Encoded {encoded} articles.")

    print(f"\nBenchmark corpus, site {site_id}:")
    with SessionLocal() as db:
        _report(_summarize(db, site_id))

    print("\nNext:")
    print(
        "  uv run --no-sync python scripts/run_evaluation.py"
        f" --cutoff 2015-01-01T00:00:00+00:00 --ground-truth observed --site-id {site_id}"
    )


if __name__ == "__main__":
    main()
