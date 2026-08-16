"""Control-loop engine: goto/retry/continue state machine, arbitration, phase-task creation."""

import json
import logging
import threading as _threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


from src.autopilot.spec import GATED_PHASES, build_phase_output
from src.core.constants import (
    CONTEXT_DIR_NAME,
    DIAGNOSTIC_TASK_PREFIX,
    GOTO_REASON_PREFIX,
)
from src.core.database import (
    Agent,
    DatabaseManager,
    Phase,
    PhaseExecution,
    Task,
    Workflow,
    get_db,
)
from src.core.simple_config import get_config
from src.phases import PhaseManager

from src.autopilot.orchestrator.engine_client import (
    create_agent_for_task_direct,
    get_tasks,
    increment_task_retry_count,
    update_task_status,
)
from src.autopilot.orchestrator.queue import (
    _assess_run_health,
)
from src.autopilot.orchestrator.worktree_integration import (
    _run_ash_scan,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.autopilot.orchestrator import OrchestratorLogger

logger = logging.getLogger(__name__)


POLL_INTERVAL = 15


CLAIM_STALE_TIMEOUT_SECONDS = 480  # 8 minutes -- must stay shorter than


_advance_phases_locks: Dict[str, "_threading.Lock"] = {}


_advance_phases_locks_guard = _threading.Lock()


def _try_advance_phases(workflow_id: str, call_logger: "OrchestratorLogger") -> bool:
    """Call _advance_phases for workflow_id, skipping (and logging) if
    another caller is already inside it for the same workflow. Both
    run_single_workflow's inline call and the background sweep's call
    must go through this, not _advance_phases directly.

    Uses the module-level `logger` for the skip notice, not call_logger --
    callers pass either the module logger (run_single_workflow) or an
    OrchestratorLogger instance (the sweep, which has no .debug method), so
    this can't assume call_logger supports every level.
    """
    with _advance_phases_locks_guard:
        lock = _advance_phases_locks.setdefault(workflow_id, _threading.Lock())
    if not lock.acquire(blocking=False):
        logger.debug(
            f"[ADVANCE-PHASES] Skipping concurrent call for workflow "
            f"{workflow_id[:8]} -- already in progress elsewhere"
        )
        return False
    try:
        return _advance_phases(workflow_id, call_logger)
    finally:
        lock.release()


def _retry_failed_tasks(workflow_id: str, logger: "OrchestratorLogger") -> List[str]:
    """Retry every failed task in a workflow directly, up to 2 attempts each.

    Extracted from attempt_recovery so this piece alone -- the only part
    that's safe to run unconditionally on every background sweep tick for
    every active workflow -- can be called on its own. attempt_recovery's
    OTHER actions (git reset --hard / clean -fd on any dirty repo, and
    terminating every currently-working agent) are appropriate as a rare,
    capped, last-resort action (see its caller: only after
    is_design_fully_complete fails, capped at 5 attempts, only for the one
    workflow a fresh pipeline run happens to resume) but would be
    destructive run every ~20s across every active workflow -- it would
    kill agents mid-task and blow away uncommitted work constantly.

    Returns the list of "retried task X" messages for callers that want to
    fold this into their own recovered-actions summary (attempt_recovery).
    """
    recovered = []
    failed = get_tasks(status="failed", workflow_id=workflow_id)
    for task in failed:
        task_id = task.get("id")
        phase_id = task.get("phase_id")

        # Never feed the human-only Git hand-off back into the agent retry
        # path while review mode actually requires one -- its intentional
        # dispatch rejection is a hand-off signal there, not an agent
        # failure. In full autopilot (review_mode off), git_commit_push
        # dispatches like any other phase, so a real failure there is a
        # real failure and should retry normally.
        try:
            with get_db() as phase_db:
                failed_phase = phase_db.query(Phase).filter_by(id=phase_id).first()
                if failed_phase and failed_phase.name in MANUAL_ONLY_PHASES and _manual_handoff_required(workflow_id):
                    logger.info(f"  Skipping retry for manual-only phase task {task_id[:8]}")
                    continue
        except Exception as exc:
            logger.debug(f"  Could not inspect phase for failed task {task_id[:8]}: {exc}")

        # Arbitration tasks carry a one-off custom prompt
        # (enriched_data["validation_prompt"], see _trigger_arbitration) that
        # this generic retry path has no way to reconstruct -- re-creating
        # one via create_agent_for_task_direct's default agent_type="phase"
        # would silently launch it with the wrong identity and instructions.
        # A failed arbitration task is instead picked up by
        # _maybe_resolve_arbitration as a "fail" outcome -- explicit and
        # visible, not silently retried into a broken prompt.
        if task.get("created_by_agent_id") == ARBITRATION_CREATED_BY:
            continue

        # Only retry if not retried too many times.
        # Orphaned tasks (never dispatched to an agent) are scheduling
        # issues, not agent failures -- they should retry indefinitely.
        retry_count = task.get("retry_count", 0)
        is_orphan = "Orphaned" in (task.get("failure_reason") or "")
        # Read max_task_retries from workflow config, default to 5
        try:
            from src.autopilot.spec import load_workflow_definition
            wf_def = load_workflow_definition(workflow_id)
            max_retry = wf_def.get("orchestrator", {}).get("max_task_retries", 5)
        except Exception:
            max_retry = 5
        if retry_count >= max_retry and not is_orphan:
            logger.info(
                f"  Task {task_id[:8]} failed {retry_count} times - skipping retry"
            )
            continue

        # Pre-check: does this phase already have an active task -- e.g. one
        # a goto/retry from _fire_phase_transition created independently
        # while this task was sitting failed? The dispatch-time check below
        # (inside create_agent_for_task_direct) already catches this, but
        # only AFTER burning a retry_count increment and resetting this task
        # to "pending", just to lose the race and land right back here as
        # "duplicated" -- wasted churn for an outcome already decidable up
        # front. Observed live: task ce152617 (architecture_design) went
        # through exactly this cycle against sibling 8b9d0368.
        # Scoped to phase_id being set -- Task.phase_id == None compiles to
        # "IS NULL" in SQLAlchemy, not a no-match, so an unguarded query
        # here would treat EVERY phase-less task in the database (ad-hoc
        # ones an agent created directly via create_task, e.g. an
        # adversarial re-review request) as a "sibling" of every other one,
        # regardless of workflow or how many months apart they were
        # created. Observed live: task 5b29d427 (a phase-less adversarial
        # re-review request) got marked "duplicated" of task a7430ccc -- an
        # unrelated debris task from five weeks earlier with no phase_id of
        # its own, matched only because both were NULL.
        _sibling_id = None
        if phase_id:
            with get_db() as _db_precheck:
                _sibling = (
                    _db_precheck.query(Task)
                    .filter(
                        Task.phase_id == phase_id,
                        Task.id != task_id,
                        Task.status.in_(["pending", "assigned", "in_progress", "queued"]),
                    )
                    .first()
                )
                _sibling_id = _sibling.id if _sibling else None
        if _sibling_id:
            logger.info(
                f"  Task {task_id[:8]} superseded by active task {_sibling_id[:8]} "
                "on the same phase -- marking duplicated without retrying"
            )
            with get_db() as _db_skip:
                _t_skip = _db_skip.query(Task).filter_by(id=task_id).first()
                if _t_skip and _t_skip.status == "failed":
                    _t_skip.status = "duplicated"
                    _t_skip.duplicate_of_task_id = _sibling_id
                    _t_skip.failure_reason = f"Superseded by task {_sibling_id[:8]}, which already owns this phase"
                    _db_skip.commit()
            continue

        logger.info(f"  Retrying failed task {task_id[:8]} (retry #{retry_count + 1})")
        # Persist the increment before attempting — counting only successful
        # attempts would let a task that fails every single retry (e.g. a
        # deleted worktree) loop forever, since retry_count would never
        # reach the >= 2 cutoff above.  Orphans don't increment since they
        # aren't real agent failures.
        if not is_orphan:
            increment_task_retry_count(task_id)
        try:
            # Reset task status to pending
            update_task_status(task_id, "pending")
            # Create agent for it
            agent_data = create_agent_for_task_direct(task_id, workflow_id, phase_id)
            if not agent_data:
                # create_agent_for_task_direct returns None for two
                # different reasons: a genuine creation failure, or its
                # own deliberate "another active task already owns this
                # phase" duplicate-dispatch guard. Only the former is a
                # real retry failure -- treating both the same way marks
                # a merely-superseded task "failed" with a misleading
                # "agent creation failed" reason, and burns through its
                # retry budget for a decision that was actually correct.
                # Observed live: a task reset to "pending" by a manual
                # recovery collided with a fresh task the pipeline had
                # already, legitimately created for the same phase in the
                # meantime -- 5 retries later it was permanently "failed"
                # with a reason that had nothing to do with what actually
                # happened.
                # Scoped to phase_id being set -- see the identical guard
                # (and its rationale) on this function's own pre-check
                # above; the same NULL-matches-NULL trap applies here too.
                sibling_id = None
                if phase_id:
                    with get_db() as _db_check:
                        sibling = (
                            _db_check.query(Task)
                            .filter(
                                Task.phase_id == phase_id,
                                Task.id != task_id,
                                Task.status.in_(["pending", "assigned", "in_progress", "queued"]),
                            )
                            .first()
                        )
                        sibling_id = sibling.id if sibling else None
                if sibling_id:
                    logger.info(
                        f"  Task {task_id[:8]} superseded by active task {sibling_id[:8]} "
                        "on the same phase -- marking duplicated, not failed"
                    )
                    with get_db() as _db_skip:
                        _t_skip = _db_skip.query(Task).filter_by(id=task_id).first()
                        if _t_skip and _t_skip.status == "pending":
                            # "duplicated" (not "skipped", which isn't a
                            # valid Task.status per the DB CHECK
                            # constraint) is the established status for
                            # exactly this case -- see
                            # task_similarity_service.py's identical use
                            # of status="duplicated" +
                            # duplicate_of_task_id for a debris task
                            # superseded by a sibling.
                            _t_skip.status = "duplicated"
                            _t_skip.duplicate_of_task_id = sibling_id
                            _t_skip.failure_reason = f"Superseded by task {sibling_id[:8]}, which already owns this phase"
                            _db_skip.commit()
                    continue
                raise RuntimeError("create_agent_for_task_direct returned no agent")
            agent_id = agent_data.get("agent_id", "unknown")
            logger.info(f"  Created agent {agent_id[:8]} for retried task")
            recovered.append(f"retried task {task_id[:8]}")
            # create_agent_for_task_direct does NOT update the task row
            # itself (same contract _create_phase_task's callers already
            # rely on -- it just creates the agent and returns its id).
            # Without this, a successful retry left the task "pending"
            # with assigned_agent_id still pointing at the OLD, now-dead
            # agent from the failed attempt, completely disconnected from
            # the real, live agent now actually working on it -- neither
            # _clean_stale_assigned_tasks (only watches "assigned"/
            # "in_progress") nor anything else could ever find it again,
            # and the task looked permanently stuck even while an agent
            # was actively burning tokens on it in the background. A
            # separate try -- the agent is already live at this point, so
            # a failure here must not be reported as a failed retry (the
            # outer except below assumes agent creation itself failed).
            try:
                with get_db() as _db4:
                    _t2 = _db4.query(Task).filter_by(id=task_id).first()
                    if _t2:
                        _t2.assigned_agent_id = agent_id
                        _t2.status = "in_progress"
                        _t2.started_at = datetime.utcnow()
                        _db4.commit()
            except Exception as e3:
                logger.error(f"  Agent {agent_id[:8]} created for task {task_id[:8]} but failed to link it to the task row: {e3}")
        except Exception as e:
            # Back to "failed" (not left "pending") so a later retry pass
            # -- this function, or _maybe_retry_failed_tasks -- gets
            # another chance up to the retry_count cap above. Leaving it
            # "pending" here would strand it: nothing dispatches an agent
            # for an already-existing pending task with no agent.
            logger.error(f"  Failed to retry task {task_id[:8]}: {e}")
            try:
                with get_db() as _db3:
                    _t = _db3.query(Task).filter_by(id=task_id).first()
                    if _t and _t.status == "pending":
                        _t.status = "failed"
                        _t.failure_reason = f"Retry agent creation failed: {e}"
                        _db3.commit()
            except Exception as e2:
                logger.error(f"  Failed to revert task {task_id[:8]} to failed: {e2}")
    return recovered


def _retry_exhausted_paused_workflows(logger: "OrchestratorLogger") -> int:
    """Self-heal for a workflow _maybe_retry_failed_tasks paused after its
    retry cap was exhausted (Workflow.paused_by == "system") -- e.g. every
    task in a phase failed the same way because an LLM provider account ran
    out of credits.

    Without this, such a workflow has no automated path back: _advance_phases
    only ever un-pauses via _try_auto_resume_paused_workflow, which requires
    a Task.status == "done" already sitting in the stalled phase -- a phase
    where literally every attempt (original + both retries) failed the same
    way will never produce one on its own, so the workflow stays paused
    forever, even after whatever broke it (e.g. the credits) gets fixed.

    Recovers by resetting retry_count to 0 on the stuck phase's failed tasks
    and flipping the workflow back to "active" -- deliberately not touching
    task.status/failure_reason itself, so _maybe_retry_failed_tasks' own
    already-tested reset-and-dispatch loop does that (and folds
    failure_reason into the next attempt's prompt) on the very next
    _advance_phases pass, instead of this function reimplementing it.

    Gated two ways so this can't degrade into the exact tight-retry-loop
    problem the retry cap exists to prevent:
    - A cooldown (paused_workflow_retry_cooldown_seconds) since the workflow
      was paused (Workflow.paused_at) -- NULL (rows paused before this
      column existed) is treated as immediately eligible, not skipped.
    - A hard cap on how many times a single workflow gets this second
      chance (paused_workflow_max_retry_cycles, tracked via
      Workflow.paused_retry_count). Once hit, this treats it like a genuine
      unrecoverable failure -- paused_by flips to "system-exhausted" (no
      longer matching this function's own "system" filter, so it's excluded
      from every future pass) and status_reason is updated to say so. A
      human has to look at it at that point, same as the credits scenario
      would if it turned out to actually be permanently broken code instead.
    """
    from src.autopilot.orchestrator import _get_paused_workflow_max_retry_cycles, _get_paused_workflow_retry_cooldown_seconds
    from sqlalchemy import or_

    max_cycles = _get_paused_workflow_max_retry_cycles()
    cutoff = datetime.utcnow() - timedelta(seconds=_get_paused_workflow_retry_cooldown_seconds())
    recovered = 0
    with get_db() as db:
        # Also pick up system-exhausted workflows that may have been manually
        # fixed or had their blocker resolved (e.g. a phase that was stuck now
        # has tasks done)
        candidates = (
            db.query(Workflow)
            .filter(
                Workflow.status == "paused",
                Workflow.paused_by.in_(["system", "system-exhausted"]),
                or_(Workflow.paused_at.is_(None), Workflow.paused_at < cutoff),
            )
            .all()
        )
        for wf in candidates:
            if wf.paused_by == "system" and wf.paused_retry_count >= max_cycles:
                logger.warning(f"[WORKFLOW-RECOVERY] Workflow {wf.id[:8]} exhausted {max_cycles} auto-retry cycles -- giving up permanently, needs a manual resume")
                wf.paused_by = "system-exhausted"
                wf.status_reason = f"{wf.status_reason or ''} (auto-retry gave up after {max_cycles} attempts -- manual resume required)"
                recovered += 1  # counts as "handled", not "retried"
                continue

            # For system-exhausted workflows, only retry if there are actually
            # failed or pending tasks in in_progress phases (conditions may have changed)
            if wf.paused_by == "system-exhausted":
                in_progress_phase_ids = {
                    pid for (pid,) in db.query(PhaseExecution.phase_id).join(Phase, PhaseExecution.phase_id == Phase.id).filter(Phase.workflow_id == wf.id, PhaseExecution.status == "in_progress").all()
                }
                # Check for failed OR pending tasks (pending with no agent = stuck)
                stuck_tasks = db.query(Task).filter(
                    Task.workflow_id == wf.id,
                    Task.phase_id.in_(in_progress_phase_ids),
                    Task.status.in_(["failed", "pending"]),
                ).all() if in_progress_phase_ids else []
                # Filter pending tasks to only those with no assigned agent (truly stuck)
                stuck_tasks = [t for t in stuck_tasks if t.status == "failed" or (t.status == "pending" and not t.assigned_agent_id)]
                if not stuck_tasks:
                    continue  # No stuck tasks to retry, leave as system-exhausted
                # Has stuck tasks — reset and retry
                for task in stuck_tasks:
                    task.retry_count = 0
                    if task.status == "pending":
                        task.failure_reason = None
                wf.status = "active"
                wf.paused_by = None
                wf.status_reason = None
                wf.paused_at = None
                wf.paused_retry_count = 0
                logger.warning(
                    f"[WORKFLOW-RECOVERY] Workflow {wf.id[:8]} was system-exhausted but has "
                    f"{len(stuck_tasks)} stuck task(s) in in_progress phase -- retrying"
                )
                recovered += 1
                continue

            # Scoped to the CURRENTLY in_progress phase only -- see the
            # identical reasoning in _recover_abandoned_workflows_missing_worktree.
            in_progress_phase_ids = {
                pid for (pid,) in db.query(PhaseExecution.phase_id).join(Phase, PhaseExecution.phase_id == Phase.id).filter(Phase.workflow_id == wf.id, PhaseExecution.status == "in_progress").all()
            }
            failed_tasks = db.query(Task).filter(Task.workflow_id == wf.id, Task.status == "failed", Task.phase_id.in_(in_progress_phase_ids)).all() if in_progress_phase_ids else []
            if not failed_tasks:
                continue

            for task in failed_tasks:
                task.retry_count = 0
            wf.status = "active"
            wf.paused_by = None
            wf.status_reason = None
            wf.paused_at = None
            wf.paused_retry_count = (wf.paused_retry_count or 0) + 1
            logger.warning(
                f"[WORKFLOW-RECOVERY] Workflow {wf.id[:8]} past its exhausted-"
                f"retry cooldown -- reset retry_count on {len(failed_tasks)} "
                f"failed task(s) (cycle {wf.paused_retry_count}/{max_cycles}) "
                "and resumed; _maybe_retry_failed_tasks picks it up on the "
                "next pass"
            )
            recovered += 1
        if recovered:
            db.commit()
    return recovered


def _advance_phases(workflow_id: str, logger: "OrchestratorLogger") -> bool:
    """Check for completed phases and advance to the next one.

    This is the single source of truth for phase progression. Called from
    the polling loop in run_single_workflow.

    Returns True if a phase was advanced, False otherwise.

    Phase Transition Cases (evaluated in priority order):
    - Case 0:  No in-progress, no completed, first pending phase exists -> start it
    - Case 0b: In-progress phase with no tasks -> create task for it
    - Case 1:  Completed phase with pending successor -> fire transition
    - Case 2:  In-progress phase that is now complete -> fire transition
    """
    try:
        with get_db() as db:
            # Get workflow
            wf = db.query(Workflow).filter_by(id=workflow_id).first()
            if not wf:
                return False

            # Mark pending/in_progress tasks as failed when workflow is failed
            if wf.status == "failed":
                orphaned_tasks = (
                    db.query(Task)
                    .filter(
                        Task.workflow_id == workflow_id,
                        Task.status.in_(["pending", "in_progress", "assigned"]),
                    )
                    .all()
                )
                for t in orphaned_tasks:
                    logger.warning(f"[PHASE-ADVANCE] Task {t.id[:8]} is {t.status} but workflow is failed — marking failed")
                    t.status = "failed"
                    t.failure_reason = f"Workflow failed: {wf.status_reason or 'unknown reason'}"
                if orphaned_tasks:
                    db.commit()
                return False

            if wf.status not in ("active", "paused"):
                return False

            # Auto-resume paused workflow if it has a done task in the stalled phase
            if wf.status == "paused":
                _try_auto_resume_paused_workflow(db, workflow_id, wf, logger)
                if wf.status == "paused":
                    return False  # Still paused, nothing to do

            # Self-heal any abandoned task-creation claim before reading
            # phase statuses below, so the dispatch that follows sees the
            # repaired state, not a claim-blocked snapshot.
            _release_stale_task_creation_claims(db, workflow_id, logger)
            # Same reasoning: a phase stuck "pending" despite a done task
            # is invisible to every dispatch case below otherwise.
            _release_pending_phases_with_done_tasks(db, workflow_id, logger)

            # Self-heal: tasks that are "done" but have a failure_reason
            # indicate gate validation failed after the task completed.
            # Reset these to "failed" so they can be properly retried
            # with correct gate evaluation.
            inconsistent_tasks = (
                db.query(Task)
                .filter(
                    Task.workflow_id == workflow_id,
                    Task.status == "done",
                    Task.failure_reason.isnot(None),
                    Task.failure_reason != "",
                )
                .all()
            )
            for t in inconsistent_tasks:
                logger.warning(f"[PHASE-ADVANCE] Task {t.id[:8]} is 'done' but has failure_reason — resetting to 'failed' for proper gate evaluation")
                t.status = "failed"
                t.completed_at = None
            if inconsistent_tasks:
                db.commit()

            # Get all phases and their statuses
            phase_statuses = _get_phase_statuses(db, workflow_id)

            completed = [p for p in phase_statuses if p["status"] == "completed"]
            pending = [p for p in phase_statuses if p["status"] == "pending"]
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]

            # Case 0: No in-progress phase and first phase is pending — start it
            result = _case_start_first_phase(db, workflow_id, pending, in_progress, completed, logger)
            if result is not None:
                return result

            # Case 0b: In-progress phase with no tasks at all
            result = _case_in_progress_no_tasks(db, workflow_id, in_progress, logger)
            if result is not None:
                return result

            # Case 1: Completed phase with pending successor
            result = _case_completed_with_successor(db, workflow_id, completed, pending, in_progress, logger)
            if result is not None:
                return result

            # Case 2: In-progress phase that is now complete
            result = _case_in_progress_complete(db, workflow_id, in_progress, logger)
            if result is not None:
                return result

    except Exception as e:
        logger.warning(f"[PHASE-ADVANCE] Error: {e}")
    return False


