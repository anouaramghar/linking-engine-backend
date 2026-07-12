from app.services.suggestion_service import generate_suggestions


def analyze_site(site_id: int) -> dict:
    return generate_suggestions(site_id)
