"""Stuck/health/credit detection and recovery decisions."""

import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple


from src.core.database import (
    Workflow,
    get_db,
)

from src.autopilot.orchestrator.engine_client import (
    get_agents,
    get_tasks,
    terminate_agent_direct,
)
from src.autopilot.orchestrator.phase_transitions import (
    _retry_failed_tasks,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.autopilot.orchestrator import OrchestratorLogger

logger = logging.getLogger(__name__)


ACTIVE_AGENT_STATUSES = {
    "working",
    "idle",
}  # Excludes 'created' (not yet started), 'stuck', 'terminated'


STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS = 10  # ~10 min at the default 60s scan interval


def _workflow_appears_abandoned(workflow_id: str) -> bool:
    """True if nothing is currently happening for this workflow: no active
    agents and no task in any non-terminal status.

    Used only to decide whether a workflow stuck "active" past
    STALE_ACTIVE_WORKFLOW_TIMEOUT_SECONDS is genuinely abandoned (e.g. a
    phase's task completed but the next phase's task was never created --
    a restart mid-flight can lose that in-memory progress with nothing to
    resume it) versus still legitimately doing real work. A workflow with
    any active agent or any pending/in_progress/assigned/queued/etc. task
    is never considered abandoned, no matter how long it's been running.
    """
    try:
        agents = get_agents(workflow_id=workflow_id)
        if any(a.get("status") in ACTIVE_AGENT_STATUSES for a in agents):
            return False
        non_terminal_statuses = (
            "pending",
            "in_progress",
            "assigned",
            "queued",
            "under_review",
            "validation_in_progress",
            "needs_work",
            "blocked",
        )
        for status in non_terminal_statuses:
            if get_tasks(status=status, workflow_id=workflow_id):
                return False
        # If all tasks are done AND all phases are completed, the workflow is completed, not abandoned
        all_tasks = get_tasks(workflow_id=workflow_id)
        if all_tasks and all(t.get("status") == "done" for t in all_tasks):
            # Also check that all phases are completed
            from src.core.database import PhaseExecution, Phase
            with get_db() as db:
                incomplete_phases = db.query(PhaseExecution).join(
                    Phase, PhaseExecution.phase_id == Phase.id
                ).filter(
                    Phase.workflow_id == workflow_id,
                    PhaseExecution.status != "completed"
                ).count()
                if incomplete_phases == 0:
                    return False
        return True
    except Exception:
        # Can't verify either signal -- treat as NOT abandoned (don't risk
        # force-failing a workflow we can't positively confirm is idle).
        return False


def _update_resumed_workflow_recovery_attempts(workflow_id: str, recovery_attempts: int) -> int:
    """Advance run_continuous_pipeline's per-resume "recovery attempts"
    counter for a workflow that isn't fully complete yet.

    Resets to 0 on real activity instead of incrementing regardless --
    without this, the counter measured only "scans since this orchestrator
    process last resumed the workflow", not "scans with no actual
    progress", so ANY workflow not fully done within its threshold got
    killed even with a real agent actively mid-phase. Observed live:
    adversarial_review's agent completed its task successfully, and the
    workflow was force-failed about two minutes later anyway, purely
    because enough scans had elapsed since a backend restart. Mirrors
    _escalate_stale_active_workflows' streak-reset-on-activity pattern.
    """
    if not _workflow_appears_abandoned(workflow_id):
        return 0
    return recovery_attempts + 1


def _escalate_stale_active_workflows(
    active_workflows: list,
    abandoned_streak: Dict[str, int],
    logger: "OrchestratorLogger",
) -> List[str]:
    """Self-heal for run_continuous_pipeline's "wait for active workflow"
    gate, which otherwise has no escalation and blocks the design queue
    forever on a workflow that stays "active" in the DB but never actually
    progresses again (e.g. a backend restart mid-flight loses a multi-
    feature pipeline's in-memory progress between one feature finishing
    and the next feature's task being created, with nothing else
    positioned to notice or resume it).

    Marks a workflow "failed" once it's been observed abandoned (see
    _workflow_appears_abandoned) on STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS
    CONSECUTIVE calls -- any call where it shows real activity resets its
    streak to zero, so this only fires on genuinely sustained abandonment,
    never on a workflow that's just between two real actions.

    Args:
        active_workflows: raw get_active_workflows() result for this cycle.
        abandoned_streak: workflow_id -> consecutive abandoned-observation
            count, mutated in place so state persists across calls.

    Returns:
        workflow_ids that are still legitimately blocking (real activity,
        or not yet past the streak threshold) -- i.e. what the caller
        should still wait on.
    """
    still_blocking = []
    for wf in active_workflows:
        wf_id = wf.get("id", "")
        if not _workflow_appears_abandoned(wf_id):
            abandoned_streak.pop(wf_id, None)
            still_blocking.append(wf_id)
            continue

        streak = abandoned_streak.get(wf_id, 0) + 1
        if streak < STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS:
            abandoned_streak[wf_id] = streak
            still_blocking.append(wf_id)
            continue

        logger.warning(f"Workflow {wf_id[:8]} has shown no agent/task activity for {streak} consecutive scans -- marking failed so the design queue can proceed")
        try:
            with get_db() as _db:
                _wf_row = _db.query(Workflow).filter_by(id=wf_id).first()
                if _wf_row and _wf_row.status == "active":
                    _wf_row.status = "failed"
                    _wf_row.status_reason = f"Abandoned: no agent/task activity for {streak} consecutive scans -- likely lost mid-flight across a backend restart"
        except Exception as e:
            logger.error(f"Failed to mark stale workflow {wf_id[:8]} as failed: {e}")
        abandoned_streak.pop(wf_id, None)

    # Drop tracking for workflows no longer reported active.
    current_ids = {wf.get("id", "") for wf in active_workflows}
    for tracked_id in list(abandoned_streak):
        if tracked_id not in current_ids:
            abandoned_streak.pop(tracked_id, None)

    return still_blocking


def attempt_recovery(workflow_id: str, logger: "OrchestratorLogger") -> Tuple[bool, str]:
    """Attempt to recover issues found by is_design_fully_complete.

    Actions:
    1. Retry failed tasks by creating new agents
    2. Merge unmerged agent branches to main
    3. Terminate stale agents

    Returns:
        (success, message) tuple
    """
    recovered = []

    # 1. Retry failed tasks
    recovered.extend(_retry_failed_tasks(workflow_id, logger))

    # 1b. Clean stale "assigned" tasks whose agent is terminated
    try:
        from src.core.database import Agent as _Agent
        from src.core.database import Task as _Task
        from src.core.database import get_db as _get_db

        with _get_db() as _db:
            assigned_tasks = (
                _db.query(_Task)
                .filter(
                    _Task.workflow_id == workflow_id,
                    _Task.status.in_(["assigned", "in_progress"]),
                )
                .all()
            )
            for task in assigned_tasks:
                if task.assigned_agent_id:
                    agent = _db.query(_Agent).filter_by(id=task.assigned_agent_id).first()
                    if agent and agent.status == "terminated":
                        logger.info(f"  Task {task.id[:8]} assigned to terminated agent {task.assigned_agent_id[:8]} — marking failed")
                        task.status = "failed"
                        task.failure_reason = f"Agent {task.assigned_agent_id[:8]} terminated unexpectedly"
                        _db.commit()
                        recovered.append(f"cleaned stale task {task.id[:8]}")
    except Exception as e:
        logger.error(f"  Failed to clean stale assigned tasks: {e}")

    # 2. Clean stale merge state if repo is dirty (do NOT merge branches here —
    #    the WorktreeManager handles merges in update_task_status. Raw git merge
    #    corrupts the repo because attempt_recovery runs from the orchestrator's
    #    thread, not the agent's worktree context.)
    try:
        # Get project path from workflow's working directory
        project_path = None
        try:
            with get_db() as _db:
                _wf = _db.query(Workflow).filter_by(id=workflow_id).first()
                if _wf and _wf.working_directory and Path(_wf.working_directory).exists():
                    project_path = _wf.working_directory
        except Exception:
            pass
        if not project_path:
            project_path = os.getenv("PROJECT_PATH")
        if not project_path:
            if recovered:
                return True, f"Recovered: {', '.join(recovered)}"
            return False, "No recovery actions needed"  # Can't determine project path
        # Check if repo needs cleanup
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_path,
        )
        is_dirty = bool(status_result.stdout.strip())
        merge_in_progress = Path(project_path, ".git", "MERGE_HEAD").exists()

        if is_dirty or merge_in_progress:
            # Abort any in-progress merge that's blocking the repo
            subprocess.run(
                ["git", "merge", "--abort"],
                capture_output=True,
                timeout=10,
                cwd=project_path,
            )
            # Ensure we're on main
            subprocess.run(
                ["git", "checkout", "main"],
                capture_output=True,
                timeout=10,
                cwd=project_path,
            )
            # Clean untracked files that accumulate from failed merges
            subprocess.run(
                ["git", "clean", "-fd"],
                capture_output=True,
                timeout=10,
                cwd=project_path,
            )
            # Reset any staged but uncommitted changes
            subprocess.run(
                ["git", "reset", "--hard", "HEAD"],
                capture_output=True,
                timeout=10,
                cwd=project_path,
            )
            recovered.append("cleaned repo state")
    except Exception as e:
        logger.warning(f"  Failed to clean repo state: {e}")

    # 3. Terminate stale agents -- only genuinely stale ones (dead tmux
    # session), never merely "still working". This function runs on every
    # recovery cycle (every POLL_INTERVAL) whenever is_design_fully_complete
    # says the workflow isn't done -- which is the normal state for a
    # workflow with real in-progress work, e.g. right after a restart
    # reloads state.current_workflow_id into this branch. Terminating every
    # "working" agent unconditionally here killed live, actively-progressing
    # agents roughly once a minute until the workflow ran out of retries
    # (observed live: a security_review agent got killed and replaced three
    # times in six minutes, purely because this step never checked whether
    # the agent was actually still alive).
    agents = get_agents(workflow_id=workflow_id)
    active_agents = [a for a in agents if a.get("status") in ("working", "starting", "idle")]
    for agent in active_agents:
        aid = agent.get("id", "")
        tmux_name = agent.get("tmux_session_name")
        try:
            alive = (
                bool(tmux_name)
                and subprocess.run(
                    ["tmux", "has-session", "-t", tmux_name],
                    capture_output=True,
                    timeout=3,
                ).returncode
                == 0
            )
        except Exception:
            alive = False
        if alive:
            continue  # genuinely still working -- leave it alone
        logger.info(f"  Terminating stale agent {aid[:8]} (tmux session dead)")
        try:
            terminate_agent_direct(aid)
            recovered.append(f"terminated agent {aid[:8]}")
        except Exception as e:
            logger.warning(f"  Failed to terminate {aid[:8]}: {e}")

    if recovered:
        return True, f"Recovered: {', '.join(recovered)}"
    return False, "No recovery actions needed"


