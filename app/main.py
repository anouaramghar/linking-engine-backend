from fastapi import Depends, FastAPI

from app.api.deps import require_api_key
from app.api.routes import health, ingestion, jobs, publish, sites, suggestions

app = FastAPI(title="LinkMesh Engine", version="0.1.0")

app.include_router(health.router, prefix="/api/v1")  # open — docker healthcheck probes it
for router in [sites.router, ingestion.router, suggestions.router,
               publish.router, jobs.router]:
    app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_api_key)])
