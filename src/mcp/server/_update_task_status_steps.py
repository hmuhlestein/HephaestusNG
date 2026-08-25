"""Named steps for update_task_status -- extracted from
agent_task_routes.py's update_task_status god-function
(design_docs/phase_1c_server_decomposition.md exit criteria).

Each function below is a verbatim-logic extraction of one section of the
original update_task_status body -- behavior-preserving, not a rewrite. See
agent_task_routes.py's update_task_status for the orchestrator that calls
these in sequence.
"""

import asyncio
import functools
import logging
from typing import Optional

from fastapi import HTTPException
from git import Repo

from src.core.database import Agent, AgentLog, Phase, Task, utc_now
from src.mcp.server._create_task_steps import _dispatch_ready_dependents
from src.mcp.server._shared import (
    SELF_REVIEW_CHECKLIST_PROMPT,
    UpdateTaskStatusRequest,
    UpdateTaskStatusResponse,
    _resolve_worktree_head_sha,
    _resolve_worktree_path,
    server_state,
    spawn_background_task,
)
from src.mcp.server.background_loops import terminate_agents_and_process_queue

logger = logging.getLogger("src.mcp.server._update_task_status_steps")


def _resolve_task_for_status_update(session, request: UpdateTaskStatusRequest) -> Task:
    """Look up the task, including the truncated-8-char-id resolution
    fallback agents sometimes use (this codebase's own logs display
    task.id[:8] everywhere). Raises 404 if not found / ambiguous."""
    task = session.query(Task).filter_by(id=request.task_id).first()
    if not task and len(request.task_id) < 36:
        candidates = (
            session.query(Task)
            .filter(Task.id.like(f"{request.task_id}%"))
            .all()
        )
        if len(candidates) == 1:
            task = candidates[0]
            logger.warning(
                f"Agent used truncated task_id "
                f"'{request.task_id}' -- resolved unambiguously to {task.id}"
            )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def _authorize_agent_for_task(session, agent_id: str, task: Task, request: UpdateTaskStatusRequest) -> None:
    """Three-tier assignee check: current assignee, or current_task_id match
    (handles retry scenarios where a new agent is dispatched for the same
    task but the old still-running agent tries to report), or was-ever-
    assigned (handles a terminated agent whose tmux session is still trying
    to report). Raises 403 if none match."""
    is_current_assignee = task.assigned_agent_id == agent_id
    if is_current_assignee:
        return

    agent_record = session.query(Agent).filter_by(id=agent_id).first()
    if agent_record and agent_record.current_task_id == request.task_id:
        logger.warning(
            f"Agent {agent_id[:8]} updating task {request.task_id[:8]} but is not current assignee (current: {task.assigned_agent_id}). Allowing because agent's current_task_id matches."
        )
        return

    agent_was_assigned = (
        session.query(AgentLog)
        .filter(
            AgentLog.agent_id == agent_id,
            AgentLog.log_type == "created",
            AgentLog.details["task_id"].as_string() == request.task_id,
        )
        .first()
    )
    if not agent_was_assigned:
        agent_logs = (
            session.query(AgentLog)
            .filter(
                AgentLog.agent_id == agent_id,
                AgentLog.log_type == "created",
            )
            .all()
        )
        for log in agent_logs:
            if log.details and log.details.get("task_id") == request.task_id:
                agent_was_assigned = log
                break

    if agent_was_assigned:
        logger.warning(
            f"Agent {agent_id[:8]} updating task {request.task_id[:8]} "
            f"but is not current assignee (current: {task.assigned_agent_id}). "
            f"Allowing because agent was previously assigned to this task (terminated agent completing work)."
        )
    else:
        raise HTTPException(status_code=403, detail="Agent not authorized for this task")


