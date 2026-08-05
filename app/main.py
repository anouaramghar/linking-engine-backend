from fastapi import Depends, FastAPI

from app.api.deps import require_api_key
from app.api.routes import alerts, health, ingestion, jobs, pipelines, publish, sites, suggestions

app = FastAPI(title="LinkMesh Engine", version="0.1.0")

app.include_router(health.router, prefix="/api/v1")  # open — docker healthcheck probes it
# Add every new protected router inside this loop; routers registered elsewhere are unauthenticated.
for router in [
    sites.router,
    ingestion.router,
    suggestions.router,
    publish.router,
    jobs.router,
    alerts.router,
    pipelines.router,
]:
    app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_api_key)])
