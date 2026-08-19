"""Routes extracted from src/mcp/api.py (phase_1b_decomposition.md §4.1).

Each route is a top-level function that delegates to _shared.frontend_api.
"""

import logging

from fastapi import APIRouter

from src.mcp.frontend import _shared

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/workflows/{workflow_id}/stop")
async def stop_workflow(workflow_id: str):
    """Stop a running workflow and terminate its agents."""
    return await _shared.frontend_api.stop_workflow(workflow_id)


@router.get("/phases/{phase_id}/agents")
async def get_phase_agents(phase_id: str):
    """List agents working in this phase."""
    return await _shared.frontend_api.get_phase_agents(phase_id)


