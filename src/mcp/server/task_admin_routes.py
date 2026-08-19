"""User/dashboard task operations, health check, websocket, and root routes.

Extracted from src/mcp/server.py (design_docs/phase_1c_server_decomposition.md).
"""

import logging
from datetime import datetime

from fastapi import (
    APIRouter,
    Body,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)

from src.core.database import (
    Phase,
    Task,
    Workflow,
)
from src.mcp.server._shared import server_state

# Import routers at module level for test compatibility

logger = logging.getLogger("src.mcp.server.task_admin_routes")

router = APIRouter()

@router.get("/api/workflows")
async def get_workflows_endpoint(
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Get all workflows."""
    logger.info(f"Agent {agent_id} fetching workflows")

    try:
        session = server_state.db_manager.get_session()
        try:
            workflows = session.query(Workflow).all()

            return [
                {
                    "id": w.id,
                    "name": w.name,
                    "status": w.status,
                    "phases_folder_path": w.phases_folder_path,
                    "created_at": w.created_at.isoformat() if w.created_at else None,
                }
                for w in workflows
            ]
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Failed to fetch workflows: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/tasks/{task_id}/pause")
async def pause_task_endpoint(task_id: str):
    """Pause a single task: terminate its agent (if any, WIP is committed by
    terminate_agent) and mark it 'blocked' so it won't be picked up again until
    Resume is pressed. Mirrors /features/{id}/pause's per-task logic, scoped to
    just this one task.
    """
    logger.info(f"Pause request for task {task_id}")

    try:
        session = server_state.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            if task.status not in (
                "pending",
                "queued",
                "assigned",
                "in_progress",
                "under_review",
                "validation_in_progress",
                "needs_work",
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot pause task in '{task.status}' status",
                )

            agent_id = task.assigned_agent_id
            task_workflow_id = task.workflow_id
            task.status = "blocked"
            task.assigned_agent_id = None
            session.commit()
        finally:
            session.close()

        if agent_id:
            await server_state.agent_manager.terminate_agent(agent_id)

        from src.core.database import resolve_project_for_workflow

        bcast_project_id, bcast_project_name = resolve_project_for_workflow(task_workflow_id)
        await server_state.broadcast_update(
            {"type": "task_paused", "task_id": task_id},
            project_id=bcast_project_id,
            project_name=bcast_project_name,
        )

        return {"success": True, "task_id": task_id, "status": "blocked"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to pause task {task_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/bump_task_priority")
async def bump_task_priority_endpoint(
    task_id: str = Body(..., embed=True),
):
    """Bump a queued task and start it immediately, bypassing the agent limit.

    This allows urgent tasks to start even when at max capacity (e.g., 2/2 → 3/2).
    When agents complete, the system returns to the configured limit.
    """
    logger.info(f"Priority bump & start request for task {task_id}")

    try:
        session = server_state.db_manager.get_session()
        try:
            # Verify task exists and is queued
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            if task.status != "queued":
                raise HTTPException(
                    status_code=400,
                    detail=f"Task {task_id} is not queued (status: {task.status})",
                )

        finally:
            session.close()

        # Boost the task priority first
        success = server_state.queue_service.boost_task_priority(task_id)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to boost task priority")

        # Dequeue and start immediately (bypassing limit)
        session = server_state.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            # Dequeue the task
            server_state.queue_service.dequeue_task(task_id)
        finally:
            session.close()

        from src.services.agent_dispatch_service import AgentDispatchService

        dispatch_context = await AgentDispatchService.build_dispatch_context(
            task_description_for_rag=task.enriched_description or task.raw_description,
            phase_id=task.phase_id,
        )

        # This endpoint's whole point is "start now, bypassing the global
        # max_concurrent_agents cap" -- but a per-cli/model concurrency
        # limit (e.g. a local model's single inference slot) is a different
        # kind of constraint: forcing a 2nd agent onto it doesn't actually
        # start it any sooner, it just sits frozen waiting its turn like
        # any other agent would. Dispatch on the fallback model instead
        # when saturated; if no fallback is usable, dispatch on the primary
        # anyway (with a warning) rather than silently queue it -- queueing
        # would contradict this endpoint's "start immediately" contract.
        qs = server_state.queue_service
        _reservation = None
        if qs.cli_model_concurrency_limits:
            with qs.db_manager.session_scope() as _qsession:
                _cli_override, _model_override, _reservation, _saturated = qs.resolve_cli_model_dispatch(
                    _qsession, task
                )
            if _saturated:
                logger.warning(
                    f"Task {task_id}'s combo is at its concurrency limit with no usable "
                    "fallback -- dispatching anyway (bump-priority bypasses limits)"
                )
            elif _cli_override:
                logger.info(
                    f"Task {task_id}'s primary combo at its concurrency limit -- "
                    f"dispatching on fallback model {_model_override} instead"
                )
                dispatch_context["phase_cli_tool"] = _cli_override
                dispatch_context["phase_cli_model"] = _model_override

        # Create agent immediately (bypassing agent limit). _reservation
        # (if any) must be released once this dispatch attempt finishes,
        # success or not.
        try:
            agent = await AgentDispatchService.dispatch(
                task=task,
                enriched_data={"enriched_description": task.enriched_description},
                dispatch_context=dispatch_context,
            )
        finally:
            if _reservation:
                qs.release_cli_model_slot(*_reservation)

        # Update task status
        AgentDispatchService.mark_assigned(task_id, agent.id, status="assigned")

        # Broadcast update
        from src.core.database import resolve_project_for_workflow

        bcast_project_id, bcast_project_name = resolve_project_for_workflow(task.workflow_id)
        await server_state.broadcast_update(
            {
                "type": "task_priority_bumped",
                "task_id": task_id,
                "agent_id": agent.id,
            },
            project_id=bcast_project_id,
            project_name=bcast_project_name,
        )

        logger.info(f"Task {task_id} bumped and agent {agent.id} created (bypassing limit)")

        return {
            "success": True,
            "message": f"Task {task_id[:8]} started immediately (bypassing agent limit)",
            "agent_id": agent.id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to bump and start task: {e}")
        import traceback

        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/tasks/{task_id}/cancel")
async def cancel_task_endpoint(task_id: str):
    """Cancel a task by ID."""
    logger.info(f"Cancel request for task {task_id}")

    try:
        session = server_state.db_manager.get_session()
        cancelled_task_id = None
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                # Try prefix match with escaped LIKE wildcards
                escaped = task_id.replace("%", "\\%").replace("_", "\\_")
                task = session.query(Task).filter(Task.id.like(f"{escaped}%", escape="\\")).first()
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            # Only allow cancelling pending or queued tasks
            if task.status not in ("pending", "queued"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot cancel task in '{task.status}' status. Terminate the assigned agent first.",
                )

            task.status = "failed"
            task.failure_reason = "Cancelled by user"
            task.completed_at = datetime.utcnow()
            cancelled_task_id = task.id
            cancelled_task_workflow_id = task.workflow_id
            session.commit()

        finally:
            session.close()

        if cancelled_task_id:
            from src.core.database import resolve_project_for_workflow

            bcast_project_id, bcast_project_name = resolve_project_for_workflow(cancelled_task_workflow_id)
            await server_state.broadcast_update(
                {
                    "type": "task_cancelled",
                    "task_id": cancelled_task_id,
                },
                project_id=bcast_project_id,
                project_name=bcast_project_name,
            )

            logger.info(f"Task {cancelled_task_id} cancelled")
            return {"success": True, "task_id": cancelled_task_id}
        else:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cancel task: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/tasks/{task_id}")
async def delete_task_endpoint(task_id: str):
    """Permanently delete a single task and its dependent records.

    Unlike pause/cancel (which only apply to pending/queued/in-progress
    tasks and leave the row in place), this removes the task outright in
    any status -- for the specific case of an old, stuck task (e.g. a
    stale run's task sitting 'blocked' or 'in_progress' with a long-dead
    agent) that just clutters the queue view with no path to actually
    disappear otherwise.
    """
    logger.info(f"Delete request for task {task_id}")

    from sqlalchemy.exc import IntegrityError

    from src.core.database import (
        AgentResult,
        CostEntry,
        Memory,
        TaskPromptOverride,
        Ticket,
        ValidationReview,
        resolve_project_for_workflow,
    )

    try:
        session = server_state.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            agent_id = task.assigned_agent_id
            deleted_workflow_id = task.workflow_id
        finally:
            session.close()

        # Terminate the assigned agent first (if any) -- terminate_agent
        # itself clears Agent.current_task_id, which this task's own FK
        # deletion below would otherwise violate (foreign_keys=ON).
        if agent_id:
            await server_state.agent_manager.terminate_agent(agent_id)

        session = server_state.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            # Same dependent-record set rerun_design's own task cleanup
            # deletes for the same reason (FK constraints are enforced).
            session.query(TaskPromptOverride).filter_by(task_id=task_id).delete(synchronize_session=False)
            session.query(ValidationReview).filter_by(task_id=task_id).delete(synchronize_session=False)
            session.query(AgentResult).filter_by(task_id=task_id).delete(synchronize_session=False)
            session.query(Memory).filter_by(related_task_id=task_id).delete(synchronize_session=False)
            session.query(Ticket).filter_by(task_id=task_id).delete(synchronize_session=False)
            # CostEntry.task_id is also an enforced FK -- any task that ever
            # recorded real LLM cost (increasingly the common case, not the
            # exception) would otherwise fail to delete with an IntegrityError.
            session.query(CostEntry).filter_by(task_id=task_id).delete(synchronize_session=False)

            session.delete(task)
            session.commit()
        except IntegrityError as e:
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot delete task {task_id}: other records still reference it "
                    f"(e.g. a subtask or diagnostic run) -- {e}"
                ),
            )
        finally:
            session.close()

        bcast_project_id, bcast_project_name = resolve_project_for_workflow(deleted_workflow_id)
        await server_state.broadcast_update(
            {"type": "task_deleted", "task_id": task_id},
            project_id=bcast_project_id,
            project_name=bcast_project_name,
        )

        logger.info(f"Task {task_id} deleted")
        return {"success": True, "task_id": task_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete task {task_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/tasks/{task_id}/complete")
async def complete_task_as_user(
    task_id: str,
    summary: str = Body(..., embed=True, min_length=1),
):
    """Record human-verified completion after an agent cannot report back.

    This intentionally accepts only ``done`` and performs the same output
    checks as an agent completion before changing a failed task's state.
    It is for local operator recovery, not a general status-update API.
    """
    from src.core.database import resolve_project_for_workflow
    from src.services.task_completion_service import TaskCompletionService

    session = server_state.db_manager.get_session()
    try:
        task = session.query(Task).filter_by(id=task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        if task.status == "done":
            return {"success": True, "task_id": task.id, "message": "Task already done"}
        if task.status not in ("failed", "blocked"):
            raise HTTPException(
                status_code=409,
                detail=f"Only failed or blocked tasks can be human-completed (status: {task.status})",
            )

        phase = session.query(Phase).filter_by(id=task.phase_id).first() if task.phase_id else None
        for verify in (
            TaskCompletionService.verify_output_artifact,
            TaskCompletionService.verify_gate_result_schema,
            TaskCompletionService.verify_no_open_tickets,
        ):
            rejection = verify(session, task, phase=phase)
            if rejection:
                raise HTTPException(status_code=400, detail=rejection["message"])

        task.status = "done"
        task.completed_at = datetime.utcnow()
        task.completion_notes = summary.strip()
        task.failure_reason = None
        workflow_id = task.workflow_id

        # Mirror update_task_status's normal completion path: commit the
        # worktree and re-verify the declared output(s) survived the commit
        # before advancing the pipeline. Skipped for git_commit_push itself
        # -- the operator completing this phase manually has already done
        # the actual commit/push/PR outside Hephaestus; the pipeline must
        # never commit on its own here (see AgentManager.create_agent_for_
        # task's PermissionError guard for the same phase).
        output_lost_rejection = None
        if not phase or phase.name != "git_commit_push":
            await TaskCompletionService.commit_and_link_ticket(
                session, task.assigned_agent_id or "human-operator", task, summary.strip()
            )
            output_lost_rejection = TaskCompletionService.verify_output_survived_commit(session, task, phase=phase)
            if output_lost_rejection:
                task.status = "failed"
                task.failure_reason = output_lost_rejection["message"]
                session.commit()
                raise HTTPException(status_code=400, detail=output_lost_rejection["message"])

        from src.autopilot.orchestrator.phase_transitions import fire_spec_gate_if_ready
        await fire_spec_gate_if_ready(session, task)
        session.commit()
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logger.error(f"Failed to human-complete task {task_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

    project_id, project_name = resolve_project_for_workflow(workflow_id)
    await server_state.broadcast_update(
        {"type": "task_completed", "task_id": task_id, "human_verified": True},
        project_id=project_id,
        project_name=project_name,
    )
    return {"success": True, "task_id": task_id, "message": "Task marked done"}

@router.post("/api/cancel_queued_task")
async def cancel_queued_task_endpoint(
    task_id: str = Body(..., embed=True),
):
    """Cancel a queued task and remove it from the queue.

    The task will be marked as failed and removed from the queue.
    """
    logger.info(f"Cancel request for queued task {task_id}")

    try:
        session = server_state.db_manager.get_session()
        try:
            # Verify task exists and is queued
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            if task.status != "queued":
                raise HTTPException(
                    status_code=400,
                    detail=f"Task {task_id} is not queued (status: {task.status})",
                )

            # Mark task as failed
            task.status = "failed"
            task.failure_reason = "Cancelled by user from queue"
            task.completed_at = datetime.utcnow()
            queued_task_workflow_id = task.workflow_id
            session.commit()

        finally:
            session.close()

        # Remove from queue
        server_state.queue_service.dequeue_task(task_id)

        # Broadcast update
        from src.core.database import resolve_project_for_workflow

        bcast_project_id, bcast_project_name = resolve_project_for_workflow(queued_task_workflow_id)
        await server_state.broadcast_update(
            {
                "type": "task_cancelled",
                "task_id": task_id,
            },
            project_id=bcast_project_id,
            project_name=bcast_project_name,
        )

        logger.info(f"Task {task_id} cancelled and removed from queue")

        return {
            "success": True,
            "message": f"Task {task_id[:8]} cancelled and removed from queue",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cancel queued task: {e}")
        import traceback

        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/restart_task")
async def restart_task_endpoint(
    task_id: str = Body(..., embed=True),
):
    """Restart a completed or failed task.

    This will:
    - Clear completion data (failure_reason, completion_notes, completed_at)
    - Clear trajectory data (guardian analyses, steering interventions)
    - Reset task to pending/queued status
    - Create new agent or queue based on capacity
    """
    logger.info(f"Restart request for task {task_id}")

    try:
        session = server_state.db_manager.get_session()
        try:
            # Verify task exists and is done/failed
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            if task.status not in ["done", "failed", "blocked"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Can only restart completed, failed, or paused tasks (current status: {task.status})",
                )

            # Get agent ID before clearing (to delete trajectory data)
            old_agent_id = task.assigned_agent_id
            restart_task_workflow_id = task.workflow_id

            # Clear completion data
            task.status = "pending"
            task.assigned_agent_id = None
            task.started_at = None
            task.completed_at = None
            task.completion_notes = None
            task.failure_reason = None
            # This row is reused (not recreated) on restart -- without
            # clearing these too, a task previously tagged action="goto"/
            # "retry" by _tag_completing_task keeps showing that badge
            # (and a now-meaningless action_target_phase) after being
            # restarted into an unrelated fresh attempt.
            task.action = ""
            task.action_target_phase = None

            # Reopen-point fix (same as _create_corrective_task): resetting
            # the task alone isn't enough if its workflow/phase already
            # thinks it's "completed". Observed live: restarting an
            # already-done task left it stuck pending forever once the
            # agent-creation below got interrupted (e.g. a backend restart
            # mid-request) -- the phase-advancement sweep only ever
            # reconsiders phases that are pending/in_progress, so a task
            # sitting pending under a "completed" phase/workflow is
            # invisible to every self-heal path and nothing ever recreates
            # its agent. Without this, restart_task is only safe when its
            # own inline agent-creation below never fails.
            if task.workflow_id:
                from src.core.database import PhaseExecution, Workflow

                wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
                if wf and wf.status == "paused":
                    # A restartable task can be "blocked" -- exactly what
                    # pause_feature sets on a paused workflow's in-flight
                    # tasks -- so this is reachable on a genuinely paused
                    # workflow, not just a "completed" one. A bare
                    # wf.status = "active" here would leave paused_by/
                    # paused_at stale, the same bug class as this item's
                    # other resume-side fixes.
                    from src.autopilot.orchestrator.engine_client import resume_workflow
                    resume_workflow(task.workflow_id, force=True, session=session)
                elif wf and wf.status != "active":
                    wf.status = "active"
                if task.phase_id:
                    execution = session.query(PhaseExecution).filter_by(phase_id=task.phase_id).first()
                    if execution and execution.status != "in_progress":
                        execution.status = "in_progress"
                        execution.task_creation_claimed_at = None

            session.commit()

        finally:
            session.close()

        # Clear trajectory data for old agent
        if old_agent_id:
            session = server_state.db_manager.get_session()
            try:
                from src.core.database import GuardianAnalysis, SteeringIntervention

                # Delete guardian analyses
                session.query(GuardianAnalysis).filter_by(agent_id=old_agent_id).delete()

                # Delete steering interventions
                session.query(SteeringIntervention).filter_by(agent_id=old_agent_id).delete()

                session.commit()
                logger.info(f"Cleared trajectory data for agent {old_agent_id}")

            finally:
                session.close()

        # Check if we should queue or create agent immediately
        should_queue = server_state.queue_service.should_queue_task()

        from src.core.database import resolve_project_for_workflow

        bcast_project_id, bcast_project_name = resolve_project_for_workflow(restart_task_workflow_id)

        if should_queue:
            # Queue the task
            server_state.queue_service.enqueue_task(task_id)
            logger.info(f"Task {task_id} restarted and queued")

            # Broadcast update
            await server_state.broadcast_update(
                {
                    "type": "task_restarted",
                    "task_id": task_id,
                    "status": "queued",
                },
                project_id=bcast_project_id,
                project_name=bcast_project_name,
            )

            return {
                "success": True,
                "message": f"Task {task_id[:8]} restarted and added to queue",
                "status": "queued",
            }
        else:
            # Create agent immediately
            session = server_state.db_manager.get_session()
            try:
                task = session.query(Task).filter_by(id=task_id).first()
            finally:
                session.close()

            from src.services.agent_dispatch_service import AgentDispatchService

            # NOTE: this now also fetches phase CLI config, which the
            # previous inline version of this endpoint did not (only
            # bump_task_priority_endpoint did) — an inconsistency flagged
            # in docs/SOLID_OO_REVIEW.md finding 1.3 that this shared
            # dispatch-context helper fixes by construction.
            dispatch_context = await AgentDispatchService.build_dispatch_context(
                task_description_for_rag=task.enriched_description or task.raw_description,
                phase_id=task.phase_id,
            )

            # Per-cli/model concurrency gate -- same reasoning as create_task's
            # 6.6 (this endpoint's own should_queue_task above only covers the
            # global cap). Fall back to the fallback model if the primary
            # combo is saturated; queue the task if no fallback is usable.
            qs = server_state.queue_service
            _reservation = None
            if qs.cli_model_concurrency_limits:
                with qs.db_manager.session_scope() as _qsession:
                    _cli_override, _model_override, _reservation, _saturated = qs.resolve_cli_model_dispatch(
                        _qsession, task
                    )
                if _saturated:
                    logger.info(
                        f"Task {task_id}'s combo is already at its concurrency limit with no "
                        "usable fallback -- queueing instead of dispatching"
                    )
                    qs.enqueue_task(task_id)
                    await server_state.broadcast_update(
                        {"type": "task_restarted", "task_id": task_id, "status": "queued"},
                        project_id=bcast_project_id,
                        project_name=bcast_project_name,
                    )
                    return {
                        "success": True,
                        "message": f"Task {task_id[:8]} restarted and added to queue",
                        "status": "queued",
                    }
                if _cli_override:
                    logger.info(
                        f"Task {task_id}'s primary combo at its concurrency limit -- "
                        f"dispatching on fallback model {_model_override} instead"
                    )
                    dispatch_context["phase_cli_tool"] = _cli_override
                    dispatch_context["phase_cli_model"] = _model_override

            # Create agent for the task. _reservation (if any) must be
            # released once this dispatch attempt finishes, success or not.
            try:
                agent = await AgentDispatchService.dispatch(
                    task=task,
                    enriched_data={"enriched_description": task.enriched_description},
                    dispatch_context=dispatch_context,
                )
            finally:
                if _reservation:
                    qs.release_cli_model_slot(*_reservation)

            # Update task status
            AgentDispatchService.mark_assigned(task_id, agent.id, status="assigned")

            logger.info(f"Task {task_id} restarted with new agent {agent.id}")

            # Broadcast update
            await server_state.broadcast_update(
                {
                    "type": "task_restarted",
                    "task_id": task_id,
                    "agent_id": agent.id,
                    "status": "assigned",
                },
                project_id=bcast_project_id,
                project_name=bcast_project_name,
            )

            return {
                "success": True,
                "message": f"Task {task_id[:8]} restarted with new agent",
                "agent_id": agent.id,
                "status": "assigned",
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to restart task: {e}")
        import traceback

        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/queue_status")
async def get_queue_status_endpoint():
    """Get current queue status information.

    Returns information about active agents, queued tasks, and available slots.
    """
    try:
        status = server_state.queue_service.get_queue_status()
        return status
    except Exception as e:
        logger.error(f"Failed to get queue status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates."""
    await websocket.accept()
    server_state.active_websockets.append(websocket)

    try:
        while True:
            # Keep connection alive and handle any incoming messages
            data = await websocket.receive_text()
            # Echo back or handle commands
            await websocket.send_json({"type": "echo", "data": data})

    except WebSocketDisconnect:
        server_state.active_websockets.remove(websocket)
        logger.info("WebSocket client disconnected")

@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0",
    }

@router.get("/")
async def root():
    """Root endpoint with MCP protocol info."""
    return {
        "name": "Hephaestus MCP Server",
        "version": "1.0.0",
        "protocol_version": "1.0",
        "description": "Model Context Protocol server for AI agent orchestration",
        "capabilities": {
            "tools": True,
            "resources": True,
            "prompts": False,
            "auth": {"type": "none", "required": False},
        },
        "endpoints": [
            "/hephaestus_create_task",
            "/hephaestus_update_task_status",
            "/hephaestus_save_memory",
            "/agent_status",
            "/task_progress",
            "/health",
            "/ws",
            "/sse",
            "/tools",
            "/resources",
        ],
    }
