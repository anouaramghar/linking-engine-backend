from app.services.ingestion_service import run_ingestion
from app.services.job_service import run_durably


def ingest_site(site_id: int, job_run_id: int | None = None) -> dict:
    return run_durably(job_run_id, run_ingestion, site_id)
