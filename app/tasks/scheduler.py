"""Automatic re-crawls (closes A6): rq-scheduler cron jobs enqueue ingestion for every
site whose crawl_frequency matches. Register once, then run the rqscheduler process:

    uv run python -m app.tasks.scheduler   # registers the cron entries
    uv run rqscheduler --url $REDIS_URL    # executes them
"""

from sqlalchemy import select

from app.db import SessionLocal
from app.tasks.ingestion import ingest_site
from app.tasks.queues import default_queue, redis_conn

CRONS = {"daily": "0 3 * * *", "weekly": "0 4 * * 0"}  # off-peak; 'manual' is never scheduled


def ingest_due(frequency: str) -> dict:
    """Cron target: fan out one ingestion job per site with this frequency."""
    from app.models import Site

    db = SessionLocal()
    try:
        site_ids = db.scalars(select(Site.id).where(Site.crawl_frequency == frequency)).all()
    finally:
        db.close()
    for site_id in site_ids:
        default_queue.enqueue(ingest_site, site_id, job_timeout=3600)
    return {"frequency": frequency, "sites_enqueued": len(site_ids)}


def register() -> None:
    from rq_scheduler import Scheduler

    scheduler = Scheduler(queue=default_queue, connection=redis_conn)
    for job in scheduler.get_jobs():  # idempotent re-register
        if job.id.startswith("linkmesh-recrawl-"):
            scheduler.cancel(job)
    for frequency, cron in CRONS.items():
        scheduler.cron(
            cron,
            func=ingest_due,
            args=[frequency],
            id=f"linkmesh-recrawl-{frequency}",
            timeout=600,
        )


if __name__ == "__main__":
    register()
    print(f"registered re-crawl crons: {CRONS}")
