"""Shared module-level service instances for the frontend package.

Extracted from src/mcp/api.py (phase_1b_decomposition.md §4.1). FrontendAPI
itself was later split into per-domain services (SOLID review 1.7):
dashboard_service.DashboardService, task_service.TaskService,
phase_service.PhaseService, agent_service.AgentService -- one per existing
router file, since the routers were already split by domain but the class
underneath them wasn't.
"""

from src.mcp.frontend.agent_service import AgentService
from src.mcp.frontend.dashboard_service import DashboardService
from src.mcp.frontend.phase_service import PhaseService
from src.mcp.frontend.task_service import TaskService

# Set in create_frontend_routes().
dashboard_service = None
task_service = None
phase_service = None
agent_service = None