def _try_auto_resume_paused_workflow(db, workflow_id: str, wf, logger: "OrchestratorLogger") -> None:
    """Auto-resume paused workflow if it has a done task in the stalled phase.

    Skips workflows deliberately paused by a human or an explicit policy
    (wf.paused_by == "user", "budget", or "review"). Without this check, a
    deliberate pause could get silently reverted within one sweep tick
    (~20s) whenever the paused workflow's in-progress phase happens to have
    a done task sitting in it -- a state pausing itself commonly produces
    (the running task finishes right after being told to stop). Observed
    live: a user's pause click appeared to do nothing for a long time,
    because this function kept flipping the workflow back to "active"
    every cycle until whatever made the phase look stalled resolved on its
    own.

    "system" is deliberately let through, not skipped: it's the one
    paused_by value nothing but _maybe_retry_failed_tasks's exhausted-
    retry-cap give-up ever sets. Unlike the other three, it isn't an
    operator's intent -- it's a heuristic judgment ("this looked
    unrecoverable") that can be stale the instant it's made, exactly if a
    still-in-flight attempt (dispatched by a different code path than the
    one that paused) succeeds moments later. Observed live: a
    security_review task's final attempt (retry_count 5, its own retry
    path uncapped at 2) started at 04:46:56 and succeeded at 04:52:12, but
    _maybe_retry_failed_tasks paused the workflow at 04:47:16 based on the
    task's still-"failed" state from its prior attempt -- and because
    paused_by="system" was previously treated the same as "user"/"budget",
    nothing ever reconsidered the pause once the task actually finished.
    The phase's PhaseExecution sat "in_progress" and no qa_validation task
    was ever created, ~22 hours later. Before this function ever ran,
    every real pause site set paused_by to something non-None (user,
    budget, review, or system) -- so its "is not None" form made this
    entire self-heal a no-op for every actual pause, not just this one.
    """
    if wf.paused_by not in (None, "system"):
        return  # Respect any deliberate pause ("user", "budget", "review")
    phases = (
        db.query(Phase)
        .filter_by(workflow_id=workflow_id)
        .order_by(Phase.order)
        .all()
    )
    for phase in phases:
        exec = db.query(PhaseExecution).filter_by(phase_id=phase.id).first()
        if exec and exec.status == "in_progress":
            done_task = db.query(Task).filter_by(phase_id=phase.id, status="done").first()
            if done_task:
                logger.info(f"[PHASE-ADVANCE] Auto-resuming paused workflow — {phase.name} has done task {done_task.id[:8]}")
                wf.status = "active"
                wf.paused_by = None
                wf.status_reason = None
                db.commit()
                break


def _release_stale_task_creation_claims(db, workflow_id: str, logger: "OrchestratorLogger") -> None:
    """Self-heal for any PhaseExecution in this workflow whose
    task_creation_claimed_at claim has been held past
    CLAIM_STALE_TIMEOUT_SECONDS -- regardless of the phase's current
    status.

    Must run before _get_phase_statuses is read for this cycle's dispatch;
    it works phase-by-phase, in-progress or not, whereas
    _case_in_progress_complete's own claim check only ever sees phases
    already "in_progress" -- and a phase whose claim was never released
    also never had its status flipped to "in_progress" in the first place
    (that flip is itself part of releasing the claim). Without this, such
    a phase is invisible to every case in _advance_phases's dispatch, not
    just Case 2 -- no matter how many times its task actually finishes.
    Observed live: a phase's task completed successfully, but its
    PhaseExecution sat "pending" with a day-old claim indefinitely.

    Repairs each stale claim exactly as if its rightful holder had
    released it (_release_phase_task_creation_claim): if a Task already
    exists for the phase, treat the guarded work as done -- flip
    pending/completed to in_progress and clear the claim. If no Task
    exists at all, just clear the claim so Case 0/0b can create one fresh.

    Uses datetime.utcnow(), matching _claim_phase_task_creation's writer and
    every other timestamp in this codebase -- datetime.now() (ambient local
    time) here previously meant a claim's staleness depended on whatever
    TZ the process happened to be running under at the moment it compared,
    not real elapsed time. Observed live: a claim set hours earlier under a
    UTC-flavored clock never registered as stale against a later process's
    local-time now(), because the raw naive values didn't share a clock to
    compare against -- the workflow stayed silently stuck indefinitely,
    invisible to this self-heal despite being its exact intended case.
    """
    stale_cutoff = datetime.utcnow() - timedelta(seconds=CLAIM_STALE_TIMEOUT_SECONDS)
    stale_executions = (
        db.query(PhaseExecution)
        .join(Phase, PhaseExecution.phase_id == Phase.id)
        .filter(
            Phase.workflow_id == workflow_id,
            PhaseExecution.task_creation_claimed_at.isnot(None),
            PhaseExecution.task_creation_claimed_at < stale_cutoff,
        )
        .all()
    )
    for execution in stale_executions:
        phase = db.query(Phase).filter_by(id=execution.phase_id).first()
        latest_task = db.query(Task).filter_by(phase_id=execution.phase_id).order_by(Task.created_at.desc()).first()
        logger.warning(
            f"[PHASE-ADVANCE] {phase.name if phase else execution.phase_id}: task_creation_claimed_at held with no release -- clearing stale claim ({'task exists' if latest_task else 'no task yet'})"
        )
        if latest_task and execution.status in ("pending", "completed"):
            execution.status = "in_progress"
            # Backfill from the task that actually started this cycle, not
            # "now" -- _fire_phase_transition's done_count/incomplete
            # queries scope to Task.created_at >= started_at to ignore
            # older cycles' completions, so a "now" value here (this repair
            # can run long after the task was created) would wrongly
            # exclude that same task from its own cycle.
            execution.started_at = execution.started_at or latest_task.created_at
        execution.task_creation_claimed_at = None
    if stale_executions:
        db.commit()


def _release_pending_phases_with_done_tasks(db, workflow_id: str, logger: "OrchestratorLogger") -> None:
    """Self-heal for a PhaseExecution stuck at status="pending" despite
    already having a "done" Task -- a state none of _advance_phases's four
    dispatch cases recognize (Case 0/0b act on a *lack* of tasks, Case 1
    needs the *predecessor* completed, Case 2 only ever looks at phases
    already "in_progress"), so a phase in it is invisible to every one of
    them, forever, no matter how many times its task actually finishes.

    Several paths create/complete a task without re-flipping its phase to
    "in_progress" the way _create_phase_task does (e.g.
    _maybe_retry_failed_tasks's reset-and-redispatch loop never touches
    PhaseExecution at all), and the broader "reset ALL executions with
    order >= target back to pending" goto-reset can also revert a phase
    that's since moved on. Observed live: two workflows' phases sat
    "pending" with a done task for days, invisible to every self-heal
    path, while an unrelated workflow's endlessly-retried task (see
    _maybe_retry_failed_tasks's retry cap) hogged every poll cycle so this
    one's design queue turn never came around to notice.

    Repairs at most ONE phase per call -- the one whose done task is the
    most recent for the whole workflow (i.e. whatever it was actually
    working on right before getting stuck). A workflow with any real goto
    history has MANY pending phases each carrying SOME old done task from
    an earlier cycle -- that's normal, not stuck, and flipping every one
    of them to "in_progress" in one pass previously created several
    simultaneously-active phases for the same workflow (multiple agents
    burning tokens on unrelated phases at once, confirmed live). Also
    skips entirely if any phase is already "in_progress": a workflow
    legitimately doing something must never gain a second concurrent one.

    Must run before _get_phase_statuses is read for this cycle's dispatch,
    same as _release_stale_task_creation_claims.
    """
    already_active = db.query(PhaseExecution).join(Phase, PhaseExecution.phase_id == Phase.id).filter(Phase.workflow_id == workflow_id, PhaseExecution.status == "in_progress").first()
    if already_active:
        return

    most_recent_done_task = (
        db.query(Task)
        .join(Phase, Task.phase_id == Phase.id)
        .filter(
            Phase.workflow_id == workflow_id,
            Task.status == "done",
            # Same exclusion _case_in_progress_complete's own queries apply
            # a few lines below -- a diagnostic task (created by the
            # monitor against a stuck phase's phase_id, see
            # _create_diagnostic_agent) completing its investigation isn't
            # real phase progress and must not be mistaken for "what the
            # workflow was actually working on most recently."
            ~Task.raw_description.like(f"{DIAGNOSTIC_TASK_PREFIX}%"),
        )
        .order_by(Task.created_at.desc())
        .first()
    )
    if not most_recent_done_task:
        return

    execution = db.query(PhaseExecution).filter_by(phase_id=most_recent_done_task.phase_id).first()
    if not execution or execution.status != "pending":
        return

    phase = db.query(Phase).filter_by(id=execution.phase_id).first()
    logger.warning(
        f"[PHASE-ADVANCE] {phase.name if phase else execution.phase_id}: "
        f"PhaseExecution stuck 'pending' despite done task "
        f"{most_recent_done_task.id[:8]} -- flipping to in_progress so "
        "dispatch can see it"
    )
    execution.status = "in_progress"
    # Same rationale as _release_stale_task_creation_claims's backfill:
    # scope from the task that actually ran, not "now" (this repair can
    # run long after that task finished), or _fire_phase_transition's
    # done_count/incomplete queries would wrongly exclude that same task
    # from what they treat as its own cycle.
    execution.started_at = execution.started_at or most_recent_done_task.created_at
    db.commit()


def _get_phase_statuses(db, workflow_id: str) -> list:
    """Get all phases with their execution statuses."""
    phases = db.query(Phase).filter_by(workflow_id=workflow_id).order_by(Phase.order).all()

    phase_statuses = []
    for phase in phases:
        exec = db.query(PhaseExecution).filter_by(phase_id=phase.id).first()
        phase_statuses.append(
            {
                "phase": phase,
                "execution": exec,
                "status": exec.status if exec else "pending",
            }
        )
    return phase_statuses


def _claim_phase_task_creation(db, phase_id: str) -> bool:
    """Atomically claim the right to create a phase's first task.

    Two independent code paths can decide "this phase needs its first task":
    server.py's synchronous /start_workflow_execution step (fires the moment
    a workflow launches) and the orchestrator's own background self-heal
    (_case_start_first_phase / _case_in_progress_no_tasks, polling for
    in-progress phases with no tasks). A plain `Task.count() == 0` check --
    even with a short sleep-and-retry -- is a race: both sides can observe
    zero tasks and both create one. Observed live: a duplicate task+agent
    got spawned for the same phase, burning a full agent run on work the
    first task had already completed.

    This closes the race by construction instead of by timing: a single
    UPDATE ... WHERE task_creation_claimed_at IS NULL can only succeed for
    one caller no matter how the two paths interleave, because SQLite
    serializes writes to the same row. Returns True if this call won the
    claim (go ahead and create the task), False if someone else already
    holds it (skip -- a task is already being created for this phase).
    """
    claimed_at = datetime.utcnow()
    result = (
        db.query(PhaseExecution)
        .filter(
            PhaseExecution.phase_id == phase_id,
            PhaseExecution.task_creation_claimed_at.is_(None),
        )
        .update({"task_creation_claimed_at": claimed_at}, synchronize_session=False)
    )
    db.commit()
    return result > 0


