from app.services.job_service import run_durably
from app.services.suggestion_service import generate_suggestions


def analyze_site(site_id: int, job_run_id: int | None = None) -> dict:
    return run_durably(job_run_id, generate_suggestions, site_id)


def _compare_suggestions(site_id: int, job_run_id: int | None = None) -> dict:
    return generate_suggestions(
        site_id,
        job_run_id,
        ranking_mode_override="shadow",
        comparison_only=True,
    )


def compare_site(site_id: int, job_run_id: int | None = None) -> dict:
    return run_durably(job_run_id, _compare_suggestions, site_id)
