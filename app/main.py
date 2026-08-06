from fastapi import Depends, FastAPI

from app.api.deps import require_api_key
from app.api.routes import (
    admin_keys,
    alerts,
    auth,
    health,
    ingestion,
    jobs,
    pipelines,
    publish,
    sites,
    suggestions,
)

app = FastAPI(title="LinkMesh Engine", version="0.1.0")

app.include_router(health.router, prefix="/api/v1")  # open — docker healthcheck probes it
# Open at the API-key layer on purpose: login has to work before the caller
# holds anything, and these routes gate themselves on a dashboard session.
app.include_router(auth.router, prefix="/api/v1")
# Add every new protected router inside this loop; routers registered elsewhere are unauthenticated.
for router in [
    sites.router,
    ingestion.router,
    suggestions.router,
    publish.router,
    jobs.router,
    alerts.router,
    pipelines.router,
    admin_keys.router,
]:
    app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_api_key)])