def _release_phase_task_creation_claim(db, phase_id: str) -> None:
    """Release a claim taken by _claim_phase_task_creation, once the task
    it was guarding actually exists -- mirrors what _create_phase_task
    already does for every phase after the first (see its own claim-release
    comment). Also flips PhaseExecution.status to "in_progress" if it's
    still "pending"/"completed", since server.py's synchronous
    /start_workflow_execution step creates phase 1's task via the generic
    /create_task handler, which has no knowledge of this bookkeeping at all
    (unlike _create_phase_task).

    Without this, the claim stays held forever: _case_in_progress_complete
    reuses task_creation_claimed_at as a guard against evaluating a phase
    transition while another caller is mid-creation, so a permanently-held
    claim silently blocks phase 1 from ever being recognized as complete --
    no matter how many times its task actually finishes. Observed live:
    phase 1's task completed successfully but the pipeline never advanced
    to phase 2, indefinitely, for every UI-launched workflow.

    populate_existing() matters here, not just style: this project's
    sessions run with expire_on_commit=False (StaticPool convention), and
    _claim_phase_task_creation's own claiming UPDATE uses
    synchronize_session=False -- so if this PhaseExecution was already
    loaded into the session's identity map before the claim was taken
    (e.g. via _get_phase_statuses, which every caller of this function
    reads first), a plain query returns that same stale in-memory object
    instead of a fresh one. Its task_creation_claimed_at attribute would
    still show the pre-claim value; setting it to None here would be a
    no-op write SQLAlchemy doesn't even consider dirty, silently leaving
    the claim held in the database forever. Found by
    test_maybe_retry_failed_tasks_is_claim_protected.

    started_at is anchored to the guarded task's own created_at, NOT
    datetime.utcnow() -- this function always runs strictly after that
    task was already committed by the caller (server.py's create_task,
    which can spend real seconds on enrichment/embedding/dedup/capacity-
    queue checks before returning), so "now" is always later than the
    task's created_at, sometimes by several seconds. _case_in_progress_
    complete later uses execution.started_at as cycle_start and filters
    tasks with `Task.created_at >= cycle_start` to decide whether the
    phase has any tasks in its current cycle -- stamping "now" here made
    that filter exclude the very task this function exists to release the
    claim for, so the phase looked like it had zero in-cycle tasks and
    self-heal spawned a duplicate for it. Observed live: a UI-launched
    workflow's phase 1 task completed successfully while a duplicate
    self-heal task sat pending beside it, created ~15s after the first.
    """
    execution = db.query(PhaseExecution).filter_by(phase_id=phase_id).populate_existing().first()
    if not execution:
        return
    if execution.status in ("pending", "completed"):
        execution.status = "in_progress"
        earliest_task = (
            db.query(Task)
            .filter_by(phase_id=phase_id)
            .order_by(Task.created_at.asc())
            .first()
        )
        execution.started_at = earliest_task.created_at if earliest_task else datetime.utcnow()
    execution.task_creation_claimed_at = None
    db.commit()


def _case_start_first_phase(db, workflow_id: str, pending: list, in_progress: list, completed: list, logger: "OrchestratorLogger") -> Optional[bool]:
    """Case 0: No in-progress phase and first phase is pending — start it.

    Returns None if this case doesn't apply, True/False otherwise.
    """
    if not in_progress and not completed and pending:
        first_phase = min(pending, key=lambda p: p["phase"].order)
        # Check if it already has tasks
        existing = db.query(Task).filter_by(phase_id=first_phase["phase"].id).count()
        if existing == 0 and not _claim_phase_task_creation(db, first_phase["phase"].id):
            # Someone else (or a previous iteration of this same loop) is
            # already creating this phase's first task -- don't duplicate it.
            existing = 1
        if existing == 0:
            logger.info(f"[PHASE-ADVANCE] Starting first phase: {first_phase['phase'].name}")
            return _create_phase_task(
                workflow_id,
                first_phase["phase"].id,
                first_phase["phase"].name,
                "continue",
                logger,
                target_already_claimed=True,
            )
    return None


def _case_in_progress_no_tasks(db, workflow_id: str, in_progress: list, logger: "OrchestratorLogger") -> Optional[bool]:
    """Case 0b: In-progress phase with no tasks at all.

    Workflow engine set it but didn't create task.
    Returns None if this case doesn't apply, True/False otherwise.
    """
    for ps in in_progress:
        phase = ps["phase"]
        task_count = db.query(Task).filter_by(phase_id=phase.id).count()
        if task_count == 0 and not _claim_phase_task_creation(db, phase.id):
            # Same race as _case_start_first_phase: other paths (e.g. the
            # spec-gate immediate-fire path in task_completion_service.py,
            # or /start_workflow_execution's synchronous initial-task step)
            # can set a phase to in_progress and create its task while this
            # background poll checks independently. The claim above is the
            # actual fix -- it's atomic regardless of how long the other
            # path takes to finish creating its task, unlike a fixed sleep.
            task_count = 1
        if task_count == 0:
            logger.info(f"[PHASE-ADVANCE] Phase {phase.name} is in_progress but has no tasks — creating one")
            return _create_phase_task(
                workflow_id,
                phase.id,
                phase.name,
                "continue",
                logger,
                target_already_claimed=True,
            )
    return None


def _case_completed_with_successor(db, workflow_id: str, completed: list, pending: list, in_progress: list, logger: "OrchestratorLogger") -> Optional[bool]:
    """Case 1: Completed phase with pending successor.

    Phase N done, next never started.
    Returns None if this case doesn't apply, True/False otherwise.
    """
    if completed and pending and not in_progress:
        completed.sort(key=lambda p: p["phase"].order)
        last_completed = completed[-1]

        # If the phase that just completed recorded an explicit goto/retry
        # target, honor that instead of blindly picking the lowest-order
        # pending phase. A goto's own stale-reset resets EVERY phase at or
        # after ITS target back to "pending" -- including ones the
        # completing phase's own goto deliberately skips over. E.g.
        # development, after fixing adversarial_review's BLOCKERs, goto's
        # straight back to adversarial_review (its action_target_phase,
        # set when development's own corrective task was created) --
        # bypassing architectural_review on purpose, since nothing
        # architectural changed. But architectural_review is still sitting
        # "pending" from the earlier, broader reset when adversarial_review
        # first sent things back to development. Blindly picking "next
        # pending phase by order" finds architectural_review and dispatches
        # a fresh, redundant run of it -- burning real agent/LLM cycles on
        # a review that was never supposed to happen again this loop.
        # Observed live: every adversarial_review-fix cycle re-triggered a
        # full architectural_review pass in between.
        last_task = (
            db.query(Task)
            .filter(
                Task.phase_id == last_completed["phase"].id,
                Task.status == "done",
                ~Task.raw_description.like(f"{DIAGNOSTIC_TASK_PREFIX}%"),
            )
            .order_by(Task.completed_at.desc())
            .first()
        )
        successor = None
        successor_action = "continue"
        if last_task and last_task.action in ("goto", "retry") and last_task.action_target_phase:
            successor = next(
                (p for p in pending if p["phase"].name == last_task.action_target_phase),
                None,
            )
            if successor:
                successor_action = last_task.action

        if successor is None:
            # Find the next pending phase by order (handles non-sequential orders)
            successor = min(
                (p for p in pending if p["phase"].order > last_completed["phase"].order),
                key=lambda p: p["phase"].order,
                default=None,
            )
        if successor:
            # Check if successor already has tasks from the CURRENT cycle
            # (transition already fired this time around). Unscoped, an old
            # task from a PRIOR cycle -- e.g. this phase succeeded weeks
            # ago, then got reset back to "pending" by a later goto for a
            # fresh pass -- looks identical to "transition already fired"
            # and permanently blocks a fresh dispatch, even though the
            # phase's own PhaseExecution has sat "pending" ever since with
            # zero tasks from ITS current cycle. Observed live:
            # product_validation stalled 2+ days this way, its only task a
            # 'done' row from three weeks earlier -- every poll saw
            # existing_tasks > 0 and silently backed off forever.
            last_completed_execution = last_completed.get("execution")
            cycle_filter = (
                (Task.created_at >= last_completed_execution.completed_at,)
                if last_completed_execution and last_completed_execution.completed_at
                else ()
            )
            existing_tasks = db.query(Task).filter(Task.phase_id == successor["phase"].id, *cycle_filter).count()
            # This case only fires when last_completed's PhaseExecution.status
            # is ALREADY "completed" (that's what put it in the `completed`
            # list). Re-running the transition via _fire_phase_transition ->
            # mark_phase_complete on that same phase_id therefore always hit
            # mark_phase_complete's own idempotency guard (execution.status ==
            # "completed") and returned "already_completed" -- a permanent
            # no-op. The one real scenario this case exists for -- the process
            # crashing between mark_phase_complete's _close_execution commit
            # (goto/continue decision, marks last_completed done) and
            # _create_phase_task's Task-row insert for the successor -- could
            # never actually recover: every future poll repeated the same
            # no-op forever, leaving the workflow permanently stalled with a
            # completed phase, a pending successor, and zero tasks. The
            # decision to advance to `successor` was already made; call
            # _create_phase_task directly instead of re-deciding it.
            if existing_tasks == 0 and not _claim_phase_task_creation(db, successor["phase"].id):
                existing_tasks = 1
            if existing_tasks > 0:
                return False  # Already fired (or someone else just claimed it)

            logger.info(f"[PHASE-ADVANCE] {last_completed['phase'].name} completed, advancing to {successor['phase'].name}")
            return _create_phase_task(
                workflow_id,
                successor["phase"].id,
                successor["phase"].name,
                successor_action,
                logger,
                feedback=(last_task.completion_notes if successor_action != "continue" and last_task else None),
                source_phase_name=(last_completed["phase"].name if successor_action != "continue" else None),
                target_already_claimed=True,
            )
    return None


MANUAL_ONLY_PHASES = {"git_commit_push"}


def _manual_handoff_required(workflow_id: str) -> bool:
    """Whether MANUAL_ONLY_PHASES should actually block autonomous dispatch
    for this workflow's project.

    Full autopilot (the default: AutopilotProject.review_mode is False)
    keeps git_commit_push exactly as autonomous as every other phase -- a
    real agent commits, pushes, and opens the PR, same as before this
    gate existed. review_mode=True is what actually asks for a human in
    the loop, so that's the only case this reuses _should_pause_for_review
    for: same project-level toggle, same paused_by='review' convention,
    one flag rather than a second, independent "is this phase manual"
    concept a project would have to separately configure.
    """
    from src.autopilot.orchestrator import _should_pause_for_review
    from src.core.database import resolve_project_for_workflow

    project_id, _ = resolve_project_for_workflow(workflow_id)
    return bool(project_id) and _should_pause_for_review(project_id)


def _pause_for_manual_handoff(db, workflow_id: str, phase_name: str, logger: "OrchestratorLogger") -> None:
    """Park the workflow for an operator instead of treating a manual-only
    phase's failed tasks as ordinary agent failures.

    Only called once _manual_handoff_required has already confirmed this
    project is in review mode. AgentManager.create_agent_for_task raises
    PermissionError for any phase in MANUAL_ONLY_PHASES under the same
    condition (the pipeline must never commit, push, or merge on its own
    while a human is meant to be reviewing) -- create_agent_for_task_direct
    converts that into a normal "dispatch failed" None return, so without
    this, both the per-task retry sweep and the phase-completion retry path
    would treat the intentional rejection as an agent failure and retry
    forever, starving the design queue. Idempotent: a no-op past the first
    call for a given pause (checked here so every caller can call this
    unconditionally without re-querying wf.status itself).
    """
    wf = db.query(Workflow).filter_by(id=workflow_id).first()
    if wf and wf.status != "paused":
        wf.status = "paused"
        wf.paused_by = "review"
        wf.status_reason = f"{phase_name} is manual-only; human approval is required"
        wf.paused_at = datetime.utcnow()
        db.commit()
    logger.info(f"[PHASE-ADVANCE] {phase_name} is manual-only; pausing for human hand-off")


