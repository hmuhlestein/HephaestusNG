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

@router.get("/phases/{phase_id}/yaml")
async def get_phase_yaml(phase_id: str):
    """Get detailed phase configuration."""
    return await _shared.frontend_api.get_phase_details(phase_id)


@router.patch("/phases/{phase_id}")
async def update_phase(phase_id: str, updates: Dict[str, Any]):
    """Partial update of phase definition fields."""
    return await _shared.frontend_api.update_phase(phase_id, updates)


@router.post("/phases/{phase_id}/reset")
async def reset_phase(phase_id: str, body: Dict[str, Any]):
    """Reset phase execution status."""
    target_status = body.get("target_status")
    force = body.get("force", False)
    if not target_status:
        raise HTTPException(status_code=400, detail="target_status is required")
    return await _shared.frontend_api.reset_phase(phase_id, target_status, force)


@router.get("/phases/{phase_id}/prompt/versions")
async def get_phase_prompt_versions(phase_id: str):
    """List prompt versions for a phase."""
    return await _shared.frontend_api.get_phase_prompt_versions(phase_id)


@router.get("/phases/{phase_id}/prompt/versions/{version}")
async def get_phase_prompt_version(phase_id: str, version: int):
    """Get a specific prompt version's content."""
    return await _shared.frontend_api.get_phase_prompt_version(phase_id, version)


@router.post("/phases/{phase_id}/prompt/versions")
async def create_phase_prompt_version(phase_id: str, body: Dict[str, Any]):
    """Create a new prompt version."""
    return await _shared.frontend_api.create_phase_prompt_version(phase_id, body)


@router.post("/phases/{phase_id}/prompt/versions/{version}/publish")
async def publish_phase_prompt_version(phase_id: str, version: int):
    """Publish a draft version as active."""
    return await _shared.frontend_api.publish_phase_prompt_version(phase_id, version)


@router.post("/phases/{phase_id}/prompt/versions/{version}/restore")
async def restore_phase_prompt_version(phase_id: str, version: int):
    """Restore an older version as a new active version."""
    return await _shared.frontend_api.restore_phase_prompt_version(phase_id, version)


@router.get("/phases/{phase_id}/prompt/preview")
async def get_phase_prompt_preview(
    phase_id: str, variables: Optional[str] = Query(None)
):
    """Render a preview of the assembled prompt (from DB)."""
    import json

    try:
        var_dict = json.loads(variables) if variables else None
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(
            status_code=400, detail="Invalid JSON in variables parameter"
        )
    return await _shared.frontend_api.get_phase_prompt_preview(phase_id, var_dict)


@router.post("/phases/{phase_id}/prompt/preview")
async def post_phase_prompt_preview(phase_id: str, body: Dict[str, Any]):
    """Render a preview of the assembled prompt with draft content."""
    try:
        from src.core.database import DatabaseManager, Phase
        from src.prompts.assembler import PromptAssembler

        db_manager = DatabaseManager("hephaestus.db")
        with db_manager.get_session() as session:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                raise HTTPException(status_code=404, detail="Phase not found")

            all_phases = (
                session.query(Phase)
                .filter_by(workflow_id=phase.workflow_id)
                .order_by(Phase.order)
                .all()
            )
            phases_list = [
                {
                    "order": p.order,
                    "name": p.name,
                    "description": p.description,
                    "done_definitions": p.done_definitions or [],
                    "outputs": p.outputs,
                }
                for p in all_phases
            ]

        assembler = PromptAssembler(
            phase_description=body.get("description", phase.description or ""),
            done_definitions=body.get(
                "done_definitions", phase.done_definitions or []
            ),
            additional_notes=body.get("additional_notes", phase.additional_notes),
            outputs=body.get("outputs", phase.outputs),
            next_steps=body.get("next_steps", phase.next_steps),
            working_directory=phase.working_directory,
            phase_order=phase.order,
            phase_name=phase.name,
        )
        result = assembler.render(
            variables=body.get("variables", {}),
            all_phases=phases_list,
        )
        return {
            "system_prompt": result.system_prompt,
            "user_prompt": result.user_prompt,
            "variables_used": result.variables_used,
            "variables_missing": result.variables_missing,
            "warnings": result.warnings,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/phases/{phase_id}/prompt/diff")
async def get_phase_prompt_diff(
    phase_id: str, v1: int = Query(...), v2: int = Query(...)
):
    """Get diff between two prompt versions."""
    return await _shared.frontend_api.get_phase_prompt_diff(phase_id, v1, v2)


@router.get("/tasks/{task_id}/prompt")
async def get_task_prompt(task_id: str):
    """Get the assembled prompt for a task (with overrides applied)."""
    from src.prompts.assembler import assemble_task_prompt

    result = assemble_task_prompt(task_id)
    return {
        "system_prompt": result.system_prompt,
        "user_prompt": result.user_prompt,
    }


@router.get("/tasks/{task_id}/prompt/overrides")
async def get_task_prompt_overrides(task_id: str):
    """Get prompt overrides for a task."""
    return await _shared.frontend_api.get_task_prompt_overrides(task_id)


@router.put("/tasks/{task_id}/prompt/overrides")
async def set_task_prompt_overrides(task_id: str, body: Dict[str, Any]):
    """Set prompt overrides for a task."""
    return await _shared.frontend_api.set_task_prompt_overrides(task_id, body)


@router.delete("/tasks/{task_id}/prompt/overrides")
async def clear_task_prompt_overrides(task_id: str):
    """Clear prompt overrides for a task."""
    return await _shared.frontend_api.clear_task_prompt_overrides(task_id)