def check_api_credits() -> Tuple[bool, str]:
    """Check if any agents or tasks hit API credit/rate-limit errors.

    Uses specific patterns to avoid false positives on words like
    "credited", "exceeded expectations", or discussions about HTTP codes.
    """
    # Specific phrases that indicate actual credit/rate-limit issues
    credit_phrases = [
        "insufficient funds",
        "quota exceeded",
        "rate limit exceeded",
        "rate_limit_exceeded",
        "payment required",
        "out of credits",
        "credit balance",
        "billing error",
        "429 too many requests",
        "402 payment required",
    ]
    # Error keywords in agent status/error fields (not raw output)
    credit_keywords_in_error = [
        "credit",
        "quota",
        "billing",
        "payment",
    ]

    agents = get_agents()
    for agent in agents:
        # Check agent status error field (more reliable than raw output)
        agent_error = (agent.get("error", "") or "").lower()
        agent_status = (agent.get("status", "") or "").lower()

        # Check for explicit error status with credit keywords
        if agent_status == "error":
            for keyword in credit_keywords_in_error:
                if keyword in agent_error:
                    return (
                        True,
                        f"API credit issue in agent {agent.get('id', '')[:8]}: {keyword}",
                    )

        # Check output log for specific phrases (not broad keywords)
        output = (agent.get("output_log", "") or "").lower()
        for phrase in credit_phrases:
            if phrase in output:
                return True, f"API credit issue: {phrase}"

    failed_tasks = get_tasks(status="failed")
    for task in failed_tasks:
        error = (task.get("error", "") or "").lower()
        for phrase in credit_phrases:
            if phrase in error:
                return True, f"API credit issue in task: {phrase}"

    return False, ""


