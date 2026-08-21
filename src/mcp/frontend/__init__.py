"""Aggregator router for the Frontend API package (phase_1b_decomposition.md §4.1)."""

from fastapi import APIRouter

from src.mcp.frontend import _shared
from src.mcp.frontend.agent_routes import router as agent_router
from src.mcp.frontend.dashboard_routes import router as dashboard_router
from src.mcp.frontend.phase_routes import router as phase_router
from src.mcp.frontend.task_routes import router as task_router

router = APIRouter(prefix="/api", tags=["Frontend API"])

router.include_router(agent_router)
router.include_router(task_router)
router.include_router(phase_router)
router.include_router(dashboard_router)


def create_frontend_routes(db_manager, agent_manager, phase_manager=None):
    """Configure the shared per-domain services and return the aggregate router."""
    _shared.dashboard_service = _shared.DashboardService(db_manager, agent_manager, phase_manager)
    _shared.task_service = _shared.TaskService(db_manager, agent_manager, phase_manager)
    _shared.phase_service = _shared.PhaseService(db_manager, agent_manager, phase_manager)
    _shared.agent_service = _shared.AgentService(db_manager, agent_manager, phase_manager)
    return router
