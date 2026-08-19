"""Validator agent spawning for task-completion validation.

Extracted from src.services.task_completion_service.TaskCompletionService
per design_docs/phase_1b_decomposition.md section 4.4.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def spawn_validation(
    agent_id: str,
    task_id: str,
    task_workflow_id: Optional[str],
    task_validation_iteration: int,
) -> None:
    """Commit the agent's work and spawn a validator agent for a task
    marked 'done' with validation enabled.

    Runs as a background asyncio task, mirroring create_task's
    fire-and-forget pattern. On failure, marks the task failed and
    terminates the agent instead of leaving it dangling.
    """
    from src.core.app_context import get_app_state

    server_state = get_app_state()

    try:
        logger.info(f"Starting validation process for task {task_id}")

        commit_sha = None
        if hasattr(server_state, "branch_manager"):
            try:
                # commit_for_validation is a second, independent git add
                # -A/commit pair on the same worktree as commit_and_link_
                # ticket's own (task_completion/git_link.py) -- same real
                # subprocess cost, same fix: offload rather than block the
                # event loop directly. Confirmed live 2026-08-19,
                # investigating intermittent multi-second /health stalls.
                import asyncio
                import functools

                loop = asyncio.get_event_loop()
                commit_result = await loop.run_in_executor(
                    None,
                    functools.partial(
                        server_state.branch_manager.commit_for_validation,
                        agent_id=agent_id,
                        iteration=task_validation_iteration,
                    ),
                )
                commit_sha = commit_result.get("commit_sha")
            except Exception as e:
                logger.warning(f"Failed to create validation commit: {e}")

        from src.validation.validator_agent import spawn_validator_agent

        validator_id = await spawn_validator_agent(
            validation_type="task",
            target_id=task_id,
            workflow_id=task_workflow_id,
            commit_sha=commit_sha or "HEAD",
            db_manager=server_state.db_manager,
            branch_manager=getattr(server_state, "branch_manager", None),
            agent_manager=server_state.agent_manager,
            original_agent_id=agent_id,
        )

        from src.core.database import Task

        with server_state.db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id=task_id).first()
            if task:
                task.status = "validation_in_progress"
                logger.info(f"Task {task_id} validation spawned successfully, validator: {validator_id}")
            else:
                logger.error(f"Task {task_id} not found during validation update")

        from src.core.database import resolve_project_for_workflow

        bcast_project_id, bcast_project_name = resolve_project_for_workflow(task_workflow_id)
        await server_state.broadcast_update(
            {
                "type": "validation_started",
                "task_id": task_id,
                "validator_id": validator_id,
                "original_agent_id": agent_id,
            },
            project_id=bcast_project_id,
            project_name=bcast_project_name,
        )

    except Exception as e:
        logger.error(f"Failed to spawn validation for task {task_id}: {e}")
        from src.core.database import Task

        try:
            with server_state.db_manager.session_scope() as session:
                task = session.query(Task).filter_by(id=task_id).first()
                if task:
                    task.status = "failed"
                    task.failure_reason = f"Validation spawning failed: {str(e)}"

                await server_state.agent_manager.terminate_agent(agent_id)
        except Exception as inner_e:
            # FIX #17: Don't let task-update/termination errors propagate
            # and lose the original validation failure context (session_scope
            # already rolled back before re-raising here).
            logger.error(f"Failed to update task/terminate agent after validation failure: {inner_e}")

        # FIX #11/#17: Defer queue processing to avoid nested I/O in except block.
        try:
            from src.core.app_context import trigger_queue_processing

            trigger_queue_processing()
        except Exception as qe:
            logger.error(f"Failed to trigger queue processing after validation failure: {qe}")