def detect_hard_error(agents: list, failed_tasks: list, workflow_id: str = None) -> Tuple[bool, str]:
    # Filter to only tasks from the current workflow if provided
    if workflow_id:
        failed_tasks = [t for t in failed_tasks if t.get("workflow_id") == workflow_id]

    # Check for crashed/errored agents (agents list is already scoped by get_agents)
    crashed_agents = [a for a in agents if a.get("status") == "error"]
    if crashed_agents:
        names = [a.get("id", "unknown")[:20] for a in crashed_agents[:3]]
        return True, f"Crashed agents: {', '.join(names)}"

    critical_failures = [t for t in failed_tasks if t.get("priority") == "critical" or "architectural" in (t.get("description", "") or "").lower()]
    if critical_failures:
        descs = [t.get("description", "")[:60] for t in critical_failures[:3]]
        return True, f"Critical task failures: {descs}"

    return False, ""


def detect_impasse(agents: list, pending_tasks: list, in_progress_tasks: list, elapsed_seconds: int = 0) -> Tuple[bool, str]:
    """Detect if the workflow is stuck.

    Parent-child model: check if tasks are progressing, not health_check_failures.
    """
    active_agents = [a for a in agents if a.get("status") in ACTIVE_AGENT_STATUSES]

    # If there are pending tasks but no active agents, something is wrong
    # But give a generous grace period for agents to start. The monitor needs
    # time to: detect phase completion → evaluate with engine → create task →
    # spawn agent. With 60s polling intervals, this can take 2-3 minutes.
    # Also check if any pending task was recently created (monitor is working on it).
    if not active_agents and pending_tasks and elapsed_seconds > 600:
        # Check if any pending task was created recently (within last 120s)
        # If so, the monitor is likely about to spawn an agent — don't trigger impasse.
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        for task in pending_tasks:
            created = task.get("created_at")
            if created:
                try:
                    created_dt = datetime.fromisoformat(created)
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=timezone.utc)
                    task_age = (now - created_dt).total_seconds()
                    if task_age < 120:
                        # Task was just created — monitor is likely spawning agent
                        return False, ""
                except Exception:
                    pass
        return True, f"No active agents but {len(pending_tasks)} tasks pending"

    # Check for agents that have been working too long without progress
    # (assigned tasks that never move to done)
    if in_progress_tasks and not pending_tasks:
        # Tasks are in progress - check if they've been stuck
        for task in in_progress_tasks:
            started = task.get("started_at")
            if started:
                from datetime import datetime, timezone

                try:
                    started_dt = datetime.fromisoformat(started)
                    if started_dt.tzinfo is None:
                        started_dt = started_dt.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - started_dt).total_seconds()
                    if elapsed > 1800:  # 30 minutes
                        return (
                            True,
                            f"Task {task.get('id', '?')[:8]} stuck for {int(elapsed)}s",
                        )
                except Exception:
                    pass

    return False, ""


def detect_architectural_issue(report_paths: List[str]) -> Tuple[bool, str]:
    for report_path in report_paths:
        p = Path(report_path)
        if not p.exists():
            continue
        try:
            content = p.read_text().lower()
            arch_keywords = [
                "major architectural issue",
                "needs redesign",
                "fundamental flaw",
                "wrong approach",
                "should not proceed",
                "must rewrite",
            ]
            for kw in arch_keywords:
                if kw in content:
                    return True, f"Architectural issue in {p.name}: '{kw}'"
        except Exception:
            pass
    return False, ""