async def _maybe_fire_self_review_gate(
    session, task: Task, phase: Optional[Phase], agent_id: str, request: UpdateTaskStatusRequest
) -> Optional[UpdateTaskStatusResponse]:
    """One-shot self-review (docs/GAP_CHECK_SELF_LOOP_DESIGN.md) -- the first
    "done" from a phase with self_review enabled doesn't complete the task;
    it sends a fixed checklist back to the same (still-running) agent and
    requires a second "done" call. Returns the early-return response if
    fired, else None."""
    if not (request.status == "done" and task.phase_id and not task.self_review_done):
        return None
    if not (phase and phase.self_review and phase.self_review.get("enabled", False)):
        return None

    # Set BEFORE messaging -- crash-safe. If the process dies before the
    # message is delivered, the worst case is a skipped prompt, not an
    # infinite re-trigger of this branch on retry.
    task.self_review_done = True
    task.self_review_started_at = utc_now()
    # _resolve_worktree_head_sha does real GitPython I/O (Repo().head) --
    # blocking, same class of issue as commit_and_link_ticket/
    # collect_cost_on_completion below, fixed here too since this path
    # fires on every "done" for any self_review-enabled phase.
    loop = asyncio.get_event_loop()
    task.self_review_started_commit = await loop.run_in_executor(
        None, _resolve_worktree_head_sha, session, task
    )
    task.completion_notes = request.summary
    session.commit()

    logger.info(f"[SELF-REVIEW] Task {task.id[:8]} (phase {phase.name}) fired — agent {agent_id[:8]}, worktree HEAD {(task.self_review_started_commit or 'unknown')[:8]}")

    await server_state.agent_manager.send_message_to_agent(agent_id, SELF_REVIEW_CHECKLIST_PROMPT)

    return UpdateTaskStatusResponse(
        success=True,
        message="Self-review requested — re-check your work, then call update_task_status(done) again.",
        termination_scheduled=False,
    )


def _diff_stat_since(worktree_path: str, since_commit: str) -> Optional[str]:
    """`git diff --stat` since a commit -- real GitPython I/O, called via
    run_in_executor by _log_self_review_telemetry below."""
    try:
        repo = Repo(worktree_path)
        return repo.git.diff(since_commit, "HEAD", stat=True)
    except Exception as e:
        logger.debug(f"[SELF-REVIEW] Could not diff worktree: {e}")
        return None


async def _log_self_review_telemetry(session, task: Task) -> None:
    """This task went through the self-review gate on a prior call and is
    now completing for real. Log elapsed time and a diff-stat of what
    changed during the review pass."""
    if not (task.self_review_started_at is not None):
        return

    elapsed = (utc_now() - task.self_review_started_at).total_seconds()
    diff_stat = None
    if task.self_review_started_commit:
        worktree_path = _resolve_worktree_path(session, task)
        if worktree_path:
            loop = asyncio.get_event_loop()
            diff_stat = await loop.run_in_executor(
                None, _diff_stat_since, worktree_path, task.self_review_started_commit
            )
    logger.info(
        f"[SELF-REVIEW] Task {task.id[:8]} completed {elapsed:.0f}s after self-review fired. Diff since review: {diff_stat.strip() if diff_stat else '(no changes / diff unavailable)'}"
    )
    # Clear so this doesn't re-log if 'done' is ever seen again for the same
    # task (shouldn't normally happen once status is terminal).
    task.self_review_started_at = None
    task.self_review_started_commit = None
    session.commit()


