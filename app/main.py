from fastapi import FastAPI

from app.api.routes import fleet, health, ingestion, jobs, publish, sites, stats, suggestions

app = FastAPI(title="LinkMesh Engine", version="0.1.0")

for router in [health.router, sites.router, ingestion.router, suggestions.router,
               publish.router, jobs.router, stats.router, fleet.router]:
    app.include_router(router, prefix="/api/v1")
