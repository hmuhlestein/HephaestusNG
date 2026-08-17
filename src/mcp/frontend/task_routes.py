"""Routes extracted from src/mcp/api.py (phase_1b_decomposition.md §4.1).

Each route is a top-level function that delegates to _shared.frontend_api.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from src.mcp.frontend import _shared

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/tasks")
async def get_tasks(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=10000),
    status: Optional[str] = None,
    workflow_id: Optional[str] = None,
    project_id: Optional[str] = None,
):
    """Get tasks with pagination."""
    return await _shared.frontend_api.get_tasks(skip, limit, status, workflow_id, project_id)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """Get a single task by ID."""
    return await _shared.frontend_api.get_task(task_id)


@router.get("/tasks/{task_id}/full-details")
async def get_task_full_details(task_id: str):
    """Get comprehensive task details including prompts and relationships."""
    return await _shared.frontend_api.get_task_full_details(task_id)


@router.get("/blocked-tasks")
async def get_blocked_tasks(project_id: Optional[str] = None):
    """Get all blocked tasks with blocker information."""
    return await _shared.frontend_api.get_blocked_tasks(project_id)


@router.get("/blocked-tasks/{task_id}/blockers")
async def get_task_blocker_details(task_id: str):
    """Get detailed blocker information for a specific task."""
    return await _shared.frontend_api.get_task_blocker_details(task_id)


@router.post("/sync-blocking-status")
async def sync_blocking_status():
    """Manually trigger sync of task blocking status."""
    return await _shared.frontend_api.sync_blocking_status()