def _case_in_progress_complete(db, workflow_id: str, in_progress: list, logger: "OrchestratorLogger") -> Optional[bool]:
    """Case 2: In-progress phase that is now complete.

    Returns None if this case doesn't apply, True/False otherwise.
    """
    for ps in in_progress:
        phase = ps["phase"]

        # A held task_creation_claimed_at means this phase is owned
        # elsewhere right now -- most importantly, mid-arbitration (see
        # _trigger_arbitration/_maybe_resolve_arbitration, which hold the
        # claim for the arbitration task's entire lifetime). Skip the whole
        # per-phase body, not just the later "fire transition" step: a
        # FAILED arbitration task would otherwise reach
        # _maybe_retry_failed_tasks below and get re-dispatched through the
        # generic retry path, losing its arbitration-specific prompt (same
        # class of bug already fixed for _retry_failed_tasks's sweep-level
        # retry). _maybe_resolve_arbitration is the only thing that should
        # ever touch a claimed phase's failed/done arbitration task.
        #
        # A genuinely stale claim (no releaser left) is repaired earlier in
        # _advance_phases by _release_stale_task_creation_claims, which runs
        # workflow-wide before phase_statuses is even read -- it has to run
        # there, not here, because a phase whose claim was never released
        # also never had its status flipped to "in_progress" (that flip is
        # itself part of releasing the claim), so it wouldn't be in this
        # `in_progress` list at all. By the time this loop runs, any claim
        # still held is a genuinely live one.
        execution = ps.get("execution")
        if execution and execution.task_creation_claimed_at is not None:
            continue

        # Check if all tasks are done. DIAGNOSTIC tasks (created by the
        # monitor itself when a workflow looks stuck -- see
        # _create_diagnostic_agent) are deliberately excluded, matching the
        # same convention _check_workflow_stuck_state already applies ("they
        # should not block completion detection"). Without this, an
        # orphaned diagnostic task left "pending" after its agent died
        # (e.g. terminated by a restart before it could close its own task)
        # counts as real incomplete work forever -- permanently blocking
        # this phase from ever being recognized as complete, even though
        # the actual phase task finished successfully. Observed live: a
        # phase sat in_progress for 9+ hours with its real task done,
        # solely because a leftover diagnostic task from an earlier,
        # unrelated incident was still "pending" in the same phase.
        # Orphaned-pending staleness check: a task sitting at status="pending"
        # with no assigned_agent_id for more than a minute has no legitimate
        # in-flight explanation -- dispatch normally happens synchronously
        # right after a task is created (see _create_phase_task,
        # restart_task_endpoint). Without this, such a task counts toward
        # "incomplete" below forever, which short-circuits this whole
        # function (`continue`) before ever reaching _maybe_retry_failed_tasks
        # -- so a task orphaned this way (e.g. the backend killed mid-dispatch)
        # was invisible to every self-heal path, not just this one, since
        # _create_phase_task's own orphaned-task recovery only fires when a
        # phase needs its *first* task created, never for an already
        # in_progress phase re-checking a stale existing one. Marking it
        # failed here lets it both drop out of the incomplete count and
        # become eligible for the all-failed retry path right below.
        # A phase revisited via goto reuses the same phase_id across cycles
        # -- every query below must be scoped to tasks from THIS cycle
        # (execution.started_at, reset on each goto/retry) or a done_count
        # from a cycle that succeeded hours ago makes a currently-failed
        # re-attempt look like "phase complete" the moment it stops
        # counting as incomplete, firing the transition against whatever
        # (nothing, usually) the current attempt actually left on disk.
        # Observed live: a gated phase's second pass produced a false
        # "no <phase>_result.json found" goto while its own fresh task was
        # sitting "failed" mid-retry, entirely because an earlier cycle's
        # real completion still counted toward done_count. Falls back to
        # unscoped (the prior behavior) if started_at was never set.
        cycle_start = execution.started_at if execution else None
        cycle_filter = (Task.created_at >= cycle_start,) if cycle_start else ()

        orphan_cutoff = datetime.utcnow() - timedelta(minutes=1)
        stale_pending_candidates = (
            db.query(Task)
            .filter(
                Task.phase_id == phase.id,
                Task.status == "pending",
                Task.created_at < orphan_cutoff,
                ~Task.raw_description.like(f"{DIAGNOSTIC_TASK_PREFIX}%"),
                *cycle_filter,
            )
            .all()
        )
        # A stale pending task is orphaned either way: never dispatched
        # (assigned_agent_id NULL), or dispatched to an agent that died
        # since (killed mid-launch by a backend restart, or manually
        # terminated as stuck-agent cleanup) before ever flipping the task
        # to in_progress. assigned_agent_id being non-null used to be
        # enough to treat this as "still being worked" forever -- this is
        # the actual gate the periodic sweep uses (unlike _create_phase_
        # task's own orphan check, which only ever gets reached once a
        # phase has zero tasks or all-failed tasks; a lone "pending" task
        # here short-circuits every case before that check is ever hit).
        # Observed live: a security_review task sat "pending", pointing at
        # an agent terminated hours earlier, and never self-healed.
        orphaned_pending = []
        for t in stale_pending_candidates:
            if not t.assigned_agent_id:
                orphaned_pending.append((t, "never dispatched to an agent"))
                continue
            agent = db.query(Agent).filter_by(id=t.assigned_agent_id).first()
            if agent is None or agent.status not in ("working", "idle", "starting"):
                orphaned_pending.append(
                    (t, f"assigned agent {t.assigned_agent_id[:8]} is no longer active")
                )
        for orphan, reason in orphaned_pending:
            logger.info(f"[PHASE-ADVANCE] {phase.name} has an orphaned pending task {orphan.id[:8]} ({reason}, stale >1min) -- marking failed so it becomes eligible for retry")
            orphan.status = "failed"
            orphan.failure_reason = f"Orphaned: {reason}"
        if orphaned_pending:
            db.commit()

        # Also check for pending tasks with terminated agents (regardless of age)
        terminated_pending = (
            db.query(Task)
            .filter(
                Task.phase_id == phase.id,
                Task.status == "pending",
                Task.assigned_agent_id.isnot(None),
                ~Task.raw_description.like(f"{DIAGNOSTIC_TASK_PREFIX}%"),
                *cycle_filter,
            )
            .all()
        )
        terminated_tasks = []
        for t in terminated_pending:
            agent = db.query(Agent).filter_by(id=t.assigned_agent_id).first()
            if agent and agent.status == "terminated":
                terminated_tasks.append(t)
        for t in terminated_tasks:
            logger.warning(f"[PHASE-ADVANCE] {phase.name} has pending task {t.id[:8]} with terminated agent -- marking failed")
            t.status = "failed"
            t.failure_reason = "Agent terminated"
            t.assigned_agent_id = None
        if terminated_tasks:
            db.commit()

        # Mark pending tasks with retry_count past cap as failed
        # These are stuck in pending state but have been retried too many times
        try:
            from src.autopilot.spec import load_workflow_definition
            _wf_def = load_workflow_definition(phase.workflow_id)
            _max_retry = _wf_def.get("orchestrator", {}).get("max_task_retries", 5)
        except Exception:
            _max_retry = 5
        stale_retry_tasks = (
            db.query(Task)
            .filter(
                Task.phase_id == phase.id,
                Task.status == "pending",
                Task.retry_count >= _max_retry,
                ~Task.raw_description.like(f"{DIAGNOSTIC_TASK_PREFIX}%"),
                *cycle_filter,
            )
            .all()
        )
        for t in stale_retry_tasks:
            logger.warning(f"[PHASE-ADVANCE] {phase.name} has pending task {t.id[:8]} with retry_count={t.retry_count} (>= {_max_retry}) -- marking failed")
            t.status = "failed"
            t.failure_reason = t.failure_reason or "Exceeded retry cap"
        if stale_retry_tasks:
            db.commit()

        incomplete = (
            db.query(Task)
            .filter(
                Task.phase_id == phase.id,
                Task.status.in_(["pending", "assigned", "in_progress"]),
                ~Task.raw_description.like(f"{DIAGNOSTIC_TASK_PREFIX}%"),
                *cycle_filter,
            )
            .count()
        )
        if incomplete > 0:
            continue  # Still has active tasks

        done_count = (
            db.query(Task)
            .filter(
                Task.phase_id == phase.id,
                Task.status == "done",
                *cycle_filter,
            )
            .count()
        )
        if done_count == 0:
            # A phase can be "in_progress" with a cycle_start that predates
            # every task actually tied to it -- e.g. execution.started_at
            # surviving stale across a goto reset (the phases whose order
            # was rewound get status="pending" but kept their old
            # started_at, until the goto-reset fix). incomplete and
            # done_count above are both cycle-scoped, so they're 0 whether
            # every real task simply predates cycle_start (nothing to
            # retry, nothing to wait on -- an invisible, permanently stuck
            # phase) or a genuinely fresh in_progress phase has no tasks
            # yet at all. _maybe_retry_failed_tasks only handles "existing
            # cycle-scoped tasks are all failed" -- it silently no-ops for
            # either case above, exactly like Case 0b's unscoped check
            # would if it ran here (it doesn't fire either: its own
            # unscoped count still sees the phase's pre-cycle task and
            # concludes nothing needs creating). Treat a genuinely empty
            # cycle the same as Case 0b: dispatch a fresh task.
            total_cycle_tasks = db.query(Task).filter(Task.phase_id == phase.id, *cycle_filter).count()
            if total_cycle_tasks == 0:
                if not _claim_phase_task_creation(db, phase.id):
                    continue
                logger.warning(f"[PHASE-ADVANCE] {phase.name} is in_progress but has no tasks within its own cycle (stale started_at?) — creating a fresh one")
                return _create_phase_task(workflow_id, phase.id, phase.name, "continue", logger, target_already_claimed=True)

            # Check if ALL tasks are failed — retry them. Same claim
            # protection as the _fire_phase_transition path below, for the
            # identical reason its own comment documents: nothing stops a
            # concurrent poll (this same orchestrator's next cycle, or
            # monitor.py's separate stuck-check) from re-entering this
            # branch while a first call's retry dispatch (a real
            # create_agent_for_task_direct call, not instantaneous) is
            # still in flight, creating two agents for the same failed
            # task. That fix was only ever applied to the sibling path.
            if not _claim_phase_task_creation(db, phase.id):
                continue
            try:
                result = _maybe_retry_failed_tasks(db, phase, logger, cycle_start=cycle_start)
            finally:
                # Phase is already "in_progress" here (this whole function
                # only iterates that bucket), so this only clears the
                # claim -- its status-flip side effect is a no-op.
                _release_phase_task_creation_claim(db, phase.id)
            if result is not None:
                return result
            continue  # No completed tasks yet

        # Before marking phase as complete, check if there are failed tasks
        # that should be retried. A phase with done tasks AND failed tasks
        # is NOT complete — the failed tasks need retry first.
        failed_count = (
            db.query(Task)
            .filter(
                Task.phase_id == phase.id,
                Task.status == "failed",
                ~Task.raw_description.like(f"{DIAGNOSTIC_TASK_PREFIX}%"),
                *cycle_filter,
            )
            .count()
        )
        if failed_count > 0:
            if phase.name in MANUAL_ONLY_PHASES and _manual_handoff_required(workflow_id):
                _pause_for_manual_handoff(db, workflow_id, phase.name, logger)
                continue
            # Has failed tasks — try to retry them before marking complete
            if not _claim_phase_task_creation(db, phase.id):
                continue
            try:
                # _maybe_retry_failed_tasks only retries when ALL tasks are failed.
                # When we have done + failed, we need to retry the failed ones directly.
                failed_tasks = (
                    db.query(Task)
                    .filter(
                        Task.phase_id == phase.id,
                        Task.status == "failed",
                        ~Task.raw_description.like(f"{DIAGNOSTIC_TASK_PREFIX}%"),
                        *cycle_filter,
                    )
                    .all()
                )
                # Filter to retryable tasks (orphaned, session limits, and stuck tasks are always retryable)
                # Read max_task_retries from workflow config, default to 5
                try:
                    from src.autopilot.spec import load_workflow_definition
                    wf_def = load_workflow_definition(phase.workflow_id)
                    max_retry_count = wf_def.get("orchestrator", {}).get("max_task_retries", 5)
                except Exception:
                    max_retry_count = 5
                _limit_failure = lambda r: "session limit" in (r or "").lower() or "spend limit" in (r or "").lower()
                _stuck_failure = lambda r: "task stuck" in (r or "").lower()
                retryable_tasks = [
                    t for t in failed_tasks
                    if (t.retry_count or 0) < max_retry_count
                    or "Orphaned" in (t.failure_reason or "")
                    or _limit_failure(t.failure_reason)
                    or _stuck_failure(t.failure_reason)
                ]
                if retryable_tasks:
                    logger.info(f"[PHASE-ADVANCE] {phase.name} has {done_count} done but {len(retryable_tasks)} failed tasks to retry")
                    for task in retryable_tasks:
                        if task.failure_reason:
                            # Use raw_description as base to avoid accumulating retry messages
                            base = task.raw_description or ""
                            task.enriched_description = f"{base}\n\n--- RETRY: your previous attempt failed ---\n{task.failure_reason}"
                        task.status = "pending"
                        task.failure_reason = None
                        task.retry_count = (task.retry_count or 0) + 1
                    db.commit()
                    # Dispatch agents for the retried tasks
                    for task in retryable_tasks:
                        try:
                            agent_data = create_agent_for_task_direct(
                                task.id, phase.workflow_id, phase.id
                            )
                            if agent_data:
                                task.assigned_agent_id = agent_data.get("agent_id")
                                task.status = "in_progress"
                                task.started_at = datetime.utcnow()
                        except Exception as e:
                            logger.error(f"[PHASE-ADVANCE] Failed to dispatch retry agent for task {task.id[:8]}: {e}")
                    db.commit()
                    return True
                else:
                    # All failed tasks past retry cap. Bug: this used to
                    # only set execution.status = "failed" and fall
                    # straight through into the "phase complete, fire
                    # transition" section below -- _fire_phase_transition
                    # calls PhaseManager.mark_phase_complete, which
                    # evaluates the engine decision from the failed task's
                    # own stale action/completion data (e.g. "continue",
                    # written by the agent's own self-report before the
                    # output validator rejected it), NOT from
                    # execution.status. Observed live: architectural_review
                    # exhausted its retry cap on a real frontmatter-schema
                    # defect and the pipeline advanced straight to
                    # qa_validation as if the review had passed. Mirror
                    # _trigger_arbitration's own exhausted-retry-budget
                    # handling (wf.status = "failed" + status_reason, then
                    # stop) instead of silently continuing.
                    logger.warning(f"[PHASE-ADVANCE] {phase.name} has {failed_count} failed tasks all past retry cap — marking phase and workflow as failed")
                    if execution:
                        execution.status = "failed"
                        execution.completed_at = datetime.utcnow()
                    wf = db.query(Workflow).filter_by(id=workflow_id).first()
                    if wf and wf.status != "failed":
                        wf.status = "failed"
                        wf.status_reason = (
                            f"{phase.name}: {failed_count} task(s) exhausted the retry cap "
                            "without producing a valid output"
                        )
                    db.commit()
                    continue
            finally:
                _release_phase_task_creation_claim(db, phase.id)

        # Phase is complete — fire transition. mark_phase_complete's engine
        # evaluation can take minutes (an LLM call in phase_manager.py), and
        # nothing previously stopped a concurrent poll (this same
        # orchestrator's next cycle, or monitor.py's separate
        # _check_workflow_stuck_state process examining the same workflow)
        # from re-entering this exact branch while the first evaluation was
        # still in flight -- "all tasks done, 0 active" stays true the
        # whole time, since the phase's completed task doesn't disappear
        # and no new one exists yet. Observed live: a second, orphaned task
        # + agent got created for an already-completed qa_validation phase
        # a minute into the first evaluation; by the time that first
        # evaluation's "goto -> development" decision landed and the
        # pipeline moved on, the second agent was left running against a
        # phase the pipeline had already abandoned, confusedly trying to
        # manually create the next phase's task on its own.
        #
        # Reuses the same claim _create_phase_task's callers already use --
        # this closes the analogous "two things decide to act on the same
        # phase" race for the evaluate-and-transition path, not just the
        # create-the-first-task path.
        if not _claim_phase_task_creation(db, phase.id):
            logger.info(f"[PHASE-ADVANCE] {phase.name} transition already being evaluated by another caller — skipping")
            continue

        logger.info(f"[PHASE-ADVANCE] {phase.name} appears complete ({done_count} tasks done, 0 active), evaluating transition")
        # Extract primitives before session closes to avoid DetachedInstanceError
        phase_id = phase.id
        phase_name = phase.name
        try:
            return _fire_phase_transition(workflow_id, phase_id, phase_name, logger)
        finally:
            # The claim above only needed to guard AGAINST a concurrent
            # re-entry DURING evaluation -- once _fire_phase_transition
            # returns (however it went), that's done, and the claim must
            # not outlive it. Left set, it becomes a permanently stale
            # non-null value on a now-"completed" phase's row forever (only
            # _start_next_phase's explicit clear-on-reopen ever touched it
            # again, and only IF the phase gets normally reopened).
            # Observed live: _trigger_arbitration's exhaustion path tried
            # to claim this exact phase later and read the leftover stale
            # claim as "arbitration already in flight", silently refusing
            # to ever arbitrate it -- worse than the original bug, since
            # there wasn't even a pause to notice.
            #
            # Bypass update (synchronize_session=False), not load-then-
            # mutate-then-commit: this project runs with
            # expire_on_commit=False (see DatabaseManager), so `phase`
            # (loaded earlier in this same session, before
            # _claim_phase_task_creation's own bypass update) is a stale
            # cached object -- re-querying by phase_id returns that SAME
            # cached instance from the identity map, already showing
            # task_creation_claimed_at as whatever it was at load time.
            # Setting an in-memory attribute back to a value it already
            # appears to hold produces no dirty column for SQLAlchemy to
            # write, so the commit was a silent no-op in testing.
            db.query(PhaseExecution).filter_by(phase_id=phase_id).update({"task_creation_claimed_at": None}, synchronize_session=False)
            db.commit()
    return None


def _maybe_retry_failed_tasks(db, phase, logger: "OrchestratorLogger", cycle_start: Optional[datetime] = None) -> Optional[bool]:
    """Retry all failed tasks in a phase if all tasks are failed.

    cycle_start: scopes both counts to the current PhaseExecution cycle
    (its started_at, reset on each goto/retry) -- a phase revisited via
    goto reuses the same phase_id, so an unscoped total_count includes
    every task from every earlier cycle too. A phase that succeeded once
    and is now failing on a later re-attempt would otherwise never satisfy
    failed_count == total_count (the old "done" task keeps counting
    forever), so this retry path would silently never fire for it.

    Returns None if no retry was needed, True if tasks were reset for retry.
    """
    # Git hand-off is human-only in review mode -- see _pause_for_manual_
    # handoff's own docstring for why that case can't be treated as an
    # ordinary agent failure. In full autopilot this phase retries like any
    # other.
    if phase.name in MANUAL_ONLY_PHASES and _manual_handoff_required(phase.workflow_id):
        _pause_for_manual_handoff(db, phase.workflow_id, phase.name, logger)
        return None

    cycle_filter = (Task.created_at >= cycle_start,) if cycle_start else ()
    failed_count = db.query(Task).filter(Task.phase_id == phase.id, Task.status == "failed", *cycle_filter).count()
    total_count = db.query(Task).filter(Task.phase_id == phase.id, *cycle_filter).count()
    if failed_count > 0 and failed_count == total_count:
        # Same retry_count cap _retry_failed_tasks already enforces (that
        # function's own comment names this one as sharing it, but it
        # never actually checked it) -- without this, a task whose failure
        # is permanent (e.g. a deleted git worktree, which raises
        # instantly with no LLM call in between) gets reset and
        # re-dispatched every single poll cycle forever, burning a cycle
        # every few seconds indefinitely and starving every other
        # workflow's turn in the same poll loop. Observed live.
        # Read max_task_retries from workflow config, default to 5
        try:
            from src.autopilot.spec import load_workflow_definition
            wf_def = load_workflow_definition(phase.workflow_id)
            max_retry_count = wf_def.get("orchestrator", {}).get("max_task_retries", 5)
        except Exception:
            max_retry_count = 5
        failed_tasks = (
            db.query(Task)
            .filter(Task.phase_id == phase.id, Task.status == "failed", *cycle_filter)
            .all()
        )
        # Orphaned tasks (never dispatched), session/spend limit failures,
        # and stuck-task failures are not agent faults -- they should always
        # be retryable. Session limit failures will use the fallback model on retry.
        _limit_failure = lambda r: "session limit" in (r or "").lower() or "spend limit" in (r or "").lower()
        _stuck_failure = lambda r: "task stuck" in (r or "").lower()
        retryable_tasks = [
            t for t in failed_tasks
            if (t.retry_count or 0) < max_retry_count
            or "Orphaned" in (t.failure_reason or "")
            or _limit_failure(t.failure_reason)
            or _stuck_failure(t.failure_reason)
        ]
        if not retryable_tasks:
            reasons = sorted({t.failure_reason for t in failed_tasks if t.failure_reason})
            reason_text = "; ".join(reasons) if reasons else "no reason recorded"
            logger.warning(
                f"[PHASE-ADVANCE] Phase {phase.name} has {len(failed_tasks)} failed "
                f"task(s), all past the retry cap ({max_retry_count})"
            )
            # Check if there are still pending tasks in other phases —
            # don't pause the workflow if there's still work to do.
            other_pending = (
                db.query(Task)
                .filter(
                    Task.workflow_id == phase.workflow_id,
                    Task.phase_id != phase.id,
                    Task.status == "pending",
                )
                .count()
            )
            if other_pending > 0:
                logger.info(
                    f"[PHASE-ADVANCE] {phase.name} exhausted retries but {other_pending} "
                    f"pending tasks remain in other phases — not pausing workflow"
                )
                return None
            workflow = db.query(Workflow).filter_by(id=phase.workflow_id).first()
            if workflow and workflow.status != "paused":
                workflow.status = "paused"
                workflow.paused_by = "system"
                workflow.status_reason = f"{phase.name}: exhausted retries -- {reason_text}"
                workflow.paused_at = datetime.utcnow()
                db.commit()
            return None

        logger.info(f"[PHASE-ADVANCE] Phase {phase.name} has {failed_count} failed tasks and 0 done — retrying {len(retryable_tasks)} (of {len(failed_tasks)}, cap {max_retry_count})")
        # Reset retryable failed tasks to pending. Per-task (not a bulk
        # .update()) so each one's own failure_reason -- e.g. a specific
        # "missing output artifact: X" from update_task_status's validation
        # gate, or a real error preserved by _clean_stale_assigned_tasks --
        # gets folded into what the next agent actually reads
        # (enriched_description) before being cleared. A blind reset here
        # previously threw the reason away entirely, so the retried agent
        # got the same generic phase description and no idea what to fix.
        reset_task_ids = []
        for task in retryable_tasks:
            if task.failure_reason:
                # Use raw_description as base to avoid accumulating retry messages
                base = task.raw_description or ""
                task.enriched_description = f"{base}\n\n--- RETRY: your previous attempt failed with this specific error, fix it rather than repeating the same mistake ---\n{task.failure_reason}"
            task.status = "pending"
            task.failure_reason = None
            # Persist the increment before attempting -- counting only
            # successful dispatches would let a task that fails on every
            # single retry (the exact scenario this cap exists for) never
            # reach max_retry_count at all.
            task.retry_count = (task.retry_count or 0) + 1
            # Deliberately NOT clearing task.action/action_target_phase here.
            # This row is reused (not recreated) for the retry, but a task
            # that's "failed" (never reached "done") can only have gotten
            # those fields from _create_phase_task's CREATION-time tagging
            # (see its action_target_phase= assignment) -- the field means
            # "I exist because an earlier phase goto'd/retried back to me,
            # and _start_next_phase should resume AT that target once I'm
            # done." _tag_completing_task, the only other writer, tags a
            # task only AFTER it completes and gets evaluated -- a failed
            # task never reached that point, so there is no stale post-
            # completion badge to clear here. Previously this cleared both
            # fields unconditionally, silently discarding that resume
            # target on every retry -- observed live: a development task
            # that goto'd back from qa_validation got stuck (CLI session
            # limit) and retried here, losing action_target_phase=
            # "qa_validation" entirely, so its eventual completion fell back
            # to next-phase-by-order and re-ran the entire architectural_
            # review -> adversarial_review -> security_review chain from
            # scratch even though none of it had been invalidated.
            reset_task_ids.append(task.id)
        db.commit()

        # Resetting status to "pending" alone doesn't get an agent -- nothing
        # else in _advance_phases picks up a task that already exists (all
        # four cases key off task COUNT or "all done", not "pending task with
        # no agent"), and this reset bypasses the queue (no enqueue_task
        # call), so a task retried this way was previously an unrecoverable
        # dead end: reset to pending and never touched again by any live
        # code path. Observed live: a Feature Architect task sat "pending"
        # indefinitely after its one real attempt failed (an unrelated
        # generate_agent_prompt signature bug), because this reset was the
        # only thing that ever ran for it. Dispatch a fresh agent directly,
        # mirroring _create_phase_task's own create-then-update pattern.
        for task_id in reset_task_ids:
            # Check if this task failed due to session/CLI limit -- if so,
            # resolve the fallback CLI/model for the retry so we don't
            # just hit the same limit again.
            session_limit_override_cli = None
            session_limit_override_model = None
            with get_db() as check_db:
                check_task = check_db.query(Task).filter_by(id=task_id).first()
                if check_task and ("session limit" in (check_task.failure_reason or "").lower() or "spend limit" in (check_task.failure_reason or "").lower()):
                    if phase.id:
                        _phase = check_db.query(Phase).filter_by(id=phase.id).first()
                        if _phase:
                            session_limit_override_cli = getattr(_phase, 'fallback_cli_tool', None)
                            session_limit_override_model = getattr(_phase, 'fallback_cli_model', None)
                    if not session_limit_override_cli:
                        cfg = get_config()
                        if cfg.default_fallback_cli_tool:
                            session_limit_override_cli = cfg.default_fallback_cli_tool
                            session_limit_override_model = cfg.default_fallback_cli_model
                    if session_limit_override_cli:
                        logger.info(
                            f"[PHASE-ADVANCE] Task {task_id[:8]} failed due to session limit -- "
                            f"retrying with fallback {session_limit_override_cli}/{session_limit_override_model or 'default'}"
                        )

            agent_data = create_agent_for_task_direct(
                task_id, phase.workflow_id, phase.id,
                phase_cli_tool_override=session_limit_override_cli,
                phase_cli_model_override=session_limit_override_model,
            )
            with get_db() as retry_db:
                retry_task = retry_db.query(Task).filter_by(id=task_id).first()
                if not retry_task:
                    continue
                if not agent_data or not isinstance(agent_data, dict) or "agent_id" not in agent_data:
                    # Back to "failed" (not left "pending") so the next poll's
                    # _maybe_retry_failed_tasks (which only triggers on
                    # status="failed") gets another chance at this -- leaving
                    # it "pending" here would recreate the exact dead end this
                    # fix closes: no case in _advance_phases dispatches an
                    # agent for an already-existing pending task.
                    retry_task.status = "failed"
                    retry_task.failure_reason = "Retry agent creation failed"
                    retry_db.commit()
                    logger.warning(f"[PHASE-ADVANCE] Retry agent creation failed for task {task_id[:8]} in {phase.name} -- marked failed for another retry pass")
                    continue
                retry_task.assigned_agent_id = agent_data.get("agent_id", "unknown")
                retry_task.status = "in_progress"
                retry_task.started_at = datetime.utcnow()
                retry_db.commit()
        return True
    return None


