"""Aggregator router for the Autopilot API package (backend_module_decomposition.md §3.2)."""

from fastapi import APIRouter

from src.mcp.autopilot import (
    control_routes,
    feature_routes,
    intervention_routes,
    message_routes,
    project_routes,
    prompt_proposal_routes,
    queue_routes,
)

router = APIRouter(prefix="/api/autopilot", tags=["Autopilot"])

router.include_router(control_routes.router)
router.include_router(queue_routes.router)
router.include_router(project_routes.router)
router.include_router(feature_routes.router)
router.include_router(message_routes.router)
router.include_router(intervention_routes.router)
router.include_router(prompt_proposal_routes.router)
