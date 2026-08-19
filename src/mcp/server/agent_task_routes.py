"""Agent-facing task lifecycle routes: create_task, update_task_status.

Extracted from src/mcp/server.py (design_docs/phase_1c_server_decomposition.md).
"""

import asyncio
import json
import logging
import uuid

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
)

from src.core.database import Phase, TaskStatus
from src.mcp.server._create_task_steps import (
    _apply_enrichment_to_task,
    _apply_ticket_blocking_if_needed,
    _check_duplicate_active_task_for_phase,
    _check_for_duplicate_task,
    _dispatch_agent_for_task,
    _enforce_ticket_tracking_requirement,
    _finalize_task_dispatch,
    _guard_phase_ownership,
    _handle_task_processing_failure,
    _maybe_queue_task_at_capacity,
    _persist_new_task,
    _resolve_dedup_phase_id,
    _resolve_phase_and_enrich,
)
from src.mcp.server._shared import (
    CreateTaskRequest,
    CreateTaskResponse,
    UpdateTaskStatusRequest,
    UpdateTaskStatusResponse,
    _touch_agent_activity,
    server_state,
    verify_agent_authentication,
)
from src.mcp.server._update_task_status_steps import (
    _authorize_agent_for_task,
    _broadcast_task_completion,
    _complete_task_normally,
    _log_self_review_telemetry,
    _maybe_fire_self_review_gate,
    _maybe_fire_spec_gate,
    _resolve_task_for_status_update,
    _run_done_hard_floor_checks,
    _spawn_validation_for_task,
)

# Import routers at module level for test compatibility

logger = logging.getLogger("src.mcp.server.agent_task_routes")

router = APIRouter()

