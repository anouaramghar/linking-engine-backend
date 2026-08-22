from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.api.deps import require_api_key
from app.api.routes import (
    admin_keys,
    agent,
    alerts,
    auth,
    evaluation,
    graph,
    health,
    ingestion,
    jobs,
    pipelines,
    publish,
    sites,
    suggestions,
)
from app.mcp_server import authenticated_mcp_app, mcp_lifespan


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Starlette does not run mounted apps' lifespans; the MCP streamable-HTTP
    # session manager needs its own, so it is composed into the API's.
    async with mcp_lifespan()(_):
        yield


app = FastAPI(title="LinkMesh Engine", version="0.1.0", lifespan=lifespan)

# Read-only agent tool surface (streamable HTTP at /mcp/). Authenticated by the
# same X-API-Key scheme as every protected route — see app/mcp_server.py.
app.mount("/mcp", authenticated_mcp_app)

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
    evaluation.router,
    graph.router,
    admin_keys.router,
    agent.router,
]:
    app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_api_key)])