def _run_done_hard_floor_checks(session, task: Task, phase: Optional[Phase]) -> Optional[UpdateTaskStatusResponse]:
    """Mechanical hard floors that reject 'done' when a general (not
    phase-special-cased) requirement isn't met: the declared output artifact
    exists, its structured result matches the gate's expected schema, and no
    open bug tickets remain. Each is a documented instruction alone would be
    compliance-dependent; these make the requirement enforced. Returns the
    rejection response for the first failing check, else None."""
    from src.services.task_completion_service import TaskCompletionService

    if not task.phase_id:
        return None

    checks = (
        (TaskCompletionService.verify_output_artifact, "Output validation failed", "output-artifact"),
        (TaskCompletionService.verify_gate_result_schema, "Gate result schema invalid", "gate-result-schema"),
        (TaskCompletionService.verify_no_open_tickets, "Open tickets remain unresolved", "open-tickets"),
    )
    for check_fn, default_message, floor_name in checks:
        rejection = check_fn(session, task, phase=phase)
        if rejection:
            # rejection is a plain {"status", "message"} dict (not the
            # response_model's shape) -- returning it directly makes
            # FastAPI's response_model validation fail with a 500 (missing
            # 'success'/'termination_scheduled'), which hides the actual
            # rejection reason from the agent and instead just looks like a
            # broken server, causing blind retries.
            #
            # Persist the reason on the task even though status stays
            # non-terminal: if this agent's session ends (times out, killed)
            # before it retries, _clean_stale_assigned_tasks will mark this
            # task "failed" with only a generic "agent terminated" message
            # -- without this, the specific validation problem is lost, and
            # the orchestrator's retry (_maybe_retry_failed_tasks) respawns
            # a fresh agent with no memory of what actually needs fixing.
            task.failure_reason = rejection.get("message", default_message)
            session.commit()
            logger.warning(
                f"[{task.id[:8]}] 'done' rejected by {floor_name} hard floor: "
                f"{task.failure_reason}"
            )
            return UpdateTaskStatusResponse(
                success=False,
                message=rejection.get("message", default_message),
                termination_scheduled=False,
            )
    return None


async def _spawn_validation_for_task(session, task: Task, agent_id: str, request: UpdateTaskStatusRequest) -> None:
    """Agent claims done but the phase has validation enabled: flip to
    under_review, keep the reporting agent alive, and spawn a validation
    agent asynchronously (like create_task's background processing)."""
    task.status = "under_review"
    task.validation_iteration += 1
    task.completion_notes = request.summary

    # Capture task attributes before async function (to avoid detached
    # instance issues)
    task_validation_iteration = task.validation_iteration
    task_workflow_id = task.workflow_id

    session.commit()

    # Mark original agent as kept alive for validation (do this immediately)
    agent = session.query(Agent).filter_by(id=agent_id).first()
    if agent:
        agent.kept_alive_for_validation = True
        session.commit()

    from src.services.task_completion_service import TaskCompletionService

    # Terminal-state transition log: the task is now under_review and the
    # reporting agent is deliberately kept alive -- without this line the
    # only trace of "why is this agent still running after claiming done"
    # is the response payload sent back to the agent itself.
    logger.info(
        f"Task {task.id[:8]} flipped to under_review (validation iteration "
        f"{task_validation_iteration}); agent {agent_id[:8]} kept alive for "
        "validation feedback"
    )

    spawn_background_task(
        TaskCompletionService.spawn_validation(
            agent_id=agent_id,
            task_id=request.task_id,
            task_workflow_id=task_workflow_id,
            task_validation_iteration=task_validation_iteration,
        )
    )