def _fire_phase_transition(
    workflow_id: str,
    phase_id: str,
    phase_name: str,
    logger: "OrchestratorLogger",
    force_continue: bool = False,
) -> bool:
    """Fire the phase transition: mark complete, evaluate, create next task/agent.

    force_continue: skip the normal orchestrator evaluation entirely and
    force a "continue" (force_action="continue" on mark_phase_complete).
    _cap_out_review_phase's whole point is "this phase already exhausted
    its retry budget -- stop re-reviewing, treat it as clean, move on" --
    but a normal (non-forced) call here re-runs orchestrator.evaluate(),
    whose OWN retry-count check (WorkflowOrchestrator.evaluate, checked
    BEFORE the score) short-circuits straight to "arbitrate" the instant
    phase_retry_counts[phase_name] >= eval_point.max_retries, without ever
    reading the synthetic clean score _cap_out_review_phase just wrote.
    Since max_retries and max_review_runs are commonly configured to the
    same threshold (e.g. design_review: both 4), by the time the review-run
    cap trips, the retry-count cap has always already tripped too --
    arbitration then decides "goto" right back to the phase that fed this
    one, which redoes real work, completes, and immediately re-hits this
    exact same cap again. Observed live: design_review capped out at 4
    runs, arbitrated back to architecture_design, which re-ran (real,
    substantive work each time) and tried to hand off to design_review
    again -- hitting the same permanently-tripped cap every time, in a loop
    that ran for hours with design_review never actually re-reviewed again.
    """
    try:
        # Build phase output for gated phases -- skipped entirely for a
        # forced continue, which doesn't read it (_handle_force_continue
        # takes no phase_output) and shouldn't pay for computing it.
        phase_output = {}
        if not force_continue and phase_name in GATED_PHASES:
            with get_db() as db:
                wf = db.query(Workflow).filter_by(id=workflow_id).first()
                # Path is already imported at module level -- a redundant
                # local "from pathlib import Path" here previously made
                # Python treat Path as local to this whole function, so the
                # EARLIER use on this same line raised UnboundLocalError
                # every time, silently caught by this function's own
                # try/except and logged as "[PHASE-ADVANCE] Transition
                # error" -- which meant a gated phase (scope_review,
                # architecture_design, etc.) could never advance past
                # completion, forever, since the exception fired before
                # mark_phase_complete ever got called.
                if wf and wf.working_directory and Path(wf.working_directory).exists():
                    phase_output = build_phase_output(
                        phase_name, Path(wf.working_directory),
                        skip_independent_verification=True,
                    )

        # Mark phase complete and get engine decision
        from src.core.database import DatabaseManager

        pm = PhaseManager(DatabaseManager(None))
        pm.workflow_id = workflow_id
        result = (
            pm.mark_phase_complete(phase_id, "Phase completed", force_action="continue")
            if force_continue
            else pm.mark_phase_complete(
                phase_id,
                "Phase completed",
                phase_output=phase_output,
            )
        )

        action = result.get("action", "continue")
        target_phase_id = result.get("target_phase_id")
        target_phase_name = result.get("target_phase")

        logger.info(f"[PHASE-ADVANCE] Engine decision for {phase_name}: {action}" + (f" -> {target_phase_name}" if target_phase_name else ""))

        if action == "already_completed":
            # Phase was already advanced by another caller (spec gate, etc.)
            # Don't create a duplicate task.
            return False

        if action == "arbitrate":
            logger.warning(f"[PHASE-ADVANCE] Arbitration needed for {phase_name}")
            reason = result.get("reason") or f"{phase_name} exhausted its retry budget"
            _trigger_arbitration(workflow_id, target_phase_id, phase_name, reason, logger)
            return True

        if not target_phase_id:
            # Workflow complete or no next phase
            return True

        # For goto/retry, prefer the gate's own specific finding (e.g.
        # "6 BLOCKER(s) found — returning to development" from
        # score_adversarial_review) over the static workflow.yaml condition
        # reason ("Runtime failure modes found, returning to development to
        # fix") -- the gate's reason has real counts, the static one is
        # boilerplate repeated for every gate on that phase regardless of
        # what actually triggered it.
        feedback = None
        if action in ("goto", "retry"):
            metadata = result.get("metadata") or {}
            spec_gate = metadata.get("spec_gate", {})
            feedback = spec_gate.get("reason") or result.get("reason") or None

            # A "result_missing" gate reason ("no <phase>_report.md
            # found") only means build_phase_output's file read came up
            # empty right at this evaluation instant -- it says nothing
            # about whether the agent that just finished this phase
            # actually did the work. If it wrote a real completion_notes
            # summary, that's a strictly more accurate account of what
            # happened than a generic "missing" message, and the next
            # phase's corrective task should see THAT, not a reason that
            # contradicts the real work already done (observed live: a
            # developer task was told "WHY YOU'RE HERE: no
            # adversarial.md found" while the adversarial
            # review that sent it there had, per its own completion_notes,
            # found and reported 3 concrete BLOCKERs -- the agent had to
            # rediscover them itself instead of being told directly).
            if spec_gate.get("result_missing"):
                with get_db() as db:
                    completing_task = db.query(Task).filter(Task.phase_id == phase_id, Task.status == "done").order_by(Task.completed_at.desc()).first()
                if completing_task and completing_task.completion_notes:
                    feedback = completing_task.completion_notes

        # Create task and agent for the next phase
        return _create_phase_task(
            workflow_id,
            target_phase_id,
            target_phase_name,
            action,
            logger,
            feedback=feedback,
            source_phase_name=phase_name,
        )

    except Exception as e:
        logger.warning(f"[PHASE-ADVANCE] Transition error: {e}")
        return False


ARBITRATION_CREATED_BY = "arbitration"


def _gather_arbitration_context(phase_id: str, phase_name: str) -> str:
    """Plain-text summary of why this phase is stuck: its own recent
    attempt history, each carrying the "WHY YOU'RE HERE" reason
    _create_phase_task embedded in that attempt's task description."""
    with get_db() as db:
        recent_tasks = db.query(Task).filter(Task.phase_id == phase_id).order_by(Task.created_at.desc()).limit(6).all()
        lines = [f"Phase: {phase_name}", ""]
        if not recent_tasks:
            lines.append("No task history found for this phase.")
        for t in reversed(recent_tasks):
            lines.append(f"- [{t.created_at.isoformat() if t.created_at else '?'}] action={t.action or 'initial'} status={t.status}")
            if t.raw_description:
                lines.append(f"  {t.raw_description.strip()[:500]}")
            if t.failure_reason:
                lines.append(f"  failure_reason: {t.failure_reason}")
            if t.completion_notes:
                lines.append(f"  completion_notes: {str(t.completion_notes)[:300]}")
    return "\n".join(lines)


def _build_arbitration_prompt(
    phase_id: str,
    phase_name: str,
    reason: str,
    working_directory: Optional[str],
    valid_phase_names: Optional[list] = None,
) -> str:
    context = _gather_arbitration_context(phase_id, phase_name)
    phase_list_text = ", ".join(valid_phase_names) if valid_phase_names else "(could not be determined -- use the exact name from RECENT HISTORY above)"
    return f"""=== ARBITRATION TASK ===

The autopilot pipeline's phase "{phase_name}" has exhausted its automatic
retry/goto budget. Why: {reason}

Your job is ONLY to decide what happens next -- you are not the one who
fixes anything. Do NOT edit, write, or delete any project files, and do
NOT run commands that change repository state (a read-only investigation
via read/grep/bash-for-inspection is fine). If a fix is needed, that is
what a "goto" decision is for: it dispatches a fresh agent to make the
fix, with your specific instructions. Making the fix yourself here skips
that agent's own review/test cycle for the change.

The pipeline acts on your decision immediately -- it is NOT waiting for a
human, so be decisive.

RECENT HISTORY FOR THIS PHASE:
{context}

Working directory: {working_directory or "(unknown)"}

VALID PHASE NAMES (target_phase, if you choose "goto", MUST be exactly
one of these -- copy it verbatim, do not paraphrase, abbreviate, or
change case): {phase_list_text}

WHAT TO DO:
1. Read whatever evidence is relevant -- the latest gate output file(s) in
   ./.hephaestus/ (e.g. qa.md, adversarial.md,
   security.md -- whichever exist for this workflow; each starts
   with a YAML frontmatter block giving its structured verdict/counts,
   followed by the full narrative report), and the phase's own recent
   deliverables, to understand exactly what's blocking progress.
2. Decide ONE of:
   - "continue": the blocker is not a real defect worth another cycle --
     e.g. a single pre-existing/unrelated/flaky test failure, a cosmetic
     gate violation, or something already effectively resolved. Proceeding
     is safe.
   - "goto": one more attempt is warranted, but the automatic retries
     clearly weren't converging -- give a SPECIFIC, narrow instruction
     naming the exact file/test/issue to fix, not a repeat of the vague
     reason that already failed multiple times. You are explicitly allowed
     to instruct fixing pre-existing or seemingly-unrelated failures (e.g.
     a stale test assertion) if that's what's actually blocking the gate --
     "not my feature's fault" is not a reason to leave a required gate
     failing forever.
   - "fail": only if this is genuinely unrecoverable by any code change
     (e.g. a missing external credential, a fundamentally contradictory
     requirement) -- explain exactly why in your reason so a human reading
     the workflow's status later understands immediately, with no further
     digging required.
3. Write your decision to ./{CONTEXT_DIR_NAME}/arbitration_result.json:
   {{
     "decision": "continue" | "goto" | "fail",
     "target_phase": "<one of the VALID PHASE NAMES above, only if decision is goto, else null>",
     "reason": "<specific, actionable, one paragraph>"
   }}
4. Call hephaestus_update_task_status(status="done") once written. If you
   cannot complete this analysis, call it with status="failed" and a
   failure_reason -- a failed arbitration is treated as a "fail" decision,
   so an explicit reason there is still far more useful than none.
"""


def _phase_currently_passes(
    workflow_id: str,
    phase_name: str,
    working_directory: str,
    logger: "OrchestratorLogger",
) -> Tuple[bool, str]:
    """Whether phase_name's CURRENT on-disk output already scores as
    passing, evaluated fresh against the workflow's real eval_point
    conditions -- bypassing WorkflowOrchestrator.evaluate()'s retry-count
    gate (checked before any score is even read), which is exactly what
    makes evaluate() itself unusable for this: a phase whose retry/
    arbitration budget is exhausted always short-circuits straight to
    "arbitrate" there, regardless of what its actual output says.

    Used only by _trigger_arbitration's cap-exhausted fallback, once
    there's no pending arbitration decision left to resolve, to
    distinguish "genuinely still broken, a human should look" from "a
    later redo cycle already fixed this, but the loop never got to
    re-check." Returns (False, ...) for anything that isn't a clean,
    confident "continue" -- a non-gated phase, a missing orchestrator
    config, a non-heuristic evaluator, or (the common case) a missing/
    still-failing artifact, e.g. this phase's own gate-result file having
    been deleted by consume_gate_artifacts after its last real run and
    never regenerated since. Never skips evaluation the way the
    max_review_runs bug (cb60308) did -- it only ever advances on a
    genuine fresh passing score.
    """
    if phase_name not in GATED_PHASES:
        return False, "not a gated phase"
    if not working_directory or not Path(working_directory).exists():
        return False, "no working directory"

    try:
        phase_output = build_phase_output(
            phase_name, Path(working_directory), skip_independent_verification=True
        )

        pm = PhaseManager(DatabaseManager(None))
        session = pm.db_manager.get_session()
        try:
            orchestrator = pm._get_orchestrator(session, workflow_id)
            if not orchestrator:
                return False, "no orchestrator config"
            eval_point = orchestrator._find_evaluation_point(phase_name)
            if not eval_point:
                return False, "no evaluation point"
            if eval_point.evaluator != "heuristic":
                return False, f"non-heuristic evaluator ({eval_point.evaluator}), can't safely re-score outside a real run"
            score, metadata = orchestrator._heuristic_evaluate(phase_name, phase_output, eval_point.conditions)
            action = orchestrator._evaluate_conditions(eval_point.conditions, score, metadata, phase_output)
            return action.action.value == "continue", f"score={score}, {action.reason}"
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"[ARBITRATE] {phase_name}: fresh pass-check failed ({e}) -- treating as not passing")
        return False, f"pass-check error: {e}"


