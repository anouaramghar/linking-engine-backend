"""Fleet-scale operations (v5): one call fans out a job per site — the API stays
instant, RQ workers absorb the load."""

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Site
from app.tasks.analysis import analyze_site
from app.tasks.ingestion import ingest_site
from app.tasks.queues import default_queue

router = APIRouter(prefix="/fleet", tags=["fleet"])


def _fan_out(db: Session, func, *args, timeout: int) -> dict:
    jobs = [
        default_queue.enqueue(func, site_id, *args, job_timeout=timeout).id
        for site_id in db.scalars(select(Site.id).order_by(Site.id))
    ]
    return {"jobs_enqueued": len(jobs), "job_ids": jobs}


@router.post("/ingest", status_code=202)
def ingest_fleet(db: Session = Depends(get_db)) -> dict:
    return _fan_out(db, ingest_site, timeout=3600)


@router.post("/analyze", status_code=202)
def analyze_fleet(
    method: Literal["baseline_cosine", "gnn_graphsage"] = "baseline_cosine",
    db: Session = Depends(get_db),
) -> dict:
    return _fan_out(db, analyze_site, method, timeout=7200)
