from app.services.job_service import run_durably
from app.services.suggestion_service import generate_suggestions


def analyze_site(site_id: int, job_run_id: int | None = None) -> dict:
    return run_durably(job_run_id, generate_suggestions, site_id)
