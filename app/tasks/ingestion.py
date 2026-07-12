from app.services.ingestion_service import run_ingestion


def ingest_site(site_id: int) -> dict:
    return run_ingestion(site_id)
