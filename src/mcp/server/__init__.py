"""MCP server package -- FastAPI app assembly (design_docs/phase_1c_server_decomposition.md)."""

from src.mcp.server import (
    agent_task_routes,
    mcp_protocol,
    oauth_routes,
    task_admin_routes,
    workflow_execution_routes,
)
from src.mcp.server import (
    lifecycle as lifecycle,  # noqa: F401 -- import needed for its @app.on_event side effects
)
from src.mcp.server._shared import app  # noqa: F401 -- re-exported for uvicorn/tests

app.include_router(agent_task_routes.router)
app.include_router(task_admin_routes.router)
app.include_router(oauth_routes.router)
app.include_router(workflow_execution_routes.router)
app.include_router(mcp_protocol.router)