@router.post("/create_task", response_model=CreateTaskResponse)
async def create_task(
    request: CreateTaskRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Create a new task with automatic enrichment and agent assignment."""
    from src.core.log_context import set_log_context

    # SECURITY: Verify agent authentication before allowing task creation
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    _touch_agent_activity(agent_id)
    set_log_context(agent=agent_id)
    logger.info(f"Creating task from agent {agent_id}: {request.task_description[:100]}...")

    try:
        _enforce_ticket_tracking_requirement(agent_id, request)

        task_id = str(uuid.uuid4())

        if request.workflow_id:
            dedup_phase_id = _resolve_dedup_phase_id(agent_id, request)
            duplicate_response = _check_duplicate_active_task_for_phase(request, dedup_phase_id)
            if duplicate_response:
                return duplicate_response
            _guard_phase_ownership(agent_id, request, dedup_phase_id)

        _persist_new_task(agent_id, request, task_id)

        blocked_response = await _apply_ticket_blocking_if_needed(request, task_id)
        if blocked_response:
            return blocked_response

        # Process the rest asynchronously
        async def process_task_async():
            try:
                ctx = await _resolve_phase_and_enrich(request, agent_id)
                enriched_task = ctx["enriched_task"]

                task_data = _apply_enrichment_to_task(
                    task_id, request, ctx["phase_id"], ctx["workflow_id"], enriched_task
                )
                if not task_data:
                    return

                if await _check_for_duplicate_task(task_id, ctx["phase_id"], enriched_task):
                    return  # Don't create agent for duplicates

                if await _maybe_queue_task_at_capacity(task_id, request, enriched_task):
                    return  # Don't create agent yet

                agent = await _dispatch_agent_for_task(
                    task_id,
                    task_data,
                    agent_id,
                    request,
                    enriched_task,
                    ctx["context_memories"],
                    ctx["project_context"],
                    ctx["working_directory"],
                )
                if agent is None:
                    return  # Queued instead of dispatched

                await _finalize_task_dispatch(task_id, task_data, agent, enriched_task)

                logger.info(f"Task {task_id} processed successfully in background")

            except Exception as e:
                await _handle_task_processing_failure(task_id, e)

        asyncio.create_task(process_task_async())

        # Return immediately with pending status
        return CreateTaskResponse(
            task_id=task_id,
            enriched_description=f"[Processing] {request.task_description}",
            assigned_agent_id="pending",
            estimated_completion_time=25,
            status="pending",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create task: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/validate_agent_id/{agent_id}")
async def validate_agent_id(agent_id: str):
    """Quick endpoint for agents to validate their ID format.

    Returns:
        Success if ID matches UUID format, error otherwise
    """
    import re

    uuid_pattern = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

    if uuid_pattern.match(agent_id):
        return {
            "valid": True,
            "message": f"✅ Agent ID {agent_id} is valid UUID format",
        }
    else:
        return {
            "valid": False,
            "message": f"❌ Agent ID '{agent_id}' is NOT valid. Use the UUID from your initial prompt.",
            "common_mistakes": [
                "Using 'agent-mcp' instead of actual UUID",
                "Using 'main-session-agent' when you're not the main session",
                "Typo in UUID",
            ],
        }

@router.post("/update_task_status", response_model=UpdateTaskStatusResponse)
async def update_task_status(
    request: UpdateTaskStatusRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Update task status when complete or failed."""
    from src.core.log_context import set_log_context

    # SECURITY: Verify agent authentication before allowing status updates
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    _touch_agent_activity(agent_id)
    set_log_context(agent=agent_id, task=request.task_id)
    logger.info(f"Updating task {request.task_id} status to {request.status}")

    # There's no dedicated column for structured verdict/count data agents
    # sometimes attach (e.g. a scope-review gate's verdict + issue counts) --
    # fold it into the summary text so it's preserved everywhere summary
    # already flows (completion_notes, memories, etc.) instead of adding a
    # new storage path for what's still just descriptive detail.
    if request.metadata:
        request.summary = (f"{request.summary}\n\n[metadata] {json.dumps(request.metadata)}").strip()

    from src.services.task_completion_service import TaskCompletionService

    # FIX #5: Wrap entire handler in try/finally to prevent session leaks
    # on early returns (404, 403, rejection dict).
    session = server_state.db_manager.get_session()
    try:
        task = _resolve_task_for_status_update(session, request)
        _authorize_agent_for_task(session, agent_id, task, request)

        # Idempotency guard: a task already in a terminal state (done/
        # failed/duplicated) has nothing left to process. Without this, a
        # redundant completion call -- observed live, caused by the CLI's
        # own auto-compact replaying the initial task prompt, which reads
        # to the agent as a fresh request to redo work it already reported
        # done -- re-runs the ENTIRE pipeline below: record_learnings
        # (duplicate memory writes each time), self-review, output-artifact
        # re-verification, and cost re-collection, none of which are
        # idempotent. Short-circuiting here isn't just an optimization --
        # it's what actually stops the loop, since each redundant call now
        # costs nothing instead of a full reprocessing pass that itself
        # takes long enough to trigger the next auto-compact.
        if task.status in TaskStatus.TERMINAL:
            logger.info(
                f"Task {request.task_id[:8]} is already '{task.status}' -- "
                f"redundant {request.status} call from agent {agent_id[:8]}, "
                "returning success without reprocessing"
            )
            return UpdateTaskStatusResponse(
                success=True,
                message=f"Task already {task.status} — no action needed",
                termination_scheduled=True,
            )

        if request.status == "done" and not request.summary.strip():
            raise HTTPException(
                status_code=400,
                detail="summary is required when status='done' -- describe what "
                "was accomplished so the pipeline and UI have something real to "
                "show instead of guessing from output files.",
            )

        await TaskCompletionService.record_learnings(session, agent_id, request.task_id, request.key_learnings, request.code_changes)

        if request.status == "done" and not task.has_results:
            logger.warning(f"Task {request.task_id} completed without formal results reported")

        # Fetched once and reused below (self-review gate + the hard-floor
        # checks below both need this same task's Phase row).
        phase = session.query(Phase).filter_by(id=task.phase_id).first() if task.phase_id else None

        self_review_response = await _maybe_fire_self_review_gate(session, task, phase, agent_id, request)
        if self_review_response:
            return self_review_response

        _log_self_review_telemetry(session, task)

        hard_floor_response = _run_done_hard_floor_checks(session, task, phase)
        if hard_floor_response:
            return hard_floor_response

        if request.status == "done" and task.phase_id:
            await TaskCompletionService.create_tickets_from_forensics_report(session, task)

        validation_spawned = False
        output_lost_rejection = None
        if request.status == "done" and task.validation_enabled:
            await _spawn_validation_for_task(session, task, agent_id, request)
            validation_spawned = True
        else:
            output_lost_rejection = await _complete_task_normally(session, agent_id, task, request, phase)

        await _maybe_fire_spec_gate(session, task, request, output_lost_rejection)

        await _broadcast_task_completion(task, agent_id, request, output_lost_rejection)

        # Return appropriate response based on whether validation was spawned
        if output_lost_rejection:
            return UpdateTaskStatusResponse(
                success=False,
                message=output_lost_rejection["message"],
                termination_scheduled=True,
            )
        elif validation_spawned:
            return UpdateTaskStatusResponse(
                success=True,
                message="Task submitted for validation. A validation agent has been spawned - please wait for validation results.",
                termination_scheduled=False,  # Agent kept alive for validation feedback
            )
        else:
            return UpdateTaskStatusResponse(
                success=True,
                message=f"Task {request.status} successfully",
                termination_scheduled=True,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update task status: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
