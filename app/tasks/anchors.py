from app.services.anchor_service import generate_anchors


def anchor_site(site_id: int) -> dict:
    return generate_anchors(site_id)