async def _complete_task_normally(
    session, agent_id: str, task: Task, request: UpdateTaskStatusRequest, phase: Optional[Phase]
) -> Optional[dict]:
    """No validation (or task failed) -- set the terminal status, collect
    cost data, commit in the shared worktree and link the ticket on success,
    re-verify output survived the commit, and schedule agent termination +
    queue processing. Returns the output_lost_rejection dict if the declared
    output vanished after the commit, else None."""
    from src.services.task_completion_service import TaskCompletionService

    task.status = request.status
    task.completed_at = utc_now()
    task.completion_notes = request.summary

    if request.status == "failed":
        task.failure_reason = request.failure_reason
    elif request.status == "done":
        # Clear any failure_reason left over from an earlier failed attempt
        # on this same task row (goto/retry reuses it) -- otherwise a task
        # that ultimately succeeds keeps showing its last failure forever.
        task.failure_reason = None

    session.commit()

    # Collect cost data for completed tasks (done or failed -- failed tasks
    # still consumed LLM tokens and should be attributed). Runs on every
    # single task completion, reads the CLI's own transcript file, and
    # cascades through the same synchronous task -> workflow -> feature
    # -> design -> project cost rollup as _invoke_and_record's own cost
    # recording (langchain_llm_client.py) -- same class of "blocks the
    # single-threaded event loop" issue that call site was fixed for,
    # confirmed live 2026-08-19 (intermittent /health timeouts under
    # concurrent agent load). Offloaded here too rather than only where
    # it was first observed, since the underlying function is identically
    # synchronous and this call site runs even more often (every
    # completion, not just every LLM call with a nonzero cost).
    if request.status in ("done", "failed"):
        import asyncio

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, TaskCompletionService.collect_cost_on_completion, request.task_id
        )

    output_lost_rejection = None
    if request.status == "done":
        await TaskCompletionService.commit_and_link_ticket(session, agent_id, task, request.summary)
        # Re-verify the declared output(s) are still there right after the
        # commit -- catches the file having vanished between the pre-commit
        # check and here, e.g. an agent whose last actual write landed
        # outside its worktree. Flips the task back to "failed" instead of
        # letting a real loss stand as a silent "done". Offloaded like
        # collect_cost_on_completion above -- its fallback path does real
        # GitPython history search (repo.iter_commits) when the file isn't
        # found directly in the worktree.
        loop = asyncio.get_event_loop()
        output_lost_rejection = await loop.run_in_executor(
            None, functools.partial(TaskCompletionService.verify_output_survived_commit, session, task, phase=phase)
        )

    if task.status == "done":
        # Checks task.status, NOT request.status -- verify_output_survived_commit
        # just above can flip task.status to "failed" in place (same ORM
        # object, same session) when the declared output vanished after
        # commit. request.status still reads "done" (it's the caller's
        # ORIGINAL request, never mutated) even when that happened, so
        # keying off it here would promote dependents of a task that was
        # just rejected. Fires the dependency-promotion sweep alongside the
        # existing capacity-queue drain below -- a task that finished can
        # unblock BOTH a capacity-queued task (already handled) and a
        # dependency-gated one (this), independent reasons a pending task
        # might not have dispatched yet.
        spawn_background_task(_dispatch_ready_dependents(task.id, task.workflow_id))

    spawn_background_task(
        terminate_agents_and_process_queue(server_state.agent_manager, [agent_id])
    )

    return output_lost_rejection


async def _maybe_fire_spec_gate(session, task: Task, request: UpdateTaskStatusRequest, output_lost_rejection: Optional[dict]) -> None:
    """When a gated phase task completes and the phase is now complete,
    trigger the gate immediately (don't wait for monitor poll) -- the
    orchestrator's _advance_phases only fires when the next phase is
    pending, and misses it if already in_progress.

    Must run AFTER the worktree commit (_complete_task_normally), not
    before: a goto decision deletes the gate phase's result files
    (consume_gate_artifacts) so a later re-run can't re-score stale ones.
    Firing before the commit would lose a report the agent had just
    written, before it was ever captured in git history.

    Skipped entirely if output_lost_rejection fired -- the task is "failed"
    now, not "done", so the phase isn't actually complete."""
    if not (request.status == "done" and task.phase_id and not output_lost_rejection):
        return
    from src.autopilot.orchestrator.phase_transitions import fire_spec_gate_if_ready

    await fire_spec_gate_if_ready(session, task)


async def _broadcast_task_completion(
    task: Task, agent_id: str, request: UpdateTaskStatusRequest, output_lost_rejection: Optional[dict]
) -> None:
    from src.core.database import resolve_project_for_workflow

    bcast_project_id, bcast_project_name = resolve_project_for_workflow(task.workflow_id)
    await server_state.broadcast_update(
        {
            "type": "task_completed",
            "task_id": request.task_id,
            "agent_id": agent_id,
            "status": "failed" if output_lost_rejection else request.status,
            "summary": request.summary[:200],
        },
        project_id=bcast_project_id,
        project_name=bcast_project_name,
    )
