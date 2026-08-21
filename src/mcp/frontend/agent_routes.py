"""Routes extracted from src/mcp/api.py (phase_1b_decomposition.md §4.1).

Each route is a top-level function that delegates to _shared.agent_service.
"""

import logging

from fastapi import APIRouter

from src.mcp.frontend import _shared

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/phases/{phase_id}/agents")
async def get_phase_agents(phase_id: str):
    """List agents working in this phase."""
    return await _shared.agent_service.get_phase_agents(phase_id)


