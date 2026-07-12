from app.services.suggestion_service import generate_suggestions


def analyze_site(site_id: int, method: str = "baseline_cosine") -> dict:
    if method == "gnn_graphsage":
        from app.ml.gnn.predict import generate_gnn_suggestions  # heavy import

        return generate_gnn_suggestions(site_id)
    return generate_suggestions(site_id)