def _trigger_arbitration(
    workflow_id: str,
    phase_id: str,
    phase_name: str,
    reason: str,
    logger: "OrchestratorLogger",
) -> bool:
    """Spawn a one-shot arbitration agent for a stuck phase, unless one is
    already in flight (idempotent via the same task_creation_claimed_at
    claim _create_phase_task uses -- see _claim_phase_task_creation).

    Hard-capped at MAX_ARBITRATIONS_PER_PHASE: a "goto" decision's task
    counts toward the SAME MAX_PHASE_ATTEMPTS budget as a normal retry
    (both go through _create_phase_task), so a persistently-confused
    arbiter that keeps choosing "goto" back into a phase that keeps
    re-exhausting could otherwise cycle forever -- 5 real attempts,
    arbitrate, goto, 5 more attempts, arbitrate again... "never pause for
    a human" doesn't mean "never terminate": an unbounded loop still
    silently burns cost/tokens forever with nobody aware. Past the cap,
    fail immediately instead of spawning yet another arbitration agent.
    """
    import uuid

    with get_db() as db:
        max_arbitrations_per_phase = 3
        # Only count arbitrations since the workflow's last on-demand Retry
        # (Workflow.gotos_reset_at) -- historical arbitration Task rows are
        # never deleted, so counting all-time would mean a workflow that
        # already exhausted this cap once stays permanently unrecoverable
        # via Retry, even after total_gotos itself was reset to give the
        # phase a genuinely fresh budget (see _resume_interrupted_workflows'
        # reactivate branch, which sets gotos_reset_at). NULL (never
        # retried) preserves the original all-time count.
        wf_for_cutoff = db.query(Workflow).filter_by(id=workflow_id).first()
        gotos_reset_at = wf_for_cutoff.gotos_reset_at if wf_for_cutoff else None
        prior_arbitrations_query = db.query(Task).filter(
            Task.phase_id == phase_id,
            Task.created_by_agent_id == ARBITRATION_CREATED_BY,
        )
        if gotos_reset_at:
            prior_arbitrations_query = prior_arbitrations_query.filter(
                Task.created_at > gotos_reset_at
            )
        prior_arbitrations = prior_arbitrations_query.count()
        if prior_arbitrations >= max_arbitrations_per_phase:
            # Before giving up: the most recent arbitration may have already
            # reached a decision that was never acted on -- e.g.
            # _maybe_resolve_arbitration hasn't gotten to it on this sweep
            # tick yet, or some other caller reached this cap-check first.
            # Observed live: 3 consecutive arbitrations all independently
            # concluded "continue" against the same unchanged, clean
            # review.md (the 3rd one's own reasoning noted it was being
            # asked the same already-settled question a third time) -- yet
            # the workflow was failed anyway because this check only counts
            # attempts, not whether they converged. A cap meant to stop a
            # genuinely flip-flopping arbiter from looping forever should
            # not discard a consistent, already-decided, unprocessed result
            # in front of it. Resolve the latest one instead of failing if
            # it's sitting there done with a valid decision.
            last_task = (
                prior_arbitrations_query.order_by(Task.created_at.desc()).first()
            )
            if last_task and last_task.status == "done":
                wf_for_result = db.query(Workflow).filter_by(id=workflow_id).first()
                pending_working_directory = wf_for_result.working_directory if wf_for_result else None
                pending_decision, pending_target, pending_reason = _read_arbitration_result(
                    pending_working_directory
                )
                if pending_decision:
                    logger.warning(
                        f"[ARBITRATE] {phase_name} hit the {max_arbitrations_per_phase}-arbitration cap, "
                        f"but the last arbitration already decided '{pending_decision}' and was never "
                        "processed -- resolving it instead of failing the workflow."
                    )
                    _resolve_arbitration_outcome(
                        workflow_id, phase_id, phase_name, pending_decision,
                        pending_target, pending_reason or reason, logger,
                    )
                    # Consume it now that it's been acted on -- without
                    # this, the NEXT time this cap-exhausted branch fires
                    # (nothing here prevents phase_id's claim from being
                    # re-armed later, e.g. by _maybe_resolve_arbitration
                    # re-discovering this same "done" last_task on a later
                    # sweep tick) _read_arbitration_result finds the exact
                    # same file and replays the exact same decision again --
                    # a real, costly agent run every cycle, forever. See
                    # _consume_arbitration_result's docstring for the live
                    # incident this closes: the same "goto architecture_
                    # design" decision replayed for 4.5 hours across 20+
                    # architecture_design runs after design_review's
                    # arbitration cap was hit once.
                    _consume_arbitration_result(pending_working_directory)
                    return True

            # Nothing left to resolve (already consumed by an earlier pass,
            # or the last arbitration agent never wrote a decision). Before
            # failing outright: check whether the phase's CURRENT on-disk
            # output already passes for real, evaluated fresh against its
            # actual eval_point conditions. This is deliberately NOT the
            # max_review_runs mistake cb60308 fixed (silently skipping the
            # review and waving it through) -- it only ever fires here, past
            # the arbitration cap with no pending decision, and only
            # advances on a genuine fresh "continue" verdict; a missing or
            # still-failing artifact (e.g. this same phase's challenge.md,
            # deleted by consume_gate_artifacts after its last real run and
            # never regenerated since) correctly scores as not-passing and
            # falls through to failing the workflow below, same as today.
            wf_for_check = db.query(Workflow).filter_by(id=workflow_id).first()
            if wf_for_check and wf_for_check.working_directory:
                passes, fresh_reason = _phase_currently_passes(
                    workflow_id, phase_name, wf_for_check.working_directory, logger
                )
                if passes:
                    logger.warning(
                        f"[ARBITRATE] {phase_name} exhausted its {max_arbitrations_per_phase}-arbitration "
                        f"cap with nothing pending to resolve, but its current output already passes "
                        f"({fresh_reason}) -- advancing instead of failing the workflow."
                    )
                    return _fire_phase_transition(workflow_id, phase_id, phase_name, logger, force_continue=True)
            logger.error(f"[ARBITRATE] {phase_name} has already been arbitrated {prior_arbitrations} times without converging -- failing the workflow instead of arbitrating again")
            wf = db.query(Workflow).filter_by(id=workflow_id).first()
            if wf:
                wf.status = "failed"
                wf.status_reason = f"{phase_name}: arbitrated {prior_arbitrations} times without converging (last reason: {reason})"
                db.commit()
            return False

        if not _claim_phase_task_creation(db, phase_id):
            logger.info(f"[ARBITRATE] {phase_name} already has arbitration in flight -- skipping")
            return False

        execution = db.query(PhaseExecution).filter_by(phase_id=phase_id).first()
        if execution:
            # Keep the phase alive/visible until arbitration resolves.
            # Deliberately NOT "completed": mark_phase_complete would bail
            # via its idempotency guard when arbitration resolves. And NOT
            # "pending" either -- see _handle_evaluation_arbitrate's own
            # comment on this exact status value for why a mid-pipeline
            # "pending" phase sitting behind later-order completed phases
            # gets bypassed entirely by _case_completed_with_successor's
            # ordering logic. "in_progress" (with the arbitration task
            # that already exists) reads as a normal active phase to every
            # other advancement case.
            execution.status = "in_progress"

        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        working_directory = wf.working_directory if wf else None
        if wf:
            wf.status_reason = f"Awaiting arbiter decision for {phase_name}: {reason}"
        db.commit()

        valid_phase_names = [p.name for p in db.query(Phase).filter_by(workflow_id=workflow_id).order_by(Phase.order).all()]

    prompt = _build_arbitration_prompt(phase_id, phase_name, reason, working_directory, valid_phase_names)

    task_id = str(uuid.uuid4())
    with get_db() as db:
        # Ensure created_by_agent_id's FK is satisfied -- Task.created_by_
        # agent_id is a real ForeignKey("agents.id"), and ARBITRATION_CREATED_BY
        # ("arbitration") was never a real Agent row, only a sentinel string.
        # With FK enforcement on, every single insert below raised
        # sqlite3.IntegrityError, silently caught by _fire_phase_transition's
        # catch-all and re-logged as "[PHASE-ADVANCE] Transition error" --
        # the arbitration Task never persisted, so arbitration could never
        # actually happen; the phase just kept re-evaluating to "arbitrate"
        # every sweep tick forever. Mirrors the same get-or-create server.py's
        # create_task endpoint already does for its own created_by_agent_id.
        # Observed live: 1180+ failed attempts over ~30 hours on one
        # workflow, total_gotos climbing the whole time, zero arbitration
        # tasks ever created.
        if not db.query(Agent).filter_by(id=ARBITRATION_CREATED_BY).first():
            db.add(
                Agent(
                    id=ARBITRATION_CREATED_BY,
                    system_prompt="auto-created for arbitration task attribution",
                    status="idle",
                    cli_type="system",
                )
            )
            db.flush()
        task = Task(
            id=task_id,
            raw_description=f"Arbitrate stuck phase: {phase_name}",
            enriched_description=prompt,
            done_definition="Write arbitration_result.json with a decision and mark done",
            status="pending",
            priority="high",
            phase_id=phase_id,
            workflow_id=workflow_id,
            created_by_agent_id=ARBITRATION_CREATED_BY,
            action="arbitrate",
        )
        db.add(task)
        db.commit()

    agent_data = create_agent_for_task_direct(
        task_id,
        workflow_id,
        phase_id,
        # Not "arbitration" -- Agent.agent_type has a CHECK constraint
        # ('phase', 'validator', 'result_validator', 'monitor',
        # 'diagnostic', 'orchestrator') that "arbitration" was never a
        # member of, so every dispatch here unconditionally raised
        # sqlite3.IntegrityError, silently caught by create_agent_for_task_
        # direct's own except-and-return-None and logged only at DEBUG
        # (invisible at the default log level) -- every arbitration attempt
        # hit the "if not agent_data" branch below and failed the workflow,
        # even after Task creation itself was fixed to no longer FK-fail.
        # "diagnostic" is a safe substitute, not a hack: prompt_builder.py's
        # format_initial_message already treats "diagnostic" and
        # "arbitration" identically (both use the verbatim validation_prompt
        # path), so this changes zero prompt-building behavior while
        # actually satisfying the constraint. created_by_agent_id
        # (ARBITRATION_CREATED_BY) on the Task, not Agent.agent_type, is
        # what identifies/counts arbitration tasks elsewhere (the
        # max_arbitrations_per_phase cap above) -- unaffected by this.
        agent_type="diagnostic",
        enriched_data_override={"validation_prompt": prompt},
    )
    if not agent_data:
        # Dispatch itself failed -- never leave the phase silently claimed
        # forever with nothing working on it. Fail loudly and immediately
        # instead of quietly re-attempting every sweep tick.
        logger.error(f"[ARBITRATE] Failed to dispatch arbitration agent for {phase_name} -- failing the workflow instead of leaving it stuck silently")
        with get_db() as db:
            task = db.query(Task).filter_by(id=task_id).first()
            if task:
                task.status = "failed"
                task.failure_reason = "Failed to dispatch arbitration agent"

        pm = PhaseManager(DatabaseManager(None))
        pm.workflow_id = workflow_id
        pm.mark_phase_complete(
            phase_id,
            "Arbitration dispatch failed",
            force_action="fail",
        )
        with get_db() as db:
            wf = db.query(Workflow).filter_by(id=workflow_id).first()
            if wf:
                wf.status_reason = f"{phase_name}: could not dispatch an arbitration agent after exhausting retries ({reason})"
                db.commit()
        return False

    logger.warning(f"[ARBITRATE] Dispatched arbitration agent {agent_data.get('agent_id', '?')[:8]} for {phase_name}")
    return True


def _maybe_resolve_arbitration(workflow_id: str, logger: "OrchestratorLogger") -> None:
    """Check every phase with an in-flight arbitration for this workflow and
    act on the result once the arbitration agent finishes (or dies).

    Called every sweep tick alongside _advance_phases -- see
    _run_phase_advancement_sweep_once.
    """
    with get_db() as db:
        phases = db.query(Phase).filter_by(workflow_id=workflow_id).all()
        claimed_phase_ids = [p.id for p in phases if db.query(PhaseExecution).filter_by(phase_id=p.id).filter(PhaseExecution.task_creation_claimed_at.isnot(None)).first()]
        arb_tasks = {}
        for phase_id in claimed_phase_ids:
            t = (
                db.query(Task)
                .filter(
                    Task.phase_id == phase_id,
                    Task.created_by_agent_id == ARBITRATION_CREATED_BY,
                )
                .order_by(Task.created_at.desc())
                .first()
            )
            if t:
                arb_tasks[phase_id] = t
        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        working_directory = wf.working_directory if wf else None
        phase_names = {p.id: p.name for p in phases}

    for phase_id, task in arb_tasks.items():
        phase_name = phase_names.get(phase_id, phase_id)

        if task.status == "failed":
            reason = task.failure_reason or "Arbitration agent failed with no reason given"
            logger.error(f"[ARBITRATE] {phase_name}: arbitration agent failed -- {reason}")
            _resolve_arbitration_outcome(workflow_id, phase_id, phase_name, "fail", None, reason, logger)
            continue

        if task.status != "done":
            continue  # still running -- self-heal handles a dead agent eventually

        decision, target_phase, dec_reason = _read_arbitration_result(working_directory)
        if decision is None:
            logger.error(f"[ARBITRATE] {phase_name}: arbitration task marked done but arbitration_result.json is missing/invalid -- treating as fail")
            _resolve_arbitration_outcome(
                workflow_id,
                phase_id,
                phase_name,
                "fail",
                None,
                "Arbitration agent finished without writing a valid decision file",
                logger,
            )
            continue

        _resolve_arbitration_outcome(workflow_id, phase_id, phase_name, decision, target_phase, dec_reason, logger)
        # Consume it -- see _consume_arbitration_result's docstring. Not
        # strictly needed on THIS path today (a fresh arbitration task
        # overwrites the file before it's ever re-read here), but leaving
        # a resolved decision on disk is exactly the trap the cap-exhausted
        # fallback below fell into; don't leave a second copy of that trap
        # lying around for some future caller to walk into.
        _consume_arbitration_result(working_directory)


