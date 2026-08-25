"""Stuck/health/credit detection and recovery decisions."""

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from src.autopilot.orchestrator.engine_client import (
    get_agents,
    get_tasks,
    peek_agent_output,
    terminate_agent_direct,
)
from src.autopilot.orchestrator.phase_transitions import (
    _retry_failed_tasks,
)
from src.core.constants import WORKTREES_SUBDIR
from src.core.database import (
    Feature,
    Workflow,
    get_db,
)
from src.core.repo_resolution import RepoNotFoundError, resolve_repo_path

if TYPE_CHECKING:
    from src.autopilot.orchestrator import OrchestratorLogger

logger = logging.getLogger(__name__)

# One lock per resolved repo path, mirroring phase_transitions.py's
# _advance_phases_locks pattern. _clean_stale_repo_state's own git
# subprocess sequence (merge --abort / checkout main / clean -fd / reset
# --hard) was never serialized against a concurrent call for the SAME
# path -- multiple workflows sharing one repo (the common case: every
# feature in a single-repo project resolves to the same primary
# ProjectRepo) could each independently decide the repo looked "stale"
# and race their own clean/reset sequences against each other, or against
# a different workflow's legitimate git_expert merge landing in the same
# window. Flagged (not fixed) by an earlier adversarial_review pass on
# this exact function -- see its own WARNING finding, "Two workflows
# sharing one repo can run destructive git recovery concurrently."
_clean_repo_locks: Dict[str, threading.Lock] = {}
_clean_repo_locks_guard = threading.Lock()


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

    A workflow with Workflow.paused_by set ("review", "user", "budget",
    "system", "system-exhausted") is likewise never abandoned, no matter
    how long it sits with zero agent/task activity -- that absence IS the
    correct, by-design state while parked waiting on a human (or the
    system's own separate retry-budget resume path), not evidence of lost
    progress. Observed live: a design paused_by="review" after Phase 0's
    feature_review task completed got auto-marked "failed" by the
    resume-attempt-exhaustion path in pipeline.py purely because several
    backend restarts elapsed before anyone clicked Resume in the UI --
    the report it had already written was orphaned, and the review modal
    read the now-failed workflow as having nothing to show.
    """
    try:
        with get_db() as db:
            wf = db.query(Workflow).filter_by(id=workflow_id).first()
            if wf and wf.paused_by:
                return False

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
        # Use derive_workflow_status instead of hand-rolling this check —
        # the "all tasks done ≠ all phases done" mistake has recurred
        # independently at least four times in this codebase's history.
        from src.core.status_derivation import derive_workflow_status
        with get_db() as db:
            derived = derive_workflow_status(db, workflow_id, write_back=False)
            if derived == "completed":
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


def _fail_tasks_with_terminated_agents(workflow_id: str, logger: "OrchestratorLogger") -> List[str]:
    """Clean stale "assigned" tasks whose agent is terminated.

    Includes "pending", not just "assigned"/"in_progress" -- a task can carry
    assigned_agent_id while still "pending" (see _clean_stale_assigned_tasks
    in features.py, which had the identical gap, and _advance_phases's own
    phase-scoped handling of this exact live-observed state in
    phase_transitions.py).
    """
    recovered: List[str] = []
    try:
        from src.core.database import Agent as _Agent
        from src.core.database import Task as _Task
        from src.core.database import get_db as _get_db

        with _get_db() as _db:
            assigned_tasks = (
                _db.query(_Task)
                .filter(
                    _Task.workflow_id == workflow_id,
                    _Task.status.in_(["pending", "queued", "blocked", "assigned", "in_progress"]),
                )
                .all()
            )
            for task in assigned_tasks:
                if task.assigned_agent_id:
                    agent = _db.query(_Agent).filter_by(id=task.assigned_agent_id).first()
                    if agent and agent.status == "terminated":
                        logger.info(f"  Task {task.id[:8]} assigned to terminated agent {task.assigned_agent_id[:8]} — marking failed")
                        task.status = "failed"
                        # Don't clobber a real reason. update_task_status'
                        # verification records exactly why a "done" claim was
                        # rejected (e.g. "required output(s) invalid: ...") on
                        # this same field before the agent's session ends, and
                        # _maybe_retry_failed_tasks feeds failure_reason into
                        # the next attempt's prompt -- overwriting it with the
                        # generic message below costs the retry the feedback it
                        # needs to fix anything. features.py's
                        # _clean_stale_assigned_tasks, which does this same job,
                        # already guards it this way; this copy had drifted.
                        if not task.failure_reason:
                            task.failure_reason = f"Agent {task.assigned_agent_id[:8]} terminated unexpectedly"
                        _db.commit()
                        recovered.append(f"cleaned stale task {task.id[:8]}")
    except Exception as e:
        logger.error(f"  Failed to clean stale assigned tasks: {e}")
    return recovered


def _resolve_recovery_project_path(workflow_id: str) -> Optional[str]:
    """The workflow's working directory, falling back to the workflow's own
    repo (via resolve_repo_path) rather than the single global $PROJECT_PATH
    env var (REQ-01). In a multi-repo project, $PROJECT_PATH points at
    whatever repo happens to be configured globally -- not necessarily the
    repo this workflow's feature actually belongs to, so blindly falling
    back to it risks running recovery's destructive git commands
    (_clean_stale_repo_state) against the wrong repo's working tree.

    repo_id is discovered via Workflow.feature_id -> Feature.repo_id; a
    workflow with no feature (design-phase workflows) or a feature with no
    repo_id resolves to the project's primary ProjectRepo, matching prior
    single-repo behavior unchanged (REQ-06).
    """
    try:
        with get_db() as _db:
            _wf = _db.query(Workflow).filter_by(id=workflow_id).first()
            if _wf and _wf.working_directory and Path(_wf.working_directory).exists():
                return _wf.working_directory
            if _wf and _wf.project_id:
                repo_id = None
                if _wf.feature_id:
                    feature = _db.query(Feature).filter_by(id=_wf.feature_id).first()
                    repo_id = feature.repo_id if feature else None
                try:
                    return str(resolve_repo_path(_db, _wf.project_id, repo_id))
                except RepoNotFoundError as e:
                    # repo_id was SET but dangling (stale/deleted ProjectRepo
                    # row) -- unlike the repo_id=None case, this is not "no
                    # scoping info available," it's a positive signal that the
                    # workflow's own recorded repo no longer resolves. Falling
                    # back to $PROJECT_PATH here would silently run recovery's
                    # destructive git commands against a repo the workflow may
                    # not even belong to -- the exact failure REQ-01 exists to
                    # prevent. Degrade to genuine no-op (NFR-03) instead: the
                    # caller (_clean_stale_repo_state) treats a falsy path as
                    # nothing to do.
                    logger.warning(f"[RECOVERY] repo_id={repo_id!r} for workflow {workflow_id[:8]} does not resolve: {e} -- skipping repo recovery rather than guessing a path")
                    return None
                except ValueError as e:
                    # Workflow.project_id itself doesn't resolve to any
                    # AutopilotProject row (deleted project, orphaned FK) --
                    # same reasoning as the RepoNotFoundError branch above:
                    # this is a positive signal the workflow's own scoping
                    # data is broken, not "nothing to go on," so degrade to
                    # no-op rather than guessing $PROJECT_PATH.
                    logger.warning(f"[RECOVERY] project_id={_wf.project_id!r} for workflow {workflow_id[:8]} does not resolve: {e} -- skipping repo recovery rather than guessing a path")
                    return None
    except Exception:
        logger.exception(f"[RECOVERY] failed to resolve recovery project path for workflow {workflow_id[:8]}")
    return os.getenv("PROJECT_PATH")


def _clean_stale_repo_state(workflow_id: str, logger: "OrchestratorLogger") -> List[str]:
    """Clear a wedged repo (dirty tree or half-finished merge) -- but ONLY
    inside an isolated .worktrees/ checkout, never the project's primary
    repo path.

    Deliberately does NOT merge branches -- WorktreeManager handles merges in
    update_task_status. A raw git merge here corrupts the repo, because this
    runs on the orchestrator's thread rather than in the agent's worktree
    context.

    The primary-checkout guard below exists because _resolve_recovery_
    project_path falls back to the project's primary ProjectRepo path
    whenever a workflow has no live working_directory (an orphaned/stale
    workflow, or one that hasn't been assigned a worktree yet) -- and that
    primary path is the SAME shared checkout git_expert legitimately merges
    into, a human might have uncommitted work sitting in (e.g. a design
    spec added via the dashboard's New Feature/Report Bug flow, which
    writes into a git-tracked folder under the primary repo, not a
    worktree), or an agent (including this one) might have uncommitted
    edits in mid-session. `git clean -fd`/`reset --hard HEAD` there doesn't
    distinguish "abandoned merge debris" from "someone's real, not-yet-
    committed work" -- confirmed live: an untracked docs/bugfix/*.md spec
    a user had just added was deleted this way while an unrelated
    workflow's merge/cleanup ran against the same primary checkout minutes
    later. Recovering a genuinely wedged PRIMARY checkout (as opposed to a
    worktree) needs a human or a narrower, path-aware strategy -- not this
    blanket sweep.
    """
    recovered: List[str] = []
    try:
        project_path = _resolve_recovery_project_path(workflow_id)
        if not project_path:
            # Nothing this strategy can do. Returning (rather than the
            # `return` out of attempt_recovery this used to be) is the point:
            # the strategies that follow don't need a project path and must
            # still run.
            return recovered

        resolved_path = Path(project_path).resolve()
        if WORKTREES_SUBDIR not in resolved_path.parts:
            logger.info(
                f"  [RECOVERY] {resolved_path} is not an isolated {WORKTREES_SUBDIR}/ "
                "checkout -- skipping destructive repo cleanup rather than risking "
                "someone else's uncommitted work in the shared primary checkout"
            )
            return recovered

        with _clean_repo_locks_guard:
            lock = _clean_repo_locks.setdefault(str(resolved_path), threading.Lock())
        if not lock.acquire(blocking=False):
            logger.info(
                f"  [RECOVERY] Repo cleanup for {resolved_path} already in progress "
                "elsewhere -- skipping concurrent call"
            )
            return recovered
        try:
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
        finally:
            lock.release()
    except Exception as e:
        logger.warning(f"  Failed to clean repo state: {e}")
    return recovered


def _terminate_dead_agents(workflow_id: str, logger: "OrchestratorLogger") -> List[str]:
    """Terminate only genuinely stale agents (dead tmux session).

    Never terminates an agent merely because it is "working". This runs on
    every recovery cycle (every POLL_INTERVAL) whenever
    is_design_fully_complete says the workflow isn't done -- which is the
    normal state for a workflow with real in-progress work, e.g. right after
    a restart reloads state.current_workflow_id into this branch.
    Terminating every "working" agent unconditionally here killed live,
    actively-progressing agents roughly once a minute until the workflow ran
    out of retries (observed live: a security_review agent got killed and
    replaced three times in six minutes, purely because this step never
    checked whether the agent was actually still alive).
    """
    recovered: List[str] = []
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
    return recovered


# Recovery strategies, run in order. Each is independent and best-effort:
# it swallows its own failures and reports what it managed to recover, so no
# strategy can suppress the ones after it. That independence is the fix for
# SOLID review 2.5 -- these used to share one function body, where the
# git-cleanup step's "can't resolve a project path" guard `return`ed out of
# the whole function and silently skipped stale-agent termination, which
# needs no project path at all.
_RECOVERY_STRATEGIES = (
    _retry_failed_tasks,
    _fail_tasks_with_terminated_agents,
    _clean_stale_repo_state,
    _terminate_dead_agents,
)


def attempt_recovery(workflow_id: str, logger: "OrchestratorLogger") -> Tuple[bool, str]:
    """Attempt to recover issues found by is_design_fully_complete.

    Runs every strategy in _RECOVERY_STRATEGIES and reports what was
    recovered.

    Returns:
        (success, message) tuple
    """
    recovered: List[str] = []
    for strategy in _RECOVERY_STRATEGIES:
        recovered.extend(strategy(workflow_id, logger))

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

    # Neither get_agents() nor get_tasks() has ever returned "error" or
    # "output_log" keys -- both checks below read those anyway, so every
    # comparison silently ran against "" and this function could never
    # return True in production, no matter how real the credit exhaustion
    # was (the exact scenario _retry_exhausted_paused_workflows's own
    # docstring names as its motivating example). The only place an
    # agent's actual output text lives is peek_agent_output (tmux), and a
    # task's real failure text is failure_reason, not "error".
    #
    # peek_agent_output is a real subprocess spawn (libtmux -> tmux CLI)
    # plus a DB query -- NOT a cheap dict read. This runs once per poll
    # tick (~15-20s) per active workflow's own polling loop, so it must
    # stay scoped to agents already flagged "error" (rare), not every
    # agent get_agents() returns (every active/idle agent, system-wide,
    # every tick). Confirmed live: broadening this to peek every agent
    # unconditionally made the backend's event loop intermittently
    # unresponsive to plain requests like /health within minutes of a
    # restart with several active workflows.
    agents = get_agents()
    for agent in agents:
        agent_status = (agent.get("status", "") or "").lower()
        if agent_status != "error":
            continue
        output = (peek_agent_output(agent.get("id", "")) or "").lower()

        for keyword in credit_keywords_in_error:
            if keyword in output:
                return (
                    True,
                    f"API credit issue in agent {agent.get('id', '')[:8]}: {keyword}",
                )
        for phrase in credit_phrases:
            if phrase in output:
                return True, f"API credit issue in agent {agent.get('id', '')[:8]}: {phrase}"

    failed_tasks = get_tasks(status="failed")
    for task in failed_tasks:
        error = (task.get("failure_reason", "") or "").lower()
        for phrase in credit_phrases:
            if phrase in error:
                return True, f"API credit issue in task {task.get('id', '')[:8]}: {phrase}"

    return False, ""


def detect_hard_error(agents: list, failed_tasks: list, workflow_id: str = None) -> Tuple[bool, str]:
    # Filter to only tasks from the current workflow if provided
    if workflow_id:
        failed_tasks = [t for t in failed_tasks if t.get("workflow_id") == workflow_id]

    # Check for crashed/errored agents (agents list is already scoped by get_agents)
    crashed_agents = [a for a in agents if a.get("status") == "error"]
    if crashed_agents:
        # current_task_id rides along for free (already on every agent
        # dict get_agents() returns) -- without it, whoever answers the
        # escalation has no way to tell which task/phase actually died
        # with the agent short of a manual DB lookup.
        descs = []
        for a in crashed_agents[:3]:
            desc = a.get("id", "unknown")[:20]
            task_id = a.get("current_task_id")
            if task_id:
                desc += f" (task {task_id[:8]})"
            descs.append(desc)
        return True, f"Crashed agents: {', '.join(descs)}"

    critical_failures = [t for t in failed_tasks if t.get("priority") == "critical" or "architectural" in (t.get("description", "") or "").lower()]
    if critical_failures:
        # failure_reason (why it actually failed) and the task/phase ids
        # ride along for free -- the truncated description alone couldn't
        # tell a human WHY it failed, only what it was supposed to do.
        descs = []
        for t in critical_failures[:3]:
            entry = f"{t.get('id', '?')[:8]}"
            if t.get("phase_id"):
                entry += f"/phase {t['phase_id'][:8]}"
            entry += f": {(t.get('description', '') or '')[:60]}"
            if t.get("failure_reason"):
                entry += f" -- {t['failure_reason'][:120]}"
            descs.append(entry)
        return True, f"Critical task failures: {descs}"

    return False, ""


def detect_impasse(agents: list, pending_tasks: list, in_progress_tasks: list, elapsed_seconds: int = 0) -> Tuple[bool, str]:
    """Detect if the workflow is stuck.

    Parent-child model: check if tasks are progressing, not health_check_failures.
    """
    active_agents = [a for a in agents if a.get("status") in ACTIVE_AGENT_STATUSES]

    # "No active agents but pending tasks" is deliberately NOT treated as an
    # impasse: it's the background phase-advancement sweep's own job to
    # notice a pending task with no agent and redispatch it (see
    # _mark_orphaned_and_stale_pending_tasks_failed / _retry_failed_tasks in
    # phase_transitions.py), and this human-escalation path had no way to
    # tell "the self-heal just hasn't gotten to it yet" apart from "it's
    # genuinely wedged" other than a fixed elapsed-time guess -- every
    # threshold tried here still produced false-positive human escalations
    # for cases the self-heal already recovers from on its own. A stuck
    # pending task with a real, unrecoverable cause (e.g. permanently
    # exhausted retries) surfaces instead via _retry_exhausted_paused_
    # workflows' terminal "system-exhausted" state, which does escalate.

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
                        # phase_id/assigned_agent_id ride along for free --
                        # both are already on every task dict get_tasks()
                        # returns. Bare task IDs forced whoever answered
                        # the escalation to go look up which phase/agent
                        # was actually involved before they could act;
                        # the caller (pipeline.py's escalation site) can
                        # resolve these further into names and a live
                        # output tail.
                        phase_id = task.get("phase_id")
                        agent_id = task.get("assigned_agent_id")
                        detail = f"Task {task.get('id', '?')[:8]} stuck for {int(elapsed)}s"
                        if phase_id:
                            detail += f" (phase {phase_id[:8]}"
                            detail += f", agent {agent_id[:8]})" if agent_id else ")"
                        elif agent_id:
                            detail += f" (agent {agent_id[:8]})"
                        return (True, detail)
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
