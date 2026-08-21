"""Routes extracted from src/mcp/api.py (phase_1b_decomposition.md §4.1).

Each route is a top-level function that delegates to _shared.dashboard_service.
"""

import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from src.mcp.frontend import _shared

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/dashboard/stats")
async def get_dashboard_stats(project_id: Optional[str] = None):
    """Get dashboard statistics."""
    return await _shared.dashboard_service.get_dashboard_stats(project_id)


@router.get("/memories")
async def get_memories(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=10000),
    memory_type: Optional[str] = None,
    search: Optional[str] = None,
):
    """Get memories with pagination and search."""
    return await _shared.dashboard_service.get_memories(skip, limit, memory_type, search)


@router.get("/graph")
async def get_graph_data(workflow_id: Optional[str] = None):
    """Get graph visualization data."""
    return await _shared.dashboard_service.get_graph_data(workflow_id=workflow_id)


@router.get("/workflow")
async def get_workflow():
    """Get current workflow information."""
    return await _shared.dashboard_service.get_workflow_info()


@router.get("/phases")
async def get_phases(workflow_id: Optional[str] = None):
    """Get all phases with metrics."""
    return await _shared.dashboard_service.get_phases(workflow_id)


@router.get("/workflow-definitions/{definition_id}/phases")
async def get_definition_phases(definition_id: str):
    """Get phase definitions from a workflow definition."""
    import json as json_mod

    from src.core.database import WorkflowDefinition

    session = _shared.dashboard_service.db_manager.get_session()
    try:
        wf_def = (
            session.query(WorkflowDefinition).filter_by(id=definition_id).first()
        )
        if not wf_def:
            raise HTTPException(
                status_code=404, detail="Workflow definition not found"
            )
        phases = wf_def.phases_config
        # Handle double-encoded JSON strings
        if isinstance(phases, str):
            try:
                phases = json_mod.loads(phases)
            except (json_mod.JSONDecodeError, TypeError):
                phases = []
        if not isinstance(phases, list):
            phases = []
        return {"phases": phases}
    finally:
        session.close()


@router.get("/guardian-analyses/{agent_id}")
async def get_guardian_analyses(
    agent_id: str, limit: int = Query(50, ge=1, le=200)
):
    """Get guardian analyses for a specific agent."""
    return await _shared.dashboard_service.get_guardian_analyses(agent_id, limit)


@router.get("/conductor-analyses")
async def get_conductor_analyses(limit: int = Query(20, ge=1, le=100)):
    """Get conductor analyses for system overview."""
    return await _shared.dashboard_service.get_conductor_analyses(limit)


@router.get("/conductor-analyses/latest")
async def get_latest_conductor_analysis():
    """Get the most recent conductor analysis."""
    return await _shared.dashboard_service.get_latest_conductor_analysis()


@router.get("/steering-interventions")
async def get_steering_interventions(
    agent_id: Optional[str] = None, limit: int = Query(50, ge=1, le=200)
):
    """Get steering interventions, optionally filtered by agent."""
    return await _shared.dashboard_service.get_steering_interventions(agent_id, limit)


@router.get("/system-overview")
async def get_system_overview(workflow_id: Optional[str] = None):
    """Get comprehensive system overview data."""
    return await _shared.dashboard_service.get_system_overview(workflow_id)


@router.get("/results")
async def get_results(
    scope: str = Query("all", regex="^(all|workflow|task)$"),
    status: Optional[str] = Query(None),
    workflow_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """Get aggregated results for workflows and tasks."""
    return await _shared.dashboard_service.get_results(
        scope=scope,
        status=status,
        workflow_id=workflow_id,
        agent_id=agent_id,
        search=search,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/results/{result_id}/content")
async def get_result_content(result_id: str):
    """Get markdown content for a specific result."""
    return await _shared.dashboard_service.get_result_content(result_id)


@router.get("/results/{result_id}/validation")
async def get_result_validation(result_id: str):
    """Get validation details for a specific result."""
    return await _shared.dashboard_service.get_result_validation(result_id)


@router.get("/results/{result_id}/extra-files/{file_index}")
async def get_extra_file_content(result_id: str, file_index: int):
    """Get content of a specific extra file for a result."""
    return await _shared.dashboard_service.get_extra_file_content(result_id, file_index)


@router.get("/results/{result_id}/download")
async def download_result_markdown(result_id: str):
    """Download the markdown file for a specific result."""
    file_path = await _shared.dashboard_service.download_result_markdown(result_id)
    filename = os.path.basename(file_path)
    return FileResponse(
        path=file_path, media_type="text/markdown", filename=filename
    )


@router.get("/results/{result_id}/validation/download")
async def download_validation_report(result_id: str):
    """Download the validation report markdown file for a specific result."""
    file_path = await _shared.dashboard_service.download_validation_report(result_id)
    filename = os.path.basename(file_path)
    return FileResponse(
        path=file_path, media_type="text/markdown", filename=filename
    )