def _read_arbitration_result(
    working_directory: Optional[str],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Read + validate arbitration_result.json. Returns (decision, target_phase, reason);
    decision is None if the file is missing, unparseable, or has an invalid decision value."""
    if not working_directory:
        return None, None, None
    path = Path(working_directory) / CONTEXT_DIR_NAME / "arbitration_result.json"
    if not path.exists():
        return None, None, None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None, None, None
    decision = data.get("decision")
    if decision not in ("continue", "goto", "fail"):
        return None, None, None
    return decision, data.get("target_phase"), data.get("reason") or "(no reason given)"


def _consume_arbitration_result(working_directory: Optional[str]) -> None:
    """Delete arbitration_result.json once its decision has been acted on --
    mirrors consume_gate_artifacts's identical rationale for gate result
    files (spec.py): without this, the SAME already-resolved decision is
    still sitting on disk for any later caller to read again.

    _trigger_arbitration's cap-exhausted fallback re-reads this file every
    time it's invoked past max_arbitrations_per_phase, with no record of
    whether THIS exact decision already got acted on -- so a phase whose
    task_creation_claimed_at claim gets re-armed after the cap is hit (e.g.
    via _maybe_resolve_arbitration re-discovering the same "done"
    arbitration task on a later sweep tick) replayed the identical stale
    "goto" forever: a fresh, real, costly agent run for the goto target
    every cycle, never actually re-reviewing anything. Observed live:
    design_review's arbitration cap was hit once at 13:20, and the same
    "goto architecture_design" decision was silently replayed for the next
    4.5 hours across 20+ architecture_design runs.
    """
    if not working_directory:
        return
    path = Path(working_directory) / CONTEXT_DIR_NAME / "arbitration_result.json"
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _resolve_arbitration_outcome(
    workflow_id: str,
    phase_id: str,
    phase_name: str,
    decision: str,
    target_phase: Optional[str],
    reason: str,
    logger: "OrchestratorLogger",
) -> None:
    """Act on an arbitration decision and always release the phase's
    task_creation_claimed_at claim afterward -- regardless of outcome, or
    the phase stays permanently locked out of both normal advancement and
    future arbitration attempts.

    CRITICAL: mark_phase_complete NEVER creates the next task itself, for
    ANY action -- not force_action, not a normal evaluation. Every code
    path (_start_next_phase for continue, _handle_force_goto/
    _handle_evaluation_goto for goto) only flips PhaseExecution.status and
    returns a result dict; creating the actual Task row is always the
    CALLER's job (see _fire_phase_transition's explicit _create_phase_task
    call right after its own mark_phase_complete). An earlier version of
    this function discarded mark_phase_complete's return value entirely --
    "continue" and "goto" decisions closed out the arbitrating phase
    successfully but never dispatched anything for the next one, silently
    stranding the pipeline with workflow.status="active" and no agent
    ever running again, while status_reason got cleared as if everything
    were fine. Mirror _fire_phase_transition's pattern exactly.
    """
    logger.warning(f"[ARBITRATE] {phase_name}: decision={decision} -- {reason}")

    pm = PhaseManager(DatabaseManager(None))
    pm.workflow_id = workflow_id
    result: Dict[str, Any] = {}
    try:
        if decision == "continue":
            result = pm.mark_phase_complete(phase_id, f"Arbiter: proceed -- {reason}", force_action="continue")
        elif decision == "goto" and target_phase:
            result = pm.mark_phase_complete(
                phase_id,
                f"Arbiter: return for another attempt -- {reason}",
                force_action="goto",
                force_target_phase=target_phase,
                force_reason=reason,
            )
        else:
            result = pm.mark_phase_complete(phase_id, f"Arbiter: unrecoverable -- {reason}", force_action="fail")
    finally:
        # mark_phase_complete's _close_execution sets status but never
        # touches task_creation_claimed_at -- clear it directly rather than
        # reusing _release_phase_task_creation_claim, which would wrongly
        # flip a just-set "completed"/"failed" status back to "in_progress".
        with get_db() as db:
            execution = db.query(PhaseExecution).filter_by(phase_id=phase_id).first()
            if execution:
                execution.task_creation_claimed_at = None
            wf = db.query(Workflow).filter_by(id=workflow_id).first()
            if wf:
                # A "goto" whose target_phase didn't resolve to a real phase
                # (_find_phase_by_name_or_order does an exact-string match --
                # an LLM-hallucinated or mis-cased name won't match) falls
                # back to _advance_or_complete internally and returns
                # action != "goto" -- check the ACTUAL returned action, not
                # the raw decision, or a failed goto gets treated as a
                # silent success and status_reason is wrongly cleared.
                goto_target_missing = decision == "goto" and result.get("action") != "goto"
                if decision == "fail" or (decision == "goto" and not target_phase) or goto_target_missing:
                    detail = reason
                    if goto_target_missing:
                        detail = f"arbiter targeted unknown phase {target_phase!r} -- {reason}"
                    wf.status_reason = f"{phase_name}: {detail}"
                else:
                    wf.status_reason = None
            db.commit()

    # Dispatch the actual next task -- see this function's docstring for
    # why this can't be skipped. Any action that leaves should_continue
    # True and names a target phase (continue -> next phase in sequence,
    # goto -> the arbiter's chosen phase, or _advance_or_complete's own
    # fallback if the target didn't resolve) needs a real Task+agent.
    target_phase_id = result.get("target_phase_id")
    target_phase_name = result.get("target_phase")
    action = result.get("action")
    if target_phase_id and action in ("continue", "goto", "retry"):
        dispatched = _create_phase_task(
            workflow_id,
            target_phase_id,
            target_phase_name,
            action,
            logger,
            feedback=result.get("reason"),
            source_phase_name=phase_name,
        )
        if not dispatched:
            logger.error(f"[ARBITRATE] {phase_name}: resolved to {action} -> {target_phase_name}, but failed to create its task -- pipeline may be stalled")


def _cap_out_review_phase(
    db,
    workflow_id: str,
    phase,
    run_count: int,
    max_runs: int,
    logger: "OrchestratorLogger",
) -> Optional[bool]:
    """A review phase (architectural_review/adversarial_review, or any
    other phase opted into workflow.yaml's max_review_runs) hit its run cap
    without ever scoring clean -- stop looping instead of spawning yet
    another fresh-session agent to re-review from scratch.

    Writes a synthetic clean result (blocker_count=0) so the gate's own
    scorer lets the pipeline continue past this phase, with the
    accumulated findings history (see record_review_finding) appended to
    the phase's own report as a real, visible "unresolved, capped" record
    instead of silently dropping them. Then fires the same synthetic-
    completion path _create_phase_task already uses to skip a clean
    forensics_analysis run (_fire_phase_transition) -- no new completion
    mechanism, just a different reason for using it.

    Returns True/False (the outcome of firing the transition) once capped
    out successfully. Returns None if it couldn't safely cap out at all
    (no working_directory) -- callers must treat None as "fall through and
    create a normal task instead," not as a completed action: silently
    returning False here would strand the phase with no task, no synthetic
    completion, and no forward progress, forever, with nothing but a
    debug-level log to explain why.

    A phase with no GATE_RESULT_ARTIFACTS entry (e.g. security_review,
    doc_review -- opted into max_review_runs in workflow.yaml but not
    scored via a JSON gate artifact the way architectural_review/
    adversarial_review/qa_validation/product_validation are) has nothing
    for a scorer to re-read, so there's no synthetic result file to write
    -- but the cap must still apply. _fire_phase_transition doesn't require
    one either: it only calls build_phase_output (which reads
    GATE_RESULT_ARTIFACTS) for phases in GATED_PHASES, and _create_phase_
    task already relies on this exact same path with zero synthetic
    artifacts for forensics_analysis's clean-run shortcut. Previously this
    branch returned None here ("isn't a known gated phase"), which meant
    the cap silently never engaged for security_review/doc_review -- a live
    run hit 25 re-entries of security_review with max_review_runs: 4
    configured and doing nothing.
    """
    from src.autopilot.okf_markdown import write_okf
    from src.autopilot.spec import GATE_RESULT_ARTIFACTS, get_review_findings_history, synthetic_clean_result

    workflow = db.query(Workflow).filter_by(id=workflow_id).first()
    if not workflow or not workflow.working_directory:
        logger.warning(
            f"[PHASE-TASK] {phase.name} hit its review-run cap ({run_count}/"
            f"{max_runs}) but has no working_directory to write a synthetic "
            "completion to -- falling through to a normal task instead of "
            "stranding the phase silently"
        )
        return None

    docs_dir = Path(workflow.working_directory) / ".hephaestus" / phase.name
    docs_dir.mkdir(parents=True, exist_ok=True)

    history = get_review_findings_history(workflow_id, phase.name)
    caveats = "\n".join(f"- Run {h['run_number']}: {h['blocker_count']} blocker(s) -- {h['summary'][:200]}" for h in history) or "(no findings history recorded)"
    body = (
        f"# {phase.name} -- capped after {run_count} runs\n\n"
        f"Stopped re-reviewing after {max_runs} runs without a clean "
        "pass (workflow.yaml's max_review_runs). Unresolved findings "
        f"from prior runs:\n\n{caveats}\n"
    )

    artifacts = GATE_RESULT_ARTIFACTS.get(phase.name, ())
    if artifacts:
        write_okf(docs_dir / artifacts[0], synthetic_clean_result(phase.name, run_count), body)
    else:
        (docs_dir / f"{phase.name}_capped_notice.md").write_text(body)

    logger.warning(f"[PHASE-TASK] {phase.name} hit its review-run cap ({run_count}/{max_runs}) -- marking done with caveats instead of re-reviewing again")
    # force_continue=True: a normal (non-forced) transition re-runs
    # orchestrator.evaluate(), whose retry-count check fires before the
    # score is even read -- since max_retries and max_review_runs are
    # typically the same threshold, that check has always already tripped
    # by the time this cap engages, sending the phase straight to
    # arbitration/goto instead of past it. See _fire_phase_transition's
    # force_continue docstring for the live incident this closes.
    return _fire_phase_transition(workflow_id, phase.id, phase.name, logger, force_continue=True)


def _create_phase_task(
    workflow_id: str,
    phase_id: str,
    phase_name: str,
    action: str,
    logger: "OrchestratorLogger",
    feedback: Optional[str] = None,
    source_phase_name: Optional[str] = None,
    target_already_claimed: bool = False,
) -> bool:
    """Create a task and agent for a phase via API.

    source_phase_name: the phase whose evaluation decided `action` -- e.g.
    for a goto, the phase whose gate found something wrong and sent the
    pipeline back here. Recorded as this new task's own action_target_phase
    (same field _tag_completing_task sets on the DECIDING phase's task, just
    the complementary direction: "where I came from" here vs. "where I sent
    things" there). Irrelevant for action="continue" (normal advancement).

    target_already_claimed: True when the caller already holds phase_id's
    task_creation_claimed_at claim before calling (every self-heal case in
    this file that dispatches to the SAME phase_id it just evaluated --
    _case_start_first_phase, _case_in_progress_no_tasks,
    _case_completed_with_successor, _case_in_progress_complete's own
    empty-cycle branch). Callers that dispatch to a phase OTHER than the one
    they hold a claim on (_fire_phase_transition and
    _resolve_arbitration_outcome, whose target_phase_id is routinely a goto
    target different from the source phase they claimed) leave this False,
    so this function claims phase_id itself -- closing a gap the existing-
    task check below openly can't close alone (see its own comment): a
    caller with no claim on phase_id sees a "pending, no agent yet" task
    that's actually just a slow create_agent_for_task_direct dispatch
    (worktree setup, tmux launch can run past the 1-minute orphan cutoff)
    still in flight, and races right past it with a second task for the
    same phase. Observed live: two goto tasks landed on architecture_design
    85s apart this way.
    """
    from src.autopilot.orchestrator import _orchestrator_agent_id
    own_claim = False
    if not target_already_claimed:
        with get_db() as _claim_db:
            if not _claim_phase_task_creation(_claim_db, phase_id):
                # Held, but not necessarily by anyone still alive -- this
                # function can't assume _release_stale_task_creation_claims
                # already swept the TARGET phase this cycle (it only ever
                # runs on phases the current sweep tick's own workflow-wide
                # pass reached; a direct/out-of-band caller gets no such
                # guarantee). Reuse the same staleness window before
                # deferring to a claim that's actually just abandoned: clear
                # it and take a fresh one, exactly what
                # _release_stale_task_creation_claims would have done to it
                # anyway had it reached this phase first.
                stale_cutoff = datetime.utcnow() - timedelta(seconds=CLAIM_STALE_TIMEOUT_SECONDS)
                cleared = (
                    _claim_db.query(PhaseExecution)
                    .filter(
                        PhaseExecution.phase_id == phase_id,
                        PhaseExecution.task_creation_claimed_at.isnot(None),
                        PhaseExecution.task_creation_claimed_at < stale_cutoff,
                    )
                    .update({"task_creation_claimed_at": None}, synchronize_session=False)
                )
                _claim_db.commit()
                if not cleared or not _claim_phase_task_creation(_claim_db, phase_id):
                    logger.info(f"[PHASE-TASK] {phase_name} task creation already claimed by another caller -- skipping")
                    return False
        own_claim = True
    try:
        import uuid

        with get_db() as db:
            # Run the mandatory automated security scan ourselves before the
            # agent starts (see _run_ash_scan) — don't rely on the agent to
            # remember a "MANDATORY" prompt instruction.
            if phase_name == "security_review":
                wf = db.query(Workflow).filter_by(id=workflow_id).first()
                if wf and wf.working_directory and Path(wf.working_directory).exists():
                    _run_ash_scan(Path(wf.working_directory), logger)

            # forensics_analysis reviews every artifact + full tmux transcript
            # of a completed feature run to propose prompt/methodology fixes —
            # expensive (whole-pipeline review) and only actionable when
            # something actually went wrong. Skip spawning that agent on a
            # clean run (no tmux error patterns) and advance straight to the
            # next phase instead, using the same completion path a real agent
            # would trigger via update_task_status.
            if phase_name == "forensics_analysis":
                wf = db.query(Workflow).filter_by(id=workflow_id).first()
                if wf and wf.working_directory and Path(wf.working_directory).exists():
                    health = _assess_run_health(
                        Path(wf.working_directory),
                        workflow_id,
                        None,
                        logger,
                    )
                    if health["clean"]:
                        logger.info("[PHASE-TASK] forensics_analysis skipped — run was clean (no tmux error patterns detected)")
                        # _fire_phase_transition marks this phase complete via
                        # PhaseManager itself and advances to the next phase —
                        # the same completion path a real agent would trigger
                        # via update_task_status, just fired synthetically.
                        return _fire_phase_transition(workflow_id, phase_id, phase_name, logger)

            # deploy phase: skip entirely if DEPLOY.md doesn't exist
            if phase_name == "deploy":
                wf = db.query(Workflow).filter_by(id=workflow_id).first()
                if wf and wf.working_directory:
                    deploy_md = Path(wf.working_directory) / "DEPLOY.md"
                    if not deploy_md.exists():
                        logger.info(f"[PHASE-TASK] deploy skipped — DEPLOY.md not found in {wf.working_directory}")
                        return _fire_phase_transition(workflow_id, phase_id, phase_name, logger)

            # Check if phase already has an active task
            existing = (
                db.query(Task)
                .filter(
                    Task.phase_id == phase_id,
                    Task.status.in_(["pending", "assigned", "in_progress", "queued"]),
                )
                .first()
            )
            if existing:
                # A "pending" task with no assigned_agent_id was never
                # actually dispatched (or its agent was terminated after the
                # fact, e.g. manual cleanup of a stuck agent) -- it isn't
                # blocking anything, it's just an orphan. Treating it the
                # same as a genuinely active task here means nothing ever
                # replaces it (observed live: an architectural_review task
                # sat "pending" with no agent for hours -- every self-heal
                # pass saw it and silently skipped, since its status string
                # alone made it look active). Clear it and fall through to
                # create a fresh task instead of returning early.
                #
                # BUT: require it to actually be old before calling it
                # orphaned (same 1-minute threshold _case_in_progress_
                # complete's own orphaned-pending check already uses) --
                # without this, a task that's simply mid-flight (row
                # committed, agent not attached yet -- a normal few-second
                # gap in the creation sequence) looks identical to a
                # genuine hours-old orphan. Observed live: two callers
                # evaluating the same phase 11 seconds apart raced past
                # each other -- the second one saw the first task still
                # agentless, "helpfully" marked it failed, and spawned a
                # full duplicate agent for the same phase. The task_creation_
                # claimed_at claim only serializes who gets to create a
                # task; it does nothing to stop this check from
                # misjudging one that already exists.
                orphan_cutoff = datetime.utcnow() - timedelta(minutes=1)
                # A "pending" task can also be orphaned the OTHER way: it WAS
                # dispatched (assigned_agent_id set), but that agent later
                # died/got terminated (killed mid-launch by a backend
                # restart, or manually terminated as a stuck-agent cleanup)
                # before ever flipping the task to "in_progress" or creating
                # a replacement. assigned_agent_id alone doesn't mean "still
                # being worked" -- check whether that agent is actually
                # still active. Observed live: a task sat "pending" pointing
                # at a terminated agent indefinitely, since this check only
                # ever looked at assigned_agent_id being NULL.
                assigned_agent = (
                    db.query(Agent).filter_by(id=existing.assigned_agent_id).first()
                    if existing.assigned_agent_id
                    else None
                )
                agent_is_dead = existing.assigned_agent_id and (
                    assigned_agent is None
                    or assigned_agent.status not in ("working", "idle", "starting")
                )
                if (
                    existing.status == "pending"
                    and (not existing.assigned_agent_id or agent_is_dead)
                    and existing.created_at < orphan_cutoff
                ):
                    reason = (
                        "never dispatched to an agent"
                        if not existing.assigned_agent_id
                        else f"assigned agent {existing.assigned_agent_id[:8]} is no longer active"
                    )
                    logger.info(
                        f"[PHASE-TASK] {phase_name} has an orphaned pending task "
                        f"{existing.id[:8]} ({reason}, stale >1min) -- "
                        "marking failed and creating a fresh one"
                    )
                    existing.status = "failed"
                    existing.failure_reason = f"Orphaned: {reason}"
                    db.commit()
                else:
                    logger.info(f"[PHASE-TASK] {phase_name} already has active task {existing.id[:8]}, skipping")
                    return False

            # Check for active agent on this phase
            active_agent = db.query(Agent).filter(Agent.status.in_(["working", "idle", "starting"])).join(Task, Task.assigned_agent_id == Agent.id).filter(Task.phase_id == phase_id).first()
            if active_agent:
                logger.info(f"[PHASE-TASK] {phase_name} has active agent {active_agent.id[:8]}, skipping")
                return False

            # Check retry/goto bounds
            max_phase_attempts = 5
            if action in ("retry", "goto"):
                retries = (
                    db.query(Task)
                    .filter(
                        Task.phase_id == phase_id,
                        Task.created_by_agent_id == "orchestrator",
                        Task.action.in_(["retry", "goto"]),
                    )
                    .count()
                )
                if retries >= max_phase_attempts:
                    logger.warning(f"[PHASE-TASK] {phase_name} hit retry bound ({retries}/{max_phase_attempts}), triggering arbitration")
                    _trigger_arbitration(
                        workflow_id,
                        phase_id,
                        phase_name,
                        f"{phase_name} was sent back {retries} times without resolving (last reason: {feedback or 'unknown'})",
                        logger,
                    )
                    return False

            # Get phase info
            phase = db.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                return False

            # Review-run cap + prior-findings injection -- opt-in per phase
            # via workflow.yaml's max_review_runs (None for every phase
            # that doesn't set it, i.e. today's uncapped behavior). Counts
            # ALL Task rows ever created for this phase_id: unlike
            # PhaseExecution (reused in place across goto resets), a Task
            # row is created fresh on every re-entry, so this is a correct
            # "how many times has this phase run" total.
            from src.autopilot.spec import get_max_review_runs, get_review_findings_history

            max_review_runs = get_max_review_runs(workflow_id, phase.name)
            prior_findings_block = ""
            if max_review_runs is not None:
                run_count = db.query(Task).filter(Task.phase_id == phase_id).count()
                if run_count >= max_review_runs:
                    capped = _cap_out_review_phase(db, workflow_id, phase, run_count, max_review_runs, logger)
                    if capped is not None:
                        return capped
                    # None: couldn't safely cap out (see its own docstring)
                    # -- fall through to a normal task rather than
                    # stranding the phase with no forward progress.
                if run_count > 0:
                    history = get_review_findings_history(workflow_id, phase.name)
                    if history:
                        findings_lines = "\n".join(f"- Run {h['run_number']}: {h['blocker_count']} blocker(s) -- {h['summary'][:200]}" for h in history)
                        prior_findings_block = (
                            f"\n\nPRIOR FINDINGS FROM {len(history)} EARLIER "
                            f"RUN(S) OF THIS PHASE:\n{findings_lines}\n\n"
                            "Verify ONLY whether these specific findings are "
                            "now fixed. Do not re-review from scratch unless "
                            "you find something genuinely new. The above is "
                            "everything that survived from those earlier runs "
                            "-- their original report/result files are gone "
                            "(deleted after being read into this summary), so "
                            "don't try to read them."
                        )

            # Create task
            task_id = str(uuid.uuid4())
            base_description = f"Execute {phase.name}: {phase.description}"
            description = (
                f"{base_description}\n\n{GOTO_REASON_PREFIX}{feedback}\nAddress this specifically -- this is not a fresh implementation pass, it's a return from review with a concrete issue to fix."
                if feedback
                else base_description
            ) + prior_findings_block
            task = Task(
                id=task_id,
                raw_description=description,
                enriched_description=description,
                done_definition=(" AND ".join(phase.done_definitions) if phase.done_definitions else "Complete phase objectives"),
                status="pending",
                priority="high",
                phase_id=phase.id,
                workflow_id=workflow_id,
                # The literal "orchestrator" string was never a real Agent
                # row (the real one is registered as "orchestrator-<hex8>",
                # see run_continuous_pipeline) -- with FK enforcement this
                # unconditionally violated Task.created_by_agent_id's FK.
                # created_by_agent_id is nullable; fall back to None if the
                # orchestrator agent hasn't been registered in this process.
                created_by_agent_id=_orchestrator_agent_id,
                action=action,
                action_target_phase=(source_phase_name if action in ("goto", "retry") else None),
            )
            db.add(task)

            # Update phase execution to in_progress
            execution = db.query(PhaseExecution).filter_by(phase_id=phase_id).first()
            if execution:
                if execution.status in ("pending", "completed"):
                    execution.status = "in_progress"
                    execution.started_at = datetime.utcnow()
                # Always release the claim once the task it was guarding
                # actually exists, regardless of the entry status. The
                # status-gated version of this reset only fired for the
                # pending/completed -> in_progress transition (e.g. a GOTO
                # reactivation), but _case_in_progress_no_tasks calls
                # _create_phase_task for phases a DIFFERENT path already
                # flipped to "in_progress" before a task existed (e.g. the
                # synchronous /start_workflow_execution step) -- for those,
                # entry status is already "in_progress", the old condition
                # never matched, and the claim taken to create this task
                # was never released. Since the claim field is reused by
                # _case_in_progress_complete to guard this same phase's
                # own later completion-transition evaluation, a claim left
                # over from task creation permanently blocked that
                # evaluation forever ("transition already being evaluated
                # by another caller — skipping", repeating every sweep
                # tick with no other caller actually holding it). Observed
                # live: a Feature Architect task finished successfully but
                # its phase never advanced, sitting in_progress
                # indefinitely.
                execution.task_creation_claimed_at = None

            db.commit()

        # Create agent directly in-process (H-2 fix — no self-HTTP call)
        agent_data = create_agent_for_task_direct(task_id, workflow_id, phase_id)
        if not agent_data:
            # Agent creation failed — clean up the orphaned task
            logger.warning(f"[PHASE-TASK] Failed to create agent for {phase_name}, cleaning up task {task_id[:8]}")
            with get_db() as db:
                task = db.query(Task).filter_by(id=task_id).first()
                if task:
                    task.status = "failed"
                    db.commit()
            return False

        agent_id = agent_data.get("agent_id", "unknown")

        # Update task with agent
        with get_db() as db:
            task = db.query(Task).filter_by(id=task_id).first()
            if task:
                task.assigned_agent_id = agent_id
                task.status = "in_progress"
                task.started_at = datetime.utcnow()
                db.commit()

        logger.info(f"[PHASE-TASK] Created task {task_id[:8]} and agent {agent_id[:8]} for {phase_name}")
        return True

    except Exception as e:
        logger.warning(f"[PHASE-TASK] Error creating task for {phase_name}: {e}")
        return False
    finally:
        # Release only the claim this call itself took. A claim the caller
        # already held (target_already_claimed=True) is the caller's own
        # to release -- this function's existing success-path already
        # clears task_creation_claimed_at once the task exists either way,
        # so this only matters for the early-return/bail-out paths above,
        # which never touched it. Direct clear, not _release_phase_task_
        # creation_claim -- that helper also flips a pending/completed
        # execution to "in_progress", which would be wrong on a bail-out
        # (e.g. "existing active task found", "retry bound hit") where
        # nothing was actually started.
        if own_claim:
            with get_db() as _release_db:
                _release_db.query(PhaseExecution).filter_by(phase_id=phase_id).update(
                    {"task_creation_claimed_at": None}, synchronize_session=False
                )
                _release_db.commit()


def _create_corrective_task(
    workflow_id: str,
    phase_id: str,
    phase_name: str,
    feedback: str,
    logger: "OrchestratorLogger",
) -> Optional[str]:
    """Create a task asking the agent to fix a specific, known validation
    failure in its already-written output, instead of the phase's whole
    output getting discarded and the entire (expensive) run redone from
    scratch. Reopens the phase/workflow if the engine already marked them
    complete -- a normal 'done' claim doesn't know a downstream hard-floor
    check will later reject it.

    Claims phase_id itself, same as _create_phase_task's own
    target_already_claimed=False path and for the identical reason: this
    is called from _negotiate_validation_fix while the background sweep
    can independently decide the same (routinely phase-0/1) phase needs a
    task of its own, and nothing here previously stopped that race.

    Returns the new task's id, or None if agent creation failed.
    """
    import uuid

    from src.core.database import PhaseExecution, get_db

    with get_db() as _claim_db:
        if not _claim_phase_task_creation(_claim_db, phase_id):
            stale_cutoff = datetime.utcnow() - timedelta(seconds=CLAIM_STALE_TIMEOUT_SECONDS)
            cleared = (
                _claim_db.query(PhaseExecution)
                .filter(
                    PhaseExecution.phase_id == phase_id,
                    PhaseExecution.task_creation_claimed_at.isnot(None),
                    PhaseExecution.task_creation_claimed_at < stale_cutoff,
                )
                .update({"task_creation_claimed_at": None}, synchronize_session=False)
            )
            _claim_db.commit()
            if not cleared or not _claim_phase_task_creation(_claim_db, phase_id):
                logger.info(f"[CORRECTIVE-TASK] {phase_name} task creation already claimed by another caller -- skipping")
                return None

    task_id = str(uuid.uuid4())
    try:
        _corrective_task_id = _create_corrective_task_body(
            workflow_id, phase_id, phase_name, feedback, logger, task_id
        )
        return _corrective_task_id
    finally:
        with get_db() as _release_db:
            _release_db.query(PhaseExecution).filter_by(phase_id=phase_id).update(
                {"task_creation_claimed_at": None}, synchronize_session=False
            )
            _release_db.commit()


def _create_corrective_task_body(
    workflow_id: str,
    phase_id: str,
    phase_name: str,
    feedback: str,
    logger: "OrchestratorLogger",
    task_id: str,
) -> Optional[str]:
    """The actual task+agent creation _create_corrective_task wraps with a
    claim -- split out only so that wrapper's try/finally doesn't have to
    re-indent this whole body."""
    from src.autopilot.orchestrator import _orchestrator_agent_id
    from src.core.database import Phase, PhaseExecution, Task, Workflow, get_db

    with get_db() as db:
        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        if not wf:
            logger.warning(f"[CORRECTIVE-TASK] Workflow {workflow_id[:8]} not found")
            return None
        if wf.paused_by is not None:
            # Same class of bug _try_auto_resume_paused_workflow was fixed
            # for: don't override a deliberate pause. Unlike that function
            # (which just skips and leaves the workflow alone), this one
            # would otherwise both reactivate the workflow AND immediately
            # spawn a live agent against it -- silently resuming real work
            # on something the user or budget explicitly stopped.
            pause_reason = wf.paused_by
            logger.info(f"[CORRECTIVE-TASK] Workflow {workflow_id[:8]} is {pause_reason}-paused — skipping corrective task")
            return None
        if wf.status != "active":
            wf.status = "active"

        execution = db.query(PhaseExecution).filter_by(phase_id=phase_id).first()
        if execution and execution.status != "in_progress":
            execution.status = "in_progress"
            # NOT clearing task_creation_claimed_at here -- the wrapper
            # (_create_corrective_task) now holds this phase's claim for
            # this whole call and releases it itself once the task exists
            # (or creation fails). Clearing it here, mid-body, would open
            # exactly the race the wrapper's claim exists to close: a
            # concurrent self-heal caller could claim the "now unclaimed"
            # phase and create a sibling task before db.add(task) below
            # even commits.

        phase = db.query(Phase).filter_by(id=phase_id).first()
        done_def = " AND ".join(phase.done_definitions) if phase and phase.done_definitions else "Complete phase objectives"

        task = Task(
            id=task_id,
            raw_description=f"Fix validation failure in {phase_name}: {feedback}",
            enriched_description=(
                f"Your previous '{phase_name}' output failed validation:\n\n"
                f"    {feedback}\n\n"
                "Your existing work is still in this worktree — do NOT start "
                "over from scratch. Read what you already wrote, fix ONLY the "
                f"specific problem above, and re-check it against: {done_def}\n\n"
                "When fixed, call update_task_status(done) again."
            ),
            done_definition=done_def,
            status="pending",
            priority="high",
            phase_id=phase_id,
            workflow_id=workflow_id,
            created_by_agent_id=_orchestrator_agent_id,  # see _create_phase_task
            action="retry",
            action_target_phase=phase_name,
        )
        db.add(task)
        db.commit()

    agent_data = create_agent_for_task_direct(task_id, workflow_id, phase_id)
    if not agent_data:
        logger.warning(f"[CORRECTIVE-TASK] Failed to create agent for corrective task on {phase_name}")
        with get_db() as db:
            t = db.query(Task).filter_by(id=task_id).first()
            if t:
                t.status = "failed"
                db.commit()
        return None

    agent_id = agent_data.get("agent_id", "unknown")
    with get_db() as db:
        t = db.query(Task).filter_by(id=task_id).first()
        if t:
            t.assigned_agent_id = agent_id
            t.status = "in_progress"
            t.started_at = datetime.utcnow()
            db.commit()

    logger.info(f"[CORRECTIVE-TASK] Created task {task_id[:8]} and agent {agent_id[:8]} to fix: {feedback}")
    return task_id


def _wait_for_task_terminal(
    task_id: str,
    timeout_seconds: int,
    logger: "OrchestratorLogger",
    project_id: Optional[str] = None,
) -> str:
    """Poll a task until it reaches a terminal status or times out.

    Returns "done", "failed", "timeout", or "interrupted".
    """
    from src.autopilot.orchestrator import _should_stop
    from src.core.database import Task, get_db

    start = time.time()
    while time.time() - start < timeout_seconds:
        if _should_stop(project_id):
            return "interrupted"
        with get_db() as db:
            task = db.query(Task).filter_by(id=task_id).first()
            status = task.status if task else None
        if status in ("done", "failed"):
            return status
        time.sleep(POLL_INTERVAL)
    logger.warning(f"[CORRECTIVE-TASK] Task {task_id[:8]} timed out after {timeout_seconds}s")
    return "timeout"


def _negotiate_validation_fix(
    workflow_id: str,
    phase_id: str,
    phase_name: str,
    output_path: Path,
    validate_fn,
    initial_error: str,
    logger: "OrchestratorLogger",
    max_attempts: int = 2,
    timeout_seconds: int = 900,
    project_id: Optional[str] = None,
) -> Tuple[bool, Optional[dict]]:
    """When a phase's output fails a validation check, don't discard the
    whole run — ask the same worktree's agent to fix the specific problem,
    up to max_attempts times, before giving up.

    validate_fn(dict) must raise (json.JSONDecodeError, ValueError) on
    invalid content, matching _validate_features_json's contract.

    Returns (success, parsed_json_or_None).
    """
    error = initial_error
    for attempt in range(1, max_attempts + 1):
        logger.info(f"[NEGOTIATE] Attempt {attempt}/{max_attempts} for {phase_name}: {error}")
        task_id = _create_corrective_task(workflow_id, phase_id, phase_name, error, logger)
        if not task_id:
            return False, None

        result = _wait_for_task_terminal(task_id, timeout_seconds, logger, project_id)
        if result not in ("done",):
            logger.warning(f"[NEGOTIATE] Corrective task {result} for {phase_name} — giving up")
            return False, None

        try:
            parsed = json.loads(output_path.read_text())
            validate_fn(parsed)
            logger.info(f"[NEGOTIATE] {phase_name} fixed on attempt {attempt}")
            return True, parsed
        except (json.JSONDecodeError, ValueError) as e:
            error = str(e)
            logger.warning(f"[NEGOTIATE] Still invalid after attempt {attempt}: {error}")

    logger.error(f"[NEGOTIATE] {phase_name} still failing validation after {max_attempts} corrective attempts: {error}")
    return False, None


def _resume_stuck_workflow_tasks(workflow_id: str, logger: "OrchestratorLogger") -> int:
    """Un-pause a workflow and restart its stuck tasks in-process.

    Mirrors autopilot_api.py's resume_feature endpoint (un-pause the
    workflow, reset blocked/failed tasks plus any assigned/in_progress task
    whose agent was terminated, spawn a fresh agent for each) -- but sync,
    since this runs from the orchestrator's own background thread rather
    than a FastAPI request, so there's no event loop to await
    agent_manager calls on. Uses create_agent_for_task_direct, the same
    in-process agent-creation path _create_phase_task already uses.

    Returns the number of tasks restarted.
    """
    from src.core.database import Agent, Task, Workflow, get_db

    to_restart: List[tuple] = []
    with get_db() as db:
        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        if not wf:
            return 0
        if wf.status == "paused" and wf.paused_by is not None:
            # Same class of bug _try_auto_resume_paused_workflow was fixed
            # for: this runs whenever the design/feature queue loop cycles
            # back to a workflow it already has an id for, which can
            # include one the user or budget deliberately paused -- don't
            # silently un-pause and restart work on it.
            pause_reason = wf.paused_by
            logger.info(f"[RESUME-STUCK] Workflow {workflow_id[:8]} is {pause_reason}-paused — skipping")
            return 0
        if wf.status in ("paused", "failed"):
            wf.status = "active"

        candidates = (
            db.query(Task)
            .filter(
                Task.workflow_id == workflow_id,
                Task.status.in_(["blocked", "failed", "assigned", "in_progress", "pending"]),
            )
            .all()
        )
        restartable = []
        # "pending" tasks are the odd one out here: unlike blocked/failed
        # (always safe to retry) or assigned/in_progress (an agent was
        # dispatched, so a dead agent means genuinely stuck), a task
        # normally sits "pending" only briefly -- creation and first
        # dispatch happen in the same synchronous call. A pending task
        # with no agent at all is only actually stuck if it's sat well
        # past how long that normally takes; otherwise this would sweep
        # up tasks mid-dispatch and race the code that's about to assign
        # them. See orchestrator's _create_phase_task orphan-detection
        # comment and monitor.py's stuck-detection for the same 5-minute
        # convention used elsewhere.
        pending_stuck_minutes = 5
        for t in candidates:
            if t.status in ("blocked", "failed"):
                restartable.append(t)
            elif t.status == "pending":
                if t.assigned_agent_id:
                    agent = db.query(Agent).filter_by(id=t.assigned_agent_id).first()
                    if not agent or agent.status == "terminated":
                        restartable.append(t)
                elif t.created_at and (datetime.utcnow() - t.created_at) > timedelta(minutes=pending_stuck_minutes):
                    restartable.append(t)
            elif t.assigned_agent_id:
                agent = db.query(Agent).filter_by(id=t.assigned_agent_id).first()
                if not agent or agent.status == "terminated":
                    restartable.append(t)

        to_restart = [(t.id, t.phase_id) for t in restartable]
        for t in restartable:
            t.status = "pending"
            t.failure_reason = None
            t.assigned_agent_id = None
            # This row is reused for the restart -- clear any stale
            # goto/retry tag from a previous life (see the matching fix in
            # restart_task_endpoint / server.py's on-demand-retry resume).
            t.action = ""
            t.action_target_phase = None

        db.commit()

    restarted = 0
    for task_id, phase_id in to_restart:
        try:
            agent_data = create_agent_for_task_direct(task_id, workflow_id, phase_id)
            if not agent_data:
                logger.warning(f"[RESUME] Failed to create agent for task {task_id[:8]}")
                continue
            agent_id = agent_data.get("agent_id", "unknown")
            with get_db() as db:
                task = db.query(Task).filter_by(id=task_id).first()
                if task:
                    task.assigned_agent_id = agent_id
                    task.status = "in_progress"
                    task.started_at = datetime.utcnow()
                    db.commit()
            logger.info(f"[RESUME] Restarted task {task_id[:8]} with agent {agent_id[:8]}")
            restarted += 1
        except Exception as e:
            logger.warning(f"[RESUME] Failed to restart task {task_id[:8]}: {e}")
    return restarted
