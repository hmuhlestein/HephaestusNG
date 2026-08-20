"""Workflow-definition and workflow-execution routes.

Extracted from src/mcp/server.py (design_docs/phase_1c_server_decomposition.md).
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)

from src.core.database import (
    Agent,
    Task,
)
from src.mcp.server._shared import CreateTaskRequest, RegisterWorkflowDefinitionRequest, StartWorkflowRequest, server_state
from src.mcp.server.agent_task_routes import create_task
from src.mcp.server.lifecycle import _resume_interrupted_workflows

# Import routers at module level for test compatibility

logger = logging.getLogger("src.mcp.server.workflow_execution_routes")

router = APIRouter()

def _kill_tmux_session(tmux_session_name: Optional[str]) -> None:
    """`tmux kill-session` -- real subprocess work (up to the 5s timeout),
    called via run_in_executor by stop_workflow/cancel_workflow below so a
    workflow with several agents doesn't block the event loop for their
    combined kill time on one Stop/Cancel click."""
    import subprocess

    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", tmux_session_name],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass

@router.get("/api/workflow-definitions")
async def list_workflow_definitions():
    """List all loaded workflow definitions."""
    try:
        definitions = server_state.phase_manager.list_definitions()
    except Exception as e:
        logger.error(f"Failed to list workflow definitions: {e}")
        return {"definitions": []}

    result = []
    for d in definitions:
        try:
            phases = d.phases_config
            if isinstance(phases, str):
                import json as _json

                phases = _json.loads(phases)
            config = d.workflow_config
            if isinstance(config, str):
                import json as _json

                config = _json.loads(config)
            result.append(
                {
                    "id": d.id,
                    "name": d.name,
                    "description": d.description,
                    "phases_count": len(phases) if phases else 0,
                    "has_result": (config or {}).get("has_result", False),
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                    "launch_template": (config or {}).get("launch_template"),
                }
            )
        except Exception as e:
            logger.error(f"Error processing definition {d.id}: {e}")
            result.append(
                {
                    "id": d.id,
                    "name": d.name,
                    "description": d.description,
                    "error": str(e),
                }
            )

    return {"definitions": result}

@router.post("/api/workflow-definitions")
async def register_workflow_definition(request: RegisterWorkflowDefinitionRequest):
    """Register a workflow definition."""
    logger.info(f"Registering workflow definition: {request.id}")
    try:
        server_state.phase_manager.register_definition(
            definition_id=request.id,
            name=request.name,
            description=request.description,
            phases_config=request.phases_config,
            workflow_config=request.workflow_config,
        )
        logger.info(f"Successfully registered workflow definition: {request.id}")
        return {
            "id": request.id,
            "name": request.name,
            "status": "registered",
            "message": f"Workflow definition '{request.name}' registered successfully",
        }
    except Exception as e:
        logger.error(f"Failed to register workflow definition {request.id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/api/workflow-executions")
async def list_workflow_executions(status: str = "all"):
    """List all workflow executions."""
    executions = server_state.phase_manager.list_active_executions(status)
    return {
        "executions": [
            {
                "id": e.id,
                "definition_id": e.definition_id,
                "definition_name": e.definition.name if e.definition else None,
                "description": e.description,
                "status": e.status,
                "status_reason": e.status_reason,
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "working_directory": e.working_directory,
                # Add stats
                "stats": server_state.phase_manager.get_execution_stats(e.id),
            }
            for e in executions
        ]
    }

@router.post("/api/workflow-executions")
async def start_workflow_execution(request: StartWorkflowRequest):
    """Start a new workflow execution from a definition."""
    logger.info(f"Starting workflow execution: definition={request.definition_id}, desc={request.description}, launch_params={request.launch_params}")
    try:
        # start_execution now returns (workflow_id, initial_task_info)
        result = server_state.phase_manager.start_execution(
            definition_id=request.definition_id,
            description=request.description,
            working_directory=request.working_directory,
            launch_params=request.launch_params,
            design_id=request.design_id,
        )

        # Handle both old (just workflow_id) and new (tuple) return formats
        if isinstance(result, tuple):
            workflow_id, initial_task_info = result
        else:
            workflow_id = result
            initial_task_info = None

        logger.info(f"Successfully started workflow execution: {workflow_id}")

        # If there's an initial task to create, create it through the proper flow
        if initial_task_info:
            # Claim the right to create this phase's first task before doing
            # any of the slower work below. The orchestrator's background
            # self-heal (_case_start_first_phase / _case_in_progress_no_tasks
            # in orchestrator.py) independently creates a task for any
            # in-progress phase it finds with zero tasks -- without this
            # claim, both paths can decide to create phase 1's task and a
            # duplicate agent gets spawned (observed live: burned a full
            # agent run duplicating work the first task had already done).
            phase_uuid = initial_task_info.get("phase_uuid")
            if phase_uuid:
                from src.autopilot.orchestrator.phase_transitions import _claim_phase_task_creation
                from src.core.database import get_db as _get_db_for_claim

                with _get_db_for_claim() as _claim_db:
                    won_claim = _claim_phase_task_creation(_claim_db, phase_uuid)
                if not won_claim:
                    logger.info(f"Phase 1 task for workflow {workflow_id} is already being created by another path -- skipping")
                    initial_task_info = None

        if initial_task_info:
            logger.info(f"Creating initial Phase 1 task for workflow {workflow_id}")
            try:
                # Create the task using internal task creation
                # This mimics what /create_task does but internally
                task_request = CreateTaskRequest(
                    task_description=initial_task_info["task_description"],
                    done_definition="Complete the initial phase task as described in the prompt",
                    ai_agent_id="main-session-agent",  # UI-launched task
                    priority=initial_task_info.get("priority", "high"),
                    phase_id=initial_task_info.get("phase_id", "1"),
                    workflow_id=workflow_id,
                )

                # Call the create_task endpoint handler directly
                # Use "main-session-agent" as the creator since this is a UI-launched task
                task_response = await create_task(request=task_request, agent_id="main-session-agent")
                logger.info(f"Created initial task {task_response.task_id} for workflow {workflow_id}")

                # create_task (the generic /create_task handler) knows
                # nothing about PhaseExecution bookkeeping -- see
                # _release_phase_task_creation_claim's own docstring for
                # what silently breaks without this call.
                try:
                    from src.autopilot.orchestrator.phase_transitions import _release_phase_task_creation_claim
                    from src.core.database import get_db as _get_db_for_release

                    with _get_db_for_release() as _pdb:
                        _release_phase_task_creation_claim(_pdb, phase_uuid)
                except Exception as claim_error:
                    logger.error(f"Failed to release phase 1 task-creation claim for workflow {workflow_id}: {claim_error}")
            except Exception as task_error:
                logger.error(f"Failed to create initial task for workflow {workflow_id}: {task_error}")
                # Don't fail the whole workflow creation, just log the error

        return {
            "workflow_id": workflow_id,
            "status": "active",
            "message": f"Started workflow execution: {request.description}",
        }
    except ValueError as e:
        logger.error(f"ValueError starting workflow: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error starting workflow execution: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/workflow-executions/{workflow_id}")
async def get_workflow_execution(workflow_id: str):
    """Get details of a specific workflow execution."""
    workflow = server_state.phase_manager.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")

    stats = server_state.phase_manager.get_execution_stats(workflow_id)

    # Get phases for this workflow execution
    phases = server_state.phase_manager.get_phases_for_workflow(workflow_id)

    # Get phase stats
    session = server_state.phase_manager.db_manager.get_session()
    try:
        phases_data = []
        for phase in phases:
            # Count tasks in this phase
            total_tasks = session.query(Task).filter_by(phase_id=phase.id).count()
            completed_tasks = session.query(Task).filter_by(phase_id=phase.id, status="done").count()
            active_tasks = session.query(Task).filter_by(phase_id=phase.id, status="in_progress").count()
            pending_tasks = session.query(Task).filter_by(phase_id=phase.id, status="pending").count()

            # Count active agents working on tasks in this phase
            active_agents = session.query(Agent).join(Task, Agent.current_task_id == Task.id).filter(Task.phase_id == phase.id, Agent.status == "working").count()

            phases_data.append(
                {
                    "id": phase.id,
                    "order": phase.order,
                    "name": phase.name,
                    "description": phase.description,
                    "active_agents": active_agents,
                    "total_tasks": total_tasks,
                    "completed_tasks": completed_tasks,
                    "active_tasks": active_tasks,
                    "pending_tasks": pending_tasks,
                    "cli_config": {
                        "cli_tool": phase.cli_tool,
                        "cli_model": phase.cli_model,
                        "glm_api_token_env": phase.glm_api_token_env,
                    },
                }
            )
    finally:
        session.close()

    return {
        "id": workflow.id,
        "definition_id": workflow.definition_id,
        "definition_name": workflow.definition.name if workflow.definition else None,
        "description": workflow.description,
        "status": workflow.status,
        "status_reason": workflow.status_reason,
        "created_at": workflow.created_at.isoformat() if workflow.created_at else None,
        "working_directory": workflow.working_directory,
        "stats": stats,
        "phases": phases_data,
    }

@router.post("/api/workflow-executions/{workflow_id}/complete")
async def complete_workflow_execution(workflow_id: str, request: Request):
    """Mark a workflow execution as completed (cleanup for orchestrator).
    Only allows localhost access for security."""
    # Security: only allow localhost calls for this destructive operation
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="Only localhost can force-complete workflows")

    session = server_state.db_manager.get_session()
    try:
        from src.core.database import Workflow

        workflow = session.query(Workflow).filter_by(id=workflow_id).first()
        if not workflow:
            raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")
        if workflow.status in ("completed", "failed", "cancelled"):
            return {"status": workflow.status, "message": "Already terminal"}
        workflow.status = "completed"
        session.commit()
        return {"status": "completed", "workflow_id": workflow_id}
    finally:
        session.close()

@router.post("/api/workflow-executions/{workflow_id}/stop")
async def stop_workflow(workflow_id: str, request: Request):
    """Stop a workflow and terminate all its agents."""
    import asyncio

    session = server_state.db_manager.get_session()
    try:
        from src.core.database import Agent, Task, Workflow

        workflow = session.query(Workflow).filter_by(id=workflow_id).first()
        if not workflow:
            raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")
        if workflow.status in ("completed", "failed", "paused"):
            return {"status": workflow.status, "message": "Already stopped"}

        # Find all tasks in this workflow
        tasks = session.query(Task).filter_by(workflow_id=workflow_id).all()
        task_ids = [t.id for t in tasks]

        # Find and terminate all agents working on these tasks
        terminated_count = 0
        if task_ids:
            from src.autopilot.orchestrator.engine_client import terminate_agent

            agents = session.query(Agent).filter(Agent.current_task_id.in_(task_ids)).filter(Agent.status.in_(["working", "starting", "idle"])).all()
            loop = asyncio.get_event_loop()
            for agent in agents:
                await loop.run_in_executor(None, _kill_tmux_session, agent.tmux_session_name)
                terminate_agent(agent.id, session=session)
                terminated_count += 1

            # Reset the tasks those agents were working on -- without this,
            # a task left "assigned"/"in_progress" pointing at a now-
            # terminated agent is indistinguishable from one whose agent is
            # still genuinely working, until an unrelated periodic sweep
            # (attempt_recovery's stale-assigned-task cleanup) eventually
            # notices the mismatch and fails it with a generic "terminated
            # unexpectedly" reason instead of resetting it for a clean
            # retry once this workflow resumes.
            for t in session.query(Task).filter(Task.id.in_(task_ids), Task.status.in_(["assigned", "in_progress"])).all():
                t.status = "pending"
                t.assigned_agent_id = None

        # Sets status/paused_by/paused_at together (and cascades to any
        # linked Feature) so the background sweep's
        # _try_auto_resume_paused_workflow leaves this alone instead of
        # silently reactivating it the moment it next sees a done task
        # sitting in an in-progress phase -- a state pausing itself
        # commonly produces. Without paused_by set, a user pause could get
        # reverted within one sweep tick (~20s), repeatedly, until whatever
        # made the phase look "stalled" happened to resolve on its own.
        from src.autopilot.orchestrator.engine_client import pause_workflow
        pause_workflow(workflow_id, reason="user", session=session)
        session.commit()

        return {
            "status": "paused",
            "workflow_id": workflow_id,
            "agents_terminated": terminated_count,
        }
    finally:
        session.close()

@router.post("/api/workflow-executions/{workflow_id}/resume")
async def resume_workflow(workflow_id: str, request: Request):
    """Resume a paused workflow."""
    session = server_state.db_manager.get_session()
    try:
        from src.autopilot.orchestrator.engine_client import resume_workflow as _resume_workflow_primitive
        from src.core.database import Workflow

        workflow = session.query(Workflow).filter_by(id=workflow_id).first()
        if not workflow:
            raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")
        if workflow.status != "paused":
            return {"status": workflow.status, "message": "Not paused"}

        # force=True: an explicit Resume-button click overrides any pause
        # reason, matching this endpoint's pre-existing unconditional
        # behavior (unlike the self-heal sweep, which must not override a
        # deliberate pause -- see resume_workflow's docstring).
        _resume_workflow_primitive(workflow_id, force=True, session=session)
        session.commit()
        return {"status": "active", "workflow_id": workflow_id}
    finally:
        session.close()

@router.post("/api/autopilot/recover")
async def recover_workflows(workflow_id: Optional[str] = None, project_id: Optional[str] = None):
    """Recover interrupted runs on demand (the UI 'Retry' action, and the
    project-level Play button's self-conflict cascade).

    Re-drives workflows whose in-flight phase agent died (crash / sleep / restart):
    restarts each orphaned agent on its existing worktree branch so the run continues
    from the last committed state. With workflow_id, scopes to that one run and flips
    a paused/failed workflow back to 'active' first (also resetting its failed/blocked
    tasks). With project_id instead, does the same across every one of that project's
    workflows. With neither, recovers all interrupted active/paused workflows
    (orphaned-agent restart only, no reactivate -- the passive startup-wide scan).
    """
    try:
        summary = await _resume_interrupted_workflows(
            workflow_id=workflow_id,
            project_id=project_id,
            reactivate=bool(workflow_id or project_id),
        )
        return {
            "recovered": True,
            "resumed_agents": summary.get("resumed", 0),
            "workflows": summary.get("workflows", []),
        }
    except Exception as e:
        logger.error(f"[RECOVER] on-demand recovery failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/workflow-executions/{workflow_id}/cancel")
async def cancel_workflow(workflow_id: str, request: Request):
    """Terminate agents and mark workflow as cancelled."""
    import asyncio

    session = server_state.db_manager.get_session()
    try:
        from src.core.database import Agent, Task, Workflow

        workflow = session.query(Workflow).filter_by(id=workflow_id).first()
        if not workflow:
            raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")

        # Terminate agents
        tasks = session.query(Task).filter_by(workflow_id=workflow_id).all()
        task_ids = [t.id for t in tasks]
        terminated_count = 0
        if task_ids:
            from src.autopilot.orchestrator.engine_client import terminate_agent

            agents = session.query(Agent).filter(Agent.current_task_id.in_(task_ids)).filter(Agent.status.in_(["working", "starting", "idle"])).all()
            loop = asyncio.get_event_loop()
            for agent in agents:
                await loop.run_in_executor(None, _kill_tmux_session, agent.tmux_session_name)
                terminate_agent(agent.id, session=session)
                terminated_count += 1

        # Mark every non-terminal task failed too -- otherwise a task whose
        # agent was just terminated above is left showing its last live
        # status (e.g. still "in_progress") even though nothing is working
        # on it anymore. Mirrors what pause_feature does for its "blocked" case.
        non_terminal = {
            "pending",
            "queued",
            "blocked",
            "assigned",
            "in_progress",
            "under_review",
            "validation_in_progress",
            "needs_work",
        }
        for task in tasks:
            if task.status in non_terminal:
                task.status = "failed"
                task.failure_reason = "Workflow cancelled by user"
                task.completed_at = datetime.utcnow()

        # Mark as failed (can't delete due to FK constraints, using failed to indicate user cancellation)
        workflow.status = "failed"
        session.commit()
        return {"cancelled": workflow_id, "agents_terminated": terminated_count}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
