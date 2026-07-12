from app.services.external_service import generate_external_suggestions


def external_site(site_id: int) -> dict:
    return generate_external_suggestions(site_id)
