"""Control-loop engine: goto/retry/continue state machine, phase-task
creation. The arbitration subsystem (what happens when a phase exhausts
its retry/goto budget) lives in arbitration.py; its public names are
re-exported here (see the import block below) for backward compatibility
with existing callers/test patches.
"""

import asyncio
import json
import logging
import threading as _threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Literal, Optional, Tuple

# _trigger_arbitration/ARBITRATION_CREATED_BY are used directly below; the
# rest are re-exported (not referenced in this file) so existing external
# references keep working unchanged -- background_loops.py's own deferred
# import of _maybe_resolve_arbitration, and the many
# @patch("...phase_transitions.X") sites in test_advance_phases.py. patch()
# resolves against the CURRENT module's attribute, not the original
# definition site, so re-exporting here is sufficient; the "as X" form is
# ruff's own marker for "this import is intentionally unused, don't flag it."
from src.autopilot.orchestrator._phase_case_steps import (
    _build_phase_task,
    _handle_spec_gate_result,
    _mark_orphaned_and_stale_pending_tasks_failed,
    _retry_failed_tasks_with_done,
    _review_run_cap_and_findings,
)
from src.autopilot.orchestrator.arbitration import ARBITRATION_CREATED_BY, _trigger_arbitration
from src.autopilot.orchestrator.arbitration import (
    _build_arbitration_prompt as _build_arbitration_prompt,
)
from src.autopilot.orchestrator.arbitration import (
    _consume_arbitration_result as _consume_arbitration_result,
)
from src.autopilot.orchestrator.arbitration import (
    _gather_arbitration_context as _gather_arbitration_context,
)
from src.autopilot.orchestrator.arbitration import (
    _maybe_resolve_arbitration as _maybe_resolve_arbitration,
)
from src.autopilot.orchestrator.arbitration import (
    _maybe_resolve_human_arbitration_escalations as _maybe_resolve_human_arbitration_escalations,
)
from src.autopilot.orchestrator.arbitration import (
    _phase_currently_passes as _phase_currently_passes,
)
from src.autopilot.orchestrator.arbitration import (
    _read_arbitration_result as _read_arbitration_result,
)
from src.autopilot.orchestrator.arbitration import (
    _resolve_arbitration_outcome as _resolve_arbitration_outcome,
)
from src.autopilot.orchestrator.engine_client import (
    create_agent_for_task_direct,
    get_tasks,
    increment_task_retry_count,
    update_task_status,
)
from src.autopilot.orchestrator.queue import (
    _assess_run_health,
)
from src.autopilot.spec import DIFF_STABLE_REVIEW_PHASES, build_phase_output, get_gated_phases
from src.core.constants import (
    CONTEXT_DIR_NAME,
    DIAGNOSTIC_TASK_PREFIX,
    PHASE0_DEFINITION_IDS,
)
from src.core.database import (
    Agent,
    Feature,
    Phase,
    PhaseExecution,
    PhaseExecutionStatus,
    Task,
    Workflow,
    get_db,
    get_default_db_manager,
    utc_now,
)
from src.core.simple_config import get_config
from src.phases import PhaseManager

if TYPE_CHECKING:
    from src.autopilot.orchestrator import OrchestratorLogger

logger = logging.getLogger(__name__)


POLL_INTERVAL = 15


CLAIM_STALE_TIMEOUT_SECONDS = 480  # 8 minutes -- must stay shorter than


def _clear_stale_task_creation_claim(db, phase_id: str, *, repair_status: bool = True) -> bool:
    """Clear a stale task_creation_claimed_at on a single phase.

    If repair_status is True and the phase's execution is still
    "pending" or "completed", also flips it to "in_progress" and
    backfills started_at from the phase's latest task -- matching
    _release_stale_task_creation_claims's sweep behavior, so every
    caller that clears a stale claim also repairs the execution state
    that a held claim silently blocks.

    Returns True if a stale claim was found and cleared, False otherwise.

    Never clears a claim guarding a still-in-flight arbitration (see
    _phase_has_arbitration_in_flight), regardless of age. An arbiter is a
    real LLM-driven agent dispatch -- spawn, read context, reason, write
    arbitration_result.json -- legitimately taking longer than
    CLAIM_STALE_TIMEOUT_SECONDS (8 minutes) is not a corner case. Without
    this, this function reintroduces the exact bug
    _phase_has_arbitration_in_flight's other two call sites were added to
    fix, just on this function's own timer instead of an immediate
    caller's return: the claim vanishes out from under a real, running
    arbitration, _maybe_resolve_arbitration permanently stops looking at
    the phase once it's done, and the arbiter's eventual decision is
    silently dropped. A genuinely dead arbiter agent still gets caught --
    once its task is marked "failed" by the normal orphan/health-check
    self-heal, _phase_has_arbitration_in_flight no longer considers it in
    flight and _maybe_resolve_arbitration's own "failed" branch resolves
    and clears the claim itself.
    """
    if _phase_has_arbitration_in_flight(db, phase_id):
        return False

    stale_cutoff = utc_now() - timedelta(seconds=CLAIM_STALE_TIMEOUT_SECONDS)
    cleared = (
        db.query(PhaseExecution)
        .filter(
            PhaseExecution.phase_id == phase_id,
            PhaseExecution.task_creation_claimed_at.isnot(None),
            PhaseExecution.task_creation_claimed_at < stale_cutoff,
        )
        .update({"task_creation_claimed_at": None}, synchronize_session=False)
    )
    if cleared and repair_status:
        execution = db.query(PhaseExecution).filter_by(phase_id=phase_id).first()
        # "failed" included alongside pending/completed/skipped -- same gap
        # as _create_phase_task's reopen condition (fixed in 4d2f2005): a
        # phase execution stuck "failed" from an earlier attempt must still
        # reopen to "in_progress" once a fresh task exists under it, or it
        # stays invisible to every _advance_phases dispatch case.
        if execution and execution.status in ("pending", "completed", "skipped", "failed"):
            latest_task = (
                db.query(Task)
                .filter_by(phase_id=phase_id)
                .order_by(Task.created_at.desc())
                .first()
            )
            if latest_task:
                execution.status = "in_progress"
                execution.started_at = execution.started_at or latest_task.created_at
    if cleared:
        db.commit()
    return bool(cleared)


def reset_stale_executions_on_goto(
    db,
    workflow_id: str,
    target_phase_order: int,
    *,
    exclude_phase_id: str,
) -> int:
    """Reset PhaseExecution rows at or after a goto target to "pending".

    On a goto/rewind, phases after the target retain stale
    "in_progress"/"completed"/"failed" status from a prior pass.  Without
    resetting them, a later re-entry finds its execution already
    "completed" and re-evaluates without running -- or, for "failed",
    never runs again at all: mark_phase_complete's idempotency guard only
    checks for "completed", so a "failed" execution DOES still get a fresh
    task dispatched and can genuinely succeed on retry, but nothing ever
    flips its PhaseExecution.status off of "failed" once set. That stale
    "failed" row is a permanent tombstone for derive_workflow_status,
    whose phase-completeness check treats ANY status other than
    "completed"/"skipped" as "this phase hasn't finished" -- so a
    workflow whose every task is genuinely done can never derive
    "completed" while one such tombstone survives. Observed live:
    workflow 72ed4df8's development phase (order 5) failed once on
    2026-08-30, succeeded on four later goto-retries over the following
    two days, and its PhaseExecution sat "failed" the entire time --
    silently blocking that feature (and the next one queued behind it)
    from ever completing.

    Excludes the source phase's own execution (the one that just fired
    the goto) — its "completed" mark from mark_phase_complete must
    survive so the idempotency guard ("if execution.status == completed:
    skip") works on the next evaluation.

    Also clears task_creation_claimed_at and started_at so the phase's
    next cycle starts clean — a stale started_at makes every later
    cycle-scoped query (Task.created_at >= started_at) exclude the
    phase's own freshly-created task, silently stalling it forever.

    Live-task handling splits by how the phase relates to the goto target:

    - order == target_phase_order with a live task: left entirely alone
      (not reset). This is the goto's OWN target -- a live task there is
      the correct current work, not something stale. Protects against a
      REDUNDANT goto evaluation of the SAME already-handled completion (a
      distinct race: mark_phase_complete can get entered twice for one
      task completion, e.g. once from fire_spec_gate_if_ready's synchronous
      path and once from the periodic sweep) blindly wiping the
      target-phase task's started_at/status mid-flight. Observed live: a
      development-phase task legitimately in_progress had its
      PhaseExecution reset to started_at=None by a second, redundant
      "goto development" from adversarial_review; started_at was later
      re-derived from an unrelated duplicate task, permanently excluding
      the real task from the cycle-scoped completion check and stalling
      the phase forever.

    - order > target_phase_order with a live task: the pipeline is
      rewinding PAST this phase, so its agent is validating/reviewing code
      that is about to be rewritten under it -- and worse, sharing the
      feature worktree with the target-phase agent about to be dispatched.
      That agent is terminated (DB invariant via engine_client.terminate_agent
      -- resets its task to pending, orphan_reaper does the tmux teardown)
      and the execution is reset like any other stale one. Observed live:
      a qa_validation agent (order 9) ran concurrently with a development
      agent (order 5) for ~an hour on workflow 72ed4df8 after a
      goto-to-development, both mutating the same worktree.

    Returns the number of rows reset.
    """
    stale_rows = (
        db.query(PhaseExecution, Phase.order)
        .join(Phase, PhaseExecution.phase_id == Phase.id)
        .filter(
            Phase.workflow_id == workflow_id,
            Phase.order >= target_phase_order,
            PhaseExecution.phase_id != exclude_phase_id,
            PhaseExecution.status.in_(["in_progress", "completed", "failed"]),
        )
        .all()
    )
    stale = [pe for pe, _ in stale_rows]
    order_by_exec_id = {id(pe): order for pe, order in stale_rows}
    if stale:
        LIVE_STATUSES = ["assigned", "in_progress", "queued", "blocked", "needs_work", "under_review"]
        live_by_phase: Dict[str, list] = {}
        for phase_id, task_id, agent_id in (
            db.query(Task.phase_id, Task.id, Task.assigned_agent_id)
            .filter(
                Task.phase_id.in_([s.phase_id for s in stale]),
                Task.status.in_(LIVE_STATUSES),
            )
            .all()
        ):
            live_by_phase.setdefault(phase_id, []).append((task_id, agent_id))

        kept = []
        for s in stale:
            live_tasks = live_by_phase.get(s.phase_id)
            if not live_tasks:
                kept.append(s)
                continue
            if order_by_exec_id[id(s)] <= target_phase_order:
                # The goto's own target phase already has the correct live
                # work -- leave it (and its execution) untouched.
                continue
            # A strictly-later phase is being rewound past: kill its agent
            # so it stops racing the incoming target-phase agent on the
            # shared worktree, then let this execution reset below.
            from src.autopilot.orchestrator.engine_client import terminate_agent

            for _task_id, agent_id in live_tasks:
                if agent_id:
                    terminate_agent(
                        agent_id,
                        reason=f"superseded: pipeline goto rewound to order {target_phase_order}, past this phase",
                        session=db,
                    )
            db.flush()
            kept.append(s)
        stale = kept
    for s in stale:
        s.status = "pending"
        s.completed_at = None
        s.task_creation_claimed_at = None
        s.started_at = None
    if stale:
        db.commit()
    return len(stale)


def mark_skipped_over_phases(db, workflow_id: str, from_order: int, to_order: int, logger: "OrchestratorLogger") -> None:
    """Downgrade "pending" PhaseExecutions strictly between from_order and
    to_order to "skipped" when a jump (goto/retry action_target_phase, or a
    successor pick that lands past intervening phases) advances the
    pipeline past them.

    Extracted from _start_next_phase (phase_manager.py), which had this
    logic for its OWN jump but _case_completed_with_successor's identical
    jump (below) had no equivalent -- leaving any phase it jumped over
    stuck "pending" forever, since nothing else ever downgrades it and
    derive_workflow_status's completeness check treats "pending" as real
    work remaining. Observed live: workflow c1f0839c's design_review
    (order 4) sat "pending" from 2026-08-23 after a goto jumped
    architecture_design (order 3) straight to development (order 5) via
    this function, silently blocking the workflow from ever completing or
    pausing for review even after every phase that actually needed to run
    (through deploy, order 14) had finished.

    Only downgrades "pending" -- a genuinely "completed" phase (from an
    earlier pass this jump doesn't need to redo) must not get overwritten.
    """
    skipped_phases = (
        db.query(Phase)
        .filter(
            Phase.workflow_id == workflow_id,
            Phase.order > from_order,
            Phase.order < to_order,
        )
        .all()
    )
    for sp in skipped_phases:
        sp_execution = db.query(PhaseExecution).filter_by(phase_id=sp.id).first()
        if sp_execution and sp_execution.status == "pending":
            logger.info(
                f"[PHASE] {sp.name} skipped over by a jump from order "
                f"{from_order} to order {to_order} -- marking its "
                "PhaseExecution 'skipped' instead of leaving it 'pending' forever"
            )
            sp_execution.status = "skipped"
            sp_execution.completed_at = utc_now()
    if skipped_phases:
        db.commit()


def reopen_phase_execution(
    execution: PhaseExecution,
    *,
    status: str,
    started_at: Literal["clear", "now", "leave"] = "leave",
) -> None:
    """Reopen a PhaseExecution for a fresh cycle: write status and reset
    the one-time-per-cycle task-creation claim together.

    Extracted from 4 independent hand-copies of this exact write (see
    docs/AUTOPILOT_REFACTOR_PLAN.md §4.1's "4th copy-family" note) --
    without the reset, a reopened phase finds task_creation_claimed_at
    already set from the prior cycle and never gets a fresh task created.

    started_at is a third, independent axis this function does not
    decide -- it only applies whichever the caller already determined is
    correct for its own reopen reason (a retry cycle should start its
    own clock fresh; arbitration/restart reopen a phase that was already
    running and must not understate how long it's been open; a fresh
    "next phase" start should stamp now). Modeled as a 3-way choice, not
    a bool, because it is genuinely 3 distinct behaviors, not one
    optional feature.

    Deliberately does NOT decide *whether* to reopen, or what `status`
    should be -- those differ meaningfully per call site (e.g.
    arbitration must land on "in_progress", never "pending", or
    _case_completed_with_successor's next-pending-phase-by-order picking
    silently skips it -- see that call site's own comment) and are
    call-site business logic, not part of the duplicated write.
    """
    execution.status = status
    execution.task_creation_claimed_at = None
    if started_at == "clear":
        execution.started_at = None
    elif started_at == "now":
        execution.started_at = utc_now()
    # "leave": no-op, by design -- not every reopen should touch it.


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
        # Case-insensitive: the three writers disagree on capitalisation --
        # features.py and _create_corrective_task write "Orphaned: ...",
        # mechanical_recovery.py writes "Agent orphaned - tmux session not
        # found". A capital-only match silently exempted the first two from
        # the retry cap and not the third, for the same condition.
        is_orphan = "orphaned" in (task.get("failure_reason") or "").lower()

        # git_expert/doc_review can't fix code -- verify_no_open_tickets
        # (task_completion/verification.py) rejects their "done" call with
        # "open bug ticket(s)" and leaves the task failed, but retrying
        # either phase in place just repeats the identical rejection
        # forever: neither one can resolve a bug ticket itself. Route
        # straight to development instead -- the phase equipped to fix
        # it -- the same way product_validation's own spec-gate already
        # does for unmet requirements. Observed live: workflow ca539a75's
        # git_expert task burned 5 retries against the same open ticket
        # before landing permanently failed with no forward path.
        failure_reason = task.get("failure_reason") or ""
        if phase_id and "open bug ticket" in failure_reason.lower():
            with get_db() as _db_phase:
                _phase = _db_phase.query(Phase).filter_by(id=phase_id).first()
            if _phase and _phase.name in ("git_expert", "doc_review"):
                with get_db() as _db_dev:
                    dev_phase = (
                        _db_dev.query(Phase)
                        .filter_by(workflow_id=workflow_id, name="development")
                        .first()
                    )
                if not dev_phase:
                    logger.warning(
                        f"  Task {task_id[:8]} ({_phase.name}) blocked by an open "
                        "bug ticket, but this workflow has no development phase "
                        "to route back to -- falling through to normal retry"
                    )
                else:
                    logger.info(
                        f"  Task {task_id[:8]} ({_phase.name}) blocked by an open "
                        "bug ticket -- routing to development instead of "
                        "retrying in place"
                    )
                    _create_phase_task(
                        workflow_id, dev_phase.id, "development", "goto", logger,
                        feedback=failure_reason, source_phase_name=_phase.name,
                    )
                    # Must leave "failed" -- otherwise this same still-
                    # "failed" task matches get_tasks(status="failed")
                    # again on the very next sweep tick and fires ANOTHER
                    # goto, forever, 15s apart, with no code change ever
                    # able to stop it. Same "superseded, stop tracking"
                    # convention this function's own sibling-task check
                    # above already uses. Observed live: 7 duplicate goto-
                    # to-development tasks in 8 minutes before this was
                    # caught and the source task manually marked
                    # "duplicated" to break the loop.
                    with get_db() as _db_consume:
                        _t_consume = _db_consume.query(Task).filter_by(id=task_id).first()
                        if _t_consume and _t_consume.status == "failed":
                            _t_consume.status = "duplicated"
                            _t_consume.failure_reason = (
                                "Routed to development via goto to resolve blocking "
                                "ticket(s); this task itself is not being retried"
                            )
                            # This phase's own PhaseExecution is still
                            # "in_progress" from the run that just failed --
                            # reset it to "pending" so development's later
                            # goto back to this phase name can find it via
                            # _case_completed_with_successor's pending-list
                            # search. See the identical fix (and its own
                            # "observed live" note) in
                            # _maybe_retry_failed_tasks's sibling branch.
                            _execution = _db_consume.query(PhaseExecution).filter_by(phase_id=phase_id).first()
                            if _execution:
                                reopen_phase_execution(_execution, status="pending", started_at="clear")
                            _db_consume.commit()
                    continue

        # Read max_task_retries from workflow config, default to 5
        from src.autopilot.spec import get_max_task_retries

        max_retry = get_max_task_retries(workflow_id)
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
            # Reset task status to pending. Checked, not fire-and-forget:
            # update_task_status swallows its own DB errors and returns
            # False rather than raising -- previously that False was
            # discarded here, so a transient failure to reset the status
            # (task.status still "failed" in the DB) didn't stop the next
            # line from dispatching a live agent anyway. Every downstream
            # consistency check keying off Task.status (phase completion
            # counts, this same function's own retry-candidate query,
            # _clean_stale_assigned_tasks) could then double-dispatch or
            # misclassify the task while a real agent was mid-work on it.
            # Raising here routes the failure into this block's own
            # existing except below, which already does the right thing:
            # log it and leave/revert the task to "failed" for another
            # retry pass to pick up.
            if not update_task_status(task_id, "pending"):
                raise RuntimeError(
                    f"Failed to reset task {task_id[:8]} to pending before retry"
                )
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
                        _t2.started_at = utc_now()
                        _db4.commit()
            except Exception as e3:
                logger.error(f"  Agent {agent_id[:8]} created for task {task_id[:8]} but failed to link it to the task row: {e3}")
                # Left "pending" with assigned_agent_id still None, this task
                # is invisible to every sweep: this function only re-queries
                # status="failed", _clean_stale_assigned_tasks's terminated-
                # agent pass requires assigned_agent_id isnot(None), and
                # mechanical_recovery's detectors look their task up by a
                # live agent id. Revert to "failed" (same terminal state the
                # outer except below uses) so the next sweep tick retries it
                # -- "Orphaned:" prefix matches this function's own is_orphan
                # check above, so the real, still-live agent_id doesn't burn
                # this task's retry budget for a DB write failure that had
                # nothing to do with the agent's own work.
                try:
                    with get_db() as _db5:
                        _t3 = _db5.query(Task).filter_by(id=task_id).first()
                        if _t3 and _t3.status == "pending":
                            _t3.status = "failed"
                            _t3.failure_reason = f"Orphaned: agent {agent_id[:8]} created but failed to link to task row: {e3}"
                            _db5.commit()
                except Exception as e4:
                    logger.error(f"  Failed to revert task {task_id[:8]} to failed after link failure: {e4}")
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


def _retry_exhausted_failed_workflows(logger: "OrchestratorLogger") -> int:
    """Self-heal for a workflow failed by a phase exhausting its retry cap
    (_phase_case_steps.py's "all failed tasks past retry cap" branch, which
    sets both PhaseExecution.status="failed" and Workflow.status="failed").

    That state had no automated path back at all. Every other recovery in
    this sweep is scoped away from it:
      - _retry_exhausted_paused_workflows selects status=="paused" only.
      - _recover_abandoned_workflows_with_completed_phase requires
        status_reason LIKE "Abandoned:%" AND an in_progress phase -- the
        exhausted phase's own execution is "failed", so neither matches.
      - Every _advance_phases dispatch case filters phase_statuses to
        exactly "pending"/"in_progress"/"completed", so a "failed"
        execution is invisible to all four.
    The only way out was a human clicking Resume (resume_feature), which
    also happens to be the only caller that resets the phase execution. A
    fully automated pipeline cannot depend on that.

    Bounded exactly like its paused sibling, and deliberately sharing that
    policy's two knobs rather than inventing a second set: a cooldown since
    the phase actually failed (the execution's own completed_at, the
    moment of exhaustion -- there is no paused_at on a failed workflow),
    and a hard cap on how many times one workflow gets this second chance
    (paused_workflow_max_retry_cycles via Workflow.paused_retry_count).
    Once the cap is hit the workflow is left alone permanently, with
    status_reason saying so, exactly as the paused path does -- automation
    that retries forever is the tight-loop problem the retry cap exists to
    prevent.

    Recovery itself is just reset_failed_phase_executions + status="active":
    that makes the stuck phase visible to the normal dispatch cases again,
    and they re-enter it on the very next tick. This function deliberately
    does not evaluate or grade anything -- same division of labour as
    _recover_abandoned_workflows_with_completed_phase.
    """
    from src.autopilot.orchestrator import (
        _get_paused_workflow_max_retry_cycles,
        _get_paused_workflow_retry_cooldown_seconds,
    )
    from src.autopilot.orchestrator.engine_client import reset_failed_phase_executions

    max_cycles = _get_paused_workflow_max_retry_cycles()
    cutoff = utc_now() - timedelta(seconds=_get_paused_workflow_retry_cooldown_seconds())
    recovered = 0
    with get_db() as db:
        candidates = (
            db.query(Workflow)
            .filter(
                Workflow.status == "failed",
                Workflow.status_reason.like("%exhausted the retry cap%"),
            )
            .all()
        )
        for wf in candidates:
            # Same superseded-workflow guard as the paused sibling: a
            # per-feature workflow whose Feature no longer points back at it
            # has been replaced by a later attempt, and resuming it
            # resurrects already-dead work.
            if wf.definition_id not in PHASE0_DEFINITION_IDS:
                if not db.query(Feature).filter_by(workflow_id=wf.id).first():
                    continue

            if (wf.paused_retry_count or 0) >= max_cycles:
                if "auto-retry gave up" not in (wf.status_reason or ""):
                    logger.warning(
                        f"[WORKFLOW-RECOVERY] Workflow {wf.id[:8]} exhausted "
                        f"{max_cycles} auto-retry cycles for its retry-capped "
                        "phase -- giving up permanently, needs a manual resume"
                    )
                    wf.status_reason = (
                        f"{wf.status_reason or ''} (auto-retry gave up after "
                        f"{max_cycles} attempts -- manual resume required)"
                    )
                    recovered += 1
                continue

            # Cooldown anchored on when the phase actually failed. A
            # workflow with no failed execution left doesn't belong to this
            # function at all (something already healed it).
            failed_at = (
                db.query(PhaseExecution.completed_at)
                .join(Phase, PhaseExecution.phase_id == Phase.id)
                .filter(Phase.workflow_id == wf.id, PhaseExecution.status == "failed")
                .order_by(PhaseExecution.completed_at.desc())
                .limit(1)
                .scalar()
            )
            if failed_at is None:
                continue
            if failed_at > cutoff:
                continue  # still cooling down

            n = reset_failed_phase_executions(wf.id, session=db)
            if not n:
                continue

            wf.status = "active"
            wf.paused_by = None
            wf.paused_at = None
            wf.paused_retry_count = (wf.paused_retry_count or 0) + 1
            wf.status_reason = None
            for feat in (
                db.query(Feature)
                .filter_by(workflow_id=wf.id)
                .filter(Feature.status.in_(["paused", "failed"]))
                .all()
            ):
                feat.status = "active"
            logger.warning(
                f"[WORKFLOW-RECOVERY] Workflow {wf.id[:8]} was failed by a "
                f"retry-capped phase -- reset {n} failed phase execution(s) "
                f"and resumed (cycle {wf.paused_retry_count}/{max_cycles}); "
                "the normal dispatch cases re-enter the phase on the next tick"
            )
            recovered += 1
        if recovered:
            db.commit()
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
    from sqlalchemy import or_

    from src.autopilot.orchestrator import _get_paused_workflow_max_retry_cycles, _get_paused_workflow_retry_cooldown_seconds

    max_cycles = _get_paused_workflow_max_retry_cycles()
    cutoff = utc_now() - timedelta(seconds=_get_paused_workflow_retry_cooldown_seconds())
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
            # A per-feature workflow (not Phase 0, which creates Feature
            # rows rather than being linked from one) whose Feature no
            # longer points back at it has been superseded by a later,
            # separately-created retry attempt for the same feature --
            # resuming it resurrects already-dead work instead of the
            # design's real, current attempt. Observed live: a design
            # completed successfully hours later via a third workflow: the
            # first two failed attempts sat paused (missing worktree,
            # never rebuilt -- this function doesn't rebuild worktrees,
            # only _recover_abandoned_workflows_missing_worktree does),
            # and once their cooldown passed, got resumed anyway,
            # immediately failed again the same way, and looked to the
            # user like the already-finished design "started processing
            # again by itself".
            if wf.definition_id not in PHASE0_DEFINITION_IDS:
                if not db.query(Feature).filter_by(workflow_id=wf.id).first():
                    continue

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
                    pid for (pid,) in db.query(PhaseExecution.phase_id).join(
                        Phase, PhaseExecution.phase_id == Phase.id
                    ).filter(
                        Phase.workflow_id == wf.id,
                        PhaseExecution.status == "in_progress",
                    ).all()
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
                # Sync feature status -- this function bypasses
                # resume_workflow() (which has its own cascade), so the
                # feature row stays "paused" in the DB and the UI's
                # review-mode highlight misses it. Observed live:
                # worktree-manager-parameterization on proj-540541ed.
                for feat in db.query(Feature).filter_by(workflow_id=wf.id, status="paused").all():
                    feat.status = "active"
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
            # Sync feature status -- same reasoning as the system-exhausted
            # branch above.
            for feat in db.query(Feature).filter_by(workflow_id=wf.id, status="paused").all():
                feat.status = "active"
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
                        Task.status.in_(["pending", "queued", "blocked", "in_progress", "assigned"]),
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
                if wf.status == "paused" and wf.paused_by != "review":
                    return False  # Still paused, nothing to do

                # paused_by="review" is also used for the final human-
                # review gate (PhaseManager._complete_workflow, once every
                # phase including git_expert has finished) -- by that
                # point there's nothing left to advance anyway, but this
                # carve-out stays generically correct for any paused_by=
                # "review" state: unrelated in-progress phases must keep
                # advancing/self-healing normally rather than a workflow-
                # wide pause silently freezing them too. Observed live:
                # task a1efdda6 (adversarial_review, phase 6) sat orphaned
                # and was never retried while an unrelated phase's own
                # review-mode pause blocked this whole function. Other
                # pause reasons (user, budget, system) are genuine full
                # stops and still hard-return above.

            # Self-heal any abandoned task-creation claim before reading
            # phase statuses below, so the dispatch that follows sees the
            # repaired state, not a claim-blocked snapshot.
            _release_stale_task_creation_claims(db, workflow_id, logger)
            # Same reasoning: a phase stuck "pending" despite a done task
            # is invisible to every dispatch case below otherwise.
            _release_pending_phases_with_done_tasks(db, workflow_id, logger)
            # Sibling repair: a phase stuck "pending" despite an existing
            # non-terminal (orphaned) task -- same blind spot, a task that
            # never reached "done" instead of one that did.
            _release_pending_phases_with_orphaned_task(db, workflow_id, logger)
            # Third sibling, and the widest of the three: a phase whose
            # execution is "failed" while the workflow itself is active.
            # Every dispatch case below filters phase_statuses to exactly
            # "pending"/"in_progress"/"completed", so a "failed" execution
            # is invisible to all four -- the phase can never be entered
            # again, and the workflow can never derive "completed" either
            # (derive_workflow_status requires every execution to be
            # "completed"/"skipped"). An ACTIVE workflow with a failed
            # execution is unambiguously wrong: resuming a workflow means
            # "try this again", and every un-fail path in the codebase
            # resets the workflow and its tasks but historically not this
            # row. Unconditional, with no cooldown, precisely because the
            # workflow is already active -- the bounded-retry policy in
            # _retry_exhausted_failed_workflows governs whether a FAILED
            # workflow gets resumed at all; once it is active, leaving one
            # of its phases invisible serves nothing. Observed live:
            # workflow 72ed4df8 sat active-with-a-failed-development-
            # execution for a full day, its work advancing through direct
            # goto dispatch the whole time while its feature could never
            # report completed.
            if wf.status == "active":
                from src.autopilot.orchestrator.engine_client import reset_failed_phase_executions

                healed = reset_failed_phase_executions(workflow_id, session=db)
                if healed:
                    logger.warning(
                        f"[PHASE-ADVANCE] Reset {healed} failed phase execution(s) on "
                        f"active workflow {workflow_id[:8]} -- a failed execution is "
                        "invisible to every dispatch case, so the phase could never "
                        "be re-entered"
                    )
                    db.commit()

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

            # First-match-wins chain. SOLID review 2.8 flagged that nothing
            # here NAMES the priority order, leaving it "enforced only by
            # convention" -- so here is what actually enforces it, checked
            # case by case rather than assumed. **It is the guards, not the
            # ordering, that make this correct**: no reordering of these four
            # changes behaviour today.
            #
            #  - _case_start_first_phase requires (not in_progress and not
            #    completed and pending); _case_completed_with_successor
            #    requires (completed and pending and not in_progress). They
            #    are mutually exclusive on `completed`, and BOTH are inert
            #    whenever any in_progress phase exists. That last property is
            #    load-bearing beyond this function: it is what makes the
            #    arbitration path's _reopen_phase_execution(status=
            #    "in_progress") genuinely protective rather than illusory,
            #    since a phase parked awaiting arbitration cannot be raced
            #    past by _case_completed_with_successor.
            #
            #  - _case_in_progress_no_tasks and _case_in_progress_complete
            #    both iterate in_progress, so they are the one pair that
            #    could in principle conflict -- except the second handles the
            #    task-less case itself (`if total_cycle_tasks == 0`), against
            #    a cycle-scoped count rather than an all-time one. Swapping
            #    them changes WHICH case dispatches, not WHETHER a task is
            #    dispatched.
            #
            # Deliberately not converted to a uniform dispatch table: the
            # four cases take different subsets of pending/completed/
            # in_progress, so a shared signature would mean widening all of
            # them -- a larger change than the (currently zero) risk justifies.
            # If you add a fifth case, re-derive the guard analysis above
            # rather than assuming position in this list protects you.

            # Case 0 and Case 1 dispatch NEW downstream work (starting the
            # first phase, or advancing to a successor phase) rather than
            # healing an already-dispatched one -- unlike Case 0b/Case 2
            # below, which only ever act on a phase already `in_progress`.
            # The paused_by="review" carve-out above exists specifically for
            # that narrower self-heal case (an unrelated in-progress phase
            # orphaned while paused), not for resuming forward progress.
            # Skipping these two here closes the gap _start_next_phase's own
            # paused_by check (phase_manager.py) can't reach on its own:
            # reset_stale_executions_on_goto resets downstream
            # PhaseExecutions to "pending" whenever a goto/retry action
            # fires, with no paused_by check of its own -- so a review-paused
            # workflow's last phase (e.g. deploy) recording a stale goto
            # still produces a `pending` successor, and Case 1 would
            # otherwise dispatch a fresh task for it every sweep tick.
            # Observed live: workflow e6437c3f kept re-running its entire
            # qa_validation -> ... -> deploy tail every ~6 minutes for hours
            # after being paused for human review, via exactly this path.
            if wf.paused_by != "review":
                # Case 0: No in-progress phase and first phase is pending — start it
                result = _case_start_first_phase(db, workflow_id, pending, in_progress, completed, logger)
                if result is not None:
                    return result

            # Case 0b: In-progress phase with no tasks at all
            result = _case_in_progress_no_tasks(db, workflow_id, in_progress, logger)
            if result is not None:
                return result

            if wf.paused_by != "review":
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
                from src.autopilot.orchestrator.engine_client import resume_workflow

                # force=False: the narrowing check above already confirmed
                # paused_by is None or "system", which resume_workflow's
                # own default narrowing accepts -- redundant but keeps this
                # call correct even if the check above is ever loosened.
                resume_workflow(workflow_id, session=db)
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

    Uses utc_now(), matching _claim_phase_task_creation's writer and
    every other timestamp in this codebase -- datetime.now() (ambient local
    time) here previously meant a claim's staleness depended on whatever
    TZ the process happened to be running under at the moment it compared,
    not real elapsed time. Observed live: a claim set hours earlier under a
    UTC-flavored clock never registered as stale against a later process's
    local-time now(), because the raw naive values didn't share a clock to
    compare against -- the workflow stayed silently stuck indefinitely,
    invisible to this self-heal despite being its exact intended case.
    """
    stale_cutoff = utc_now() - timedelta(seconds=CLAIM_STALE_TIMEOUT_SECONDS)
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
        phase_label = phase.name if phase else execution.phase_id
        # An arbiter dispatch reuses this same claim to mark "arbitration in
        # flight" (see _phase_has_arbitration_in_flight's docstring) and can
        # legitimately run past CLAIM_STALE_TIMEOUT_SECONDS -- clearing the
        # claim out from under it drops the arbiter's eventual decision the
        # same way the millisecond-scale race this self-heal exists for did.
        # Leave it alone; _maybe_resolve_arbitration's own dead-agent self-heal
        # (arbitration.py) owns cleanup for a genuinely stuck arbiter.
        if _phase_has_arbitration_in_flight(db, execution.phase_id):
            logger.warning(
                f"[PHASE-ADVANCE] {phase_label}: task_creation_claimed_at is stale but arbitration is still in flight -- leaving claim in place"
            )
            continue
        latest_task = db.query(Task).filter_by(phase_id=execution.phase_id).order_by(Task.created_at.desc()).first()
        logger.warning(
            f"[PHASE-ADVANCE] {phase_label}: task_creation_claimed_at held with no release -- clearing stale claim ({'task exists' if latest_task else 'no task yet'})"
        )
        _clear_stale_task_creation_claim(db, execution.phase_id, repair_status=True)


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


def _release_pending_phases_with_orphaned_task(db, workflow_id: str, logger: "OrchestratorLogger") -> None:
    """Self-heal for a PhaseExecution stuck at status="pending" despite
    already having a non-terminal (pending/assigned/in_progress/queued)
    Task pointing at it -- the task's mere existence is proof something
    already meant to work this phase (most commonly a goto/retry reset
    that reverted the PhaseExecution without flipping it back), but no
    dispatch case recognizes a "pending" phase that already has a task:
    Case 0/0b act on a *lack* of tasks, Case 1 needs the *predecessor*
    completed and skips outright if the successor already has a task, and
    Case 2 only ever looks at phases already "in_progress".

    Sibling to _release_pending_phases_with_done_tasks (same blind spot, a
    non-terminal task instead of a done one) -- kept separate rather than
    merged into it because that function's "skip entirely if ANY phase is
    already in_progress" guard doesn't hold here: a manual-only phase
    (git_expert) sitting "in_progress" only because it's paused
    waiting on a human, with its own task already failed (not actively
    consuming an agent), must not block this repair for an unrelated
    phase behind it. Guards against real concurrency instead by checking
    for an actually-live task (assigned/in_progress) anywhere in the
    workflow, not merely a PhaseExecution.status column.

    Observed live: development (task 66e7c1ff) sat "pending" -- reverted
    by an earlier goto cycle -- for the entire time its workflow was
    paused for git_expert review, invisible to every dispatch case,
    because _release_pending_phases_with_done_tasks only matches a done
    task and unconditionally skips whenever any phase (including the
    parked git_expert one) is "in_progress".
    """
    live_task = (
        db.query(Task)
        .join(Phase, Task.phase_id == Phase.id)
        .filter(Phase.workflow_id == workflow_id, Task.status.in_(["assigned", "in_progress"]))
        .first()
    )
    if live_task:
        return

    orphaned_task = (
        db.query(Task)
        .join(Phase, Task.phase_id == Phase.id)
        .join(PhaseExecution, PhaseExecution.phase_id == Phase.id)
        .filter(
            Phase.workflow_id == workflow_id,
            PhaseExecution.status == "pending",
            Task.status.in_(["pending", "assigned", "in_progress", "queued"]),
            ~Task.raw_description.like(f"{DIAGNOSTIC_TASK_PREFIX}%"),
        )
        .order_by(Task.created_at.desc())
        .first()
    )
    if not orphaned_task:
        return

    execution = db.query(PhaseExecution).filter_by(phase_id=orphaned_task.phase_id).first()
    if not execution or execution.status != "pending":
        return

    phase = db.query(Phase).filter_by(id=execution.phase_id).first()
    logger.warning(
        f"[PHASE-ADVANCE] {phase.name if phase else execution.phase_id}: "
        f"PhaseExecution stuck 'pending' despite existing task "
        f"{orphaned_task.id[:8]} (status={orphaned_task.status}) -- "
        "flipping to in_progress so dispatch can see it"
    )
    execution.status = "in_progress"
    execution.started_at = execution.started_at or orphaned_task.created_at
    db.commit()


# Step 1 of docs/designs/PHASE_EXECUTION_STATE_MACHINE_REFACTOR.md: detect
# PhaseExecution/real-state drift and log it immediately, without changing
# any write path yet. "pending" is included alongside the other live
# statuses: a task that exists at all but hasn't been picked up yet is
# still real, pending work under this phase (see
# _release_pending_phases_with_orphaned_task above's own "never dispatched
# to an agent, stale >1min" case) -- omitting it would make this detector
# blind to exactly the failure mode that self-heal exists for.
LIVE_TASK_STATUSES = ["pending", "assigned", "in_progress", "queued", "blocked", "needs_work", "under_review"]


def find_phase_execution_drift(db, workflow_id: str) -> list:
    """Any phase with a live task whose PhaseExecution isn't "in_progress"
    is drift -- returns (Phase, PhaseExecution, Task) triples for the
    caller to debounce and log. Does not fix anything; detection only.
    """
    return (
        db.query(Phase, PhaseExecution, Task)
        .join(PhaseExecution, PhaseExecution.phase_id == Phase.id)
        .join(Task, Task.phase_id == Phase.id)
        .filter(
            Phase.workflow_id == workflow_id,
            Task.status.in_(LIVE_TASK_STATUSES),
            PhaseExecution.status != PhaseExecutionStatus.IN_PROGRESS,
        )
        .all()
    )


def find_stuck_active_workflows(db) -> list:
    """A workflow marked "active" with any "failed" PhaseExecution is stuck
    by definition -- nothing in _advance_phases's four dispatch cases will
    ever look at a "failed" execution, live task or not. Catches the exact
    shape of the tombstone bug (a done task, a "failed" execution, no live
    task for find_phase_execution_drift to key off), the single
    highest-cost failure mode this refactor's own history produced.
    Workflow-wide (not scoped to one workflow_id) -- callers run it once
    per sweep tick, not once per workflow.
    """
    return (
        db.query(Workflow, Phase, PhaseExecution)
        .join(Phase, Phase.workflow_id == Workflow.id)
        .join(PhaseExecution, PhaseExecution.phase_id == Phase.id)
        .filter(
            Workflow.status == "active",
            PhaseExecution.status == PhaseExecutionStatus.FAILED,
        )
        .all()
    )


# Debounce state for find_phase_execution_drift: module-level, in-memory,
# reset on restart -- same tradeoff orphan_reaper.py's _first_seen_orphan
# accepts for the same reason (a false positive here costs nothing but a
# log line; a missed detection self-heals on the next tick regardless).
# A task legitimately spends a few hundred milliseconds "pending" before
# _create_phase_task reopens its phase's execution to "in_progress" --
# logging on the first sighting would flag that normal window as drift.
# Require the SAME (phase_id, task_id) pair to still be present on a
# SECOND, later check before treating it as real. find_stuck_active_workflows
# needs no such debounce -- an active workflow with a failed execution is
# never a normal, momentary state.
#
# Keyed by workflow_id -- NOT a single shared set. The sweep calls this once
# per active/paused workflow per tick (background_loops.py), so a single
# global set would have every workflow's call clear() out whatever the
# PREVIOUS workflow in that same tick's iteration just recorded, and this
# workflow's own genuine, persistent drift would then never match on the
# next tick (it'd be compared against some other workflow's keys instead).
# With >1 monitored workflow -- the normal case -- that silently defeated
# the debounce entirely: real drift never reached "second sighting" and
# never got logged. Stale per-workflow entries for since-completed
# workflows are left in place rather than cleaned up -- bounded by total
# workflow count, resets on restart, not worth the extra bookkeeping.
_drift_previously_seen: Dict[str, set] = {}


def check_and_log_phase_execution_drift(db, workflow_id: str, logger: "OrchestratorLogger") -> None:
    """Debounced wrapper around find_phase_execution_drift -- call once per
    workflow per sweep tick. Logs a WARNING for drift confirmed present on
    two consecutive calls for THIS workflow; never raises, never writes
    anything."""
    current = find_phase_execution_drift(db, workflow_id)
    current_keys = {(phase.id, task.id) for phase, _execution, task in current}
    previously_seen = _drift_previously_seen.get(workflow_id, set())
    for phase, execution, task in current:
        key = (phase.id, task.id)
        if key in previously_seen:
            logger.warning(
                f"[PHASE-DRIFT] {phase.name} ({phase.id[:8]}): task {task.id[:8]} "
                f"is {task.status!r} but PhaseExecution.status is {execution.status!r}, "
                "not 'in_progress'"
            )
    # Keep only entries seen on THIS call for THIS workflow, so a mismatch
    # that resolves between ticks doesn't get logged if it later
    # (coincidentally) recurs -- each occurrence needs its own
    # two-in-a-row confirmation.
    _drift_previously_seen[workflow_id] = current_keys


def check_and_log_stuck_active_workflows(db, logger: "OrchestratorLogger") -> None:
    """Undebounced wrapper around find_stuck_active_workflows -- call once
    per sweep tick, not once per workflow. Logs a WARNING for every result;
    never raises, never writes anything."""
    for workflow, phase, execution in find_stuck_active_workflows(db):
        logger.warning(
            f"[PHASE-DRIFT] Workflow {workflow.id[:8]} is 'active' but phase "
            f"{phase.name} ({phase.id[:8]})'s PhaseExecution is 'failed' -- "
            "invisible to every _advance_phases dispatch case"
        )


_VALID_TRANSITIONS: Dict[str, set] = {
    PhaseExecutionStatus.PENDING: {PhaseExecutionStatus.IN_PROGRESS, PhaseExecutionStatus.SKIPPED},
    PhaseExecutionStatus.IN_PROGRESS: {PhaseExecutionStatus.COMPLETED, PhaseExecutionStatus.FAILED, PhaseExecutionStatus.PENDING},  # pending: goto rewind
    PhaseExecutionStatus.COMPLETED: {PhaseExecutionStatus.IN_PROGRESS, PhaseExecutionStatus.PENDING},  # goto re-entry redo
    PhaseExecutionStatus.FAILED: {PhaseExecutionStatus.IN_PROGRESS, PhaseExecutionStatus.PENDING},  # retry or un-fail
    PhaseExecutionStatus.SKIPPED: {PhaseExecutionStatus.IN_PROGRESS},  # goto sends work back through it
}

# Per-transition field resets, reconciled ONCE here instead of ad hoc at
# each of the ten existing call sites -- e.g. reset_stale_executions_on_goto
# today clears completed_at/started_at/task_creation_claimed_at when
# resetting to "pending"; _create_phase_task's reopen sets started_at="now"
# when moving to "in_progress" but leaves completed_at alone. Any (from, to)
# pair not listed here defaults to leaving started_at/completed_at/claim
# untouched -- reviewed against real call-site behavior during Step 3's
# migration of each site, not guessed in advance.
_FIELD_RESETS: Dict[Tuple[str, str], dict] = {
    (PhaseExecutionStatus.COMPLETED, PhaseExecutionStatus.PENDING): {"completed_at": None, "started_at": None, "task_creation_claimed_at": None},
    (PhaseExecutionStatus.FAILED, PhaseExecutionStatus.PENDING): {"completed_at": None, "started_at": None, "task_creation_claimed_at": None},
    (PhaseExecutionStatus.IN_PROGRESS, PhaseExecutionStatus.PENDING): {"completed_at": None, "started_at": None, "task_creation_claimed_at": None},
    (PhaseExecutionStatus.PENDING, PhaseExecutionStatus.IN_PROGRESS): {"started_at": "now", "task_creation_claimed_at": None},
    (PhaseExecutionStatus.COMPLETED, PhaseExecutionStatus.IN_PROGRESS): {"started_at": "now", "completed_at": None},
    (PhaseExecutionStatus.FAILED, PhaseExecutionStatus.IN_PROGRESS): {"started_at": "now", "completed_at": None},
    (PhaseExecutionStatus.SKIPPED, PhaseExecutionStatus.IN_PROGRESS): {"started_at": "now"},
    (PhaseExecutionStatus.PENDING, PhaseExecutionStatus.SKIPPED): {"completed_at": "now"},
}


def transition_phase_execution(
    db, phase_id: str, to_status: str, *, reason: str, extra_fields: Optional[dict] = None
) -> Optional[PhaseExecution]:
    """Atomically move phase_id's PhaseExecution to to_status. Returns the
    (freshly re-read) row on success, None if the row wasn't in a state
    this transition is valid from (someone else already moved it, or the
    caller's assumption about current state was wrong) -- callers treat
    None the same way _claim_phase_task_creation's False is treated today:
    skip, don't retry blindly, let the next sweep tick re-evaluate.

    extra_fields lets a call site atomically set fields this table doesn't
    otherwise touch (e.g. completion_summary) as part of the same UPDATE --
    added migrating _close_execution (Step 3), which sets status,
    completed_at, and an optional summary in one write. Only applied when
    the transition actually succeeds; a caller's summary must not land on
    a row this call didn't touch.

    Step 2 of docs/designs/PHASE_EXECUTION_STATE_MACHINE_REFACTOR.md --
    additive and not yet wired into any existing call site. See that
    document for why this must be a single atomic UPDATE (mirroring
    _claim_phase_task_creation) rather than SELECT-then-mutate: two
    concurrent callers could otherwise both read the same from_status,
    both see their transition as valid, and both write.
    """
    execution = db.query(PhaseExecution).filter_by(phase_id=phase_id).first()
    if execution is None:
        raise ValueError(f"No PhaseExecution for phase {phase_id}")
    from_status = execution.status
    allowed = _VALID_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        logger.error(
            f"[PHASE-TRANSITION] Invalid {from_status!r} -> {to_status!r} "
            f"for phase {phase_id} ({reason}) -- allowed: {sorted(allowed)}"
        )
        # Initial rollout: log and return None (treat as "not ours to make")
        # rather than raise, so a pre-existing bad state found on day one
        # doesn't turn into a hard outage the moment this ships. Escalate
        # to raising once Step 1's drift check has run clean for a while.
        return None

    values = {"status": to_status}
    for field, val in _FIELD_RESETS.get((from_status, to_status), {}).items():
        values[field] = utc_now() if val == "now" else val
    if extra_fields:
        values.update(extra_fields)

    # The atomic step: succeeds only if the row is STILL from_status right
    # now -- closes the exact race a SELECT-then-mutate would reopen.
    changed = (
        db.query(PhaseExecution)
        .filter(PhaseExecution.phase_id == phase_id, PhaseExecution.status == from_status)
        .update(values, synchronize_session=False)
    )
    db.commit()
    if changed == 0:
        logger.info(f"[PHASE-TRANSITION] Lost the race on phase {phase_id}: no longer {from_status!r} ({reason})")
        return None
    # This session has expire_on_commit=False (see database.py), so the
    # `execution` object loaded above is still cached in the identity map
    # with its pre-update attribute values -- a plain re-query would return
    # that same stale in-memory object rather than the row this call just
    # wrote. db.refresh() forces it to reload from the database.
    db.refresh(execution)
    return execution


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
    claimed_at = utc_now()
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
    utc_now() -- this function always runs strictly after that
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
    # "failed" included alongside pending/completed/skipped -- same gap as
    # _create_phase_task's reopen condition (fixed in 4d2f2005): a phase
    # execution stuck "failed" must still reopen once its guarded task
    # exists, or it stays invisible to every _advance_phases dispatch case.
    if execution.status in ("pending", "completed", "skipped", "failed"):
        execution.status = "in_progress"
        earliest_task = (
            db.query(Task)
            .filter_by(phase_id=phase_id)
            .order_by(Task.created_at.asc())
            .first()
        )
        execution.started_at = earliest_task.created_at if earliest_task else utc_now()
    execution.task_creation_claimed_at = None
    db.commit()


def _phase_has_arbitration_in_flight(db, phase_id: str) -> bool:
    """True if phase_id's most recent task is a still-running (not yet
    "done" or "failed") arbitration task.

    Shared by every caller that fires a phase transition and then
    unconditionally clears phase_id's task_creation_claimed_at claim once
    that call returns -- correct when the action was continue/goto (the
    claim's job was only to guard against concurrent re-evaluation), but
    wrong when the action was "arbitrate": _trigger_arbitration (invoked
    from inside that same transition call) deliberately reuses THIS SAME
    claim to mark "arbitration in flight" so _maybe_resolve_arbitration can
    find and act on the arbiter's eventual decision. Clearing it right
    away wipes that out within milliseconds of the arbiter being
    dispatched -- long before it can finish. Observed live (workflow
    a7695dc5): an arbiter's "continue" decision was silently dropped this
    way, and the workflow sat with zero agent activity for hours before
    the abandonment sweep failed it. "done"/"failed" are both already-
    resolved terminal states (a failed dispatch is handled by
    _trigger_arbitration's own force_action="fail" path, not left
    dangling) -- only a genuinely still-running status means the arbiter
    hasn't reported back yet and the claim must keep guarding it.
    """
    latest_task = (
        db.query(Task)
        .filter_by(phase_id=phase_id)
        .order_by(Task.created_at.desc())
        .first()
    )
    return (
        latest_task is not None
        and latest_task.created_by_agent_id == ARBITRATION_CREATED_BY
        and latest_task.status not in ("done", "failed")
    )


def _case_start_first_phase(db, workflow_id: str, pending: list, in_progress: list, completed: list, logger: "OrchestratorLogger") -> Optional[bool]:
    """Case 0: No in-progress phase and first phase is pending — start it.

    Returns None if this case doesn't apply, True/False otherwise.
    """
    if not in_progress and not completed and pending:
        first_phase = min(pending, key=lambda p: p["phase"].order)
        phase_id = first_phase["phase"].id
        # Check if it already has tasks
        existing = db.query(Task).filter_by(phase_id=phase_id).count()
        if existing == 0 and not _claim_phase_task_creation(db, phase_id):
            # Someone else (or a previous iteration of this same loop) is
            # already creating this phase's first task -- don't duplicate it.
            existing = 1
        if existing == 0:
            # Re-check immediately before creating, on a fresh query --
            # closes the TOCTOU gap between the count() above and winning
            # the claim. workflow_execution_routes.py's own initial-task
            # flow is independently claim-protected on the SAME phase, but
            # runs on a separate DB connection/session; a task it creates
            # can commit in the window between this function's initial
            # existing==0 read and its own claim succeeding, and the stale
            # snapshot would otherwise still look empty. Observed live: two
            # Task rows (ed82ce49, 83e86c54) for the same brand-new phase 1,
            # ~15s apart -- only one agent ever got dispatched (a separate,
            # working dedup check in create_agent_for_task_direct caught
            # that), but the extra Task row was pure debris left behind.
            #
            # If this re-check finds a task after all, this call is the one
            # holding the claim (a lost claim already set existing=1 above
            # and skipped this whole block) -- release it, since
            # _create_phase_task's own success path (which normally does
            # so) is never reached below.
            if db.query(Task).filter_by(phase_id=phase_id).count() > 0:
                _release_phase_task_creation_claim(db, phase_id)
                return None
        if existing == 0:
            logger.info(f"[PHASE-ADVANCE] Starting first phase: {first_phase['phase'].name}")
            return _create_phase_task(
                workflow_id,
                phase_id,
                first_phase["phase"].name,
                "continue",
                logger,
                target_already_claimed=True,
            )
    return None


def _correct_skewed_cycle_start(db, phase, execution, cycle_start, logger: "OrchestratorLogger"):
    """Guard against cycle_start drifting slightly LATER than the phase's
    own earliest real task, and return the (possibly corrected) cycle_start
    plus a matching cycle_filter tuple.

    started_at and a task's created_at are stamped by independent
    utc_now() calls (_start_phase, _release_phase_task_creation_claim,
    reopen_phase_execution, the task's own row insert) that can land a
    few milliseconds -- occasionally a couple of seconds, per
    _release_phase_task_creation_claim's own docstring -- apart in either
    order. When cycle_start lands after that task's stamp, every
    cycle-scoped query built from it (Task.created_at >= cycle_start)
    silently excludes that task forever: the caller's own "genuinely
    empty cycle" fresh-dispatch fallback correctly refuses to fire (an
    unscoped existence check still sees the task), but nothing else ever
    looks at it again either -- a live, real, retry-eligible task sits
    invisible while the phase stalls, or a fresh SECOND task gets created
    right next to it. Observed live twice, in two different callers:
    workflow 81b399c7's product_requirements phase (via
    _case_in_progress_complete) and workflow 2ee7f496's product_
    requirements phase (via _case_in_progress_no_tasks, which duplicated
    a UI-launched task 15s after it was created, because this correction
    hadn't been applied here yet).

    Deliberately bounded to a small grace window, NOT an unconditional
    re-anchor to the phase's overall earliest task: a goto/retry can
    leave started_at genuinely newer than a stale task from a much
    earlier cycle (minutes to hours prior) by design -- that case must
    keep treating the cycle as empty, not silently adopt the old task as
    if it belonged to the new cycle.
    """
    if not cycle_start:
        return cycle_start, ()
    skew_floor = cycle_start - timedelta(seconds=10)
    earliest_recent_task = (
        db.query(Task)
        .filter(Task.phase_id == phase.id, Task.created_at >= skew_floor)
        .order_by(Task.created_at.asc())
        .first()
    )
    if earliest_recent_task and earliest_recent_task.created_at < cycle_start:
        logger.warning(
            f"[PHASE-ADVANCE] {phase.name}'s cycle start ({cycle_start}) is "
            f"later than its own earliest task {earliest_recent_task.id[:8]} "
            f"({earliest_recent_task.created_at}), within clock-skew range -- "
            "correcting so cycle-scoped checks stop treating that task as invisible"
        )
        cycle_start = earliest_recent_task.created_at
        execution.started_at = cycle_start
        db.commit()
    return cycle_start, (Task.created_at >= cycle_start,)


def _case_in_progress_no_tasks(db, workflow_id: str, in_progress: list, logger: "OrchestratorLogger") -> Optional[bool]:
    """Case 0b: In-progress phase with no tasks at all.

    Workflow engine set it but didn't create task.
    Returns None if this case doesn't apply, True/False otherwise.
    """
    for ps in in_progress:
        phase = ps["phase"]

        # A LOWER-order phase also in_progress means a goto sent it back to
        # rework something THIS (later-order) phase found -- this pipeline
        # is otherwise strictly sequential, so two phases genuinely
        # in_progress at once only ever means that. This phase's own
        # in_progress status is stale residue from before the goto (it was
        # reset to "pending" by the routing code, e.g.
        # _maybe_retry_failed_tasks's ticket-blocked routing, or flipped
        # back to in_progress by an unrelated redundant re-evaluation of
        # an earlier phase's completion -- see that function's own
        # "Routed to development via goto" comment) -- not a fresh cycle
        # ready for a new dispatch. The earlier phase's own eventual goto
        # is what should re-target this phase (via
        # _case_completed_with_successor's pending-list search), not this
        # generic "in_progress with 0 tasks" fallback. Observed live:
        # workflow a7695dc5's doc_review got a second, premature dispatch
        # (task 7adafc03) while development (order 5) was still actively
        # reworking the exact ticket doc_review itself had just routed
        # there minutes earlier -- a wasted review pass against code that
        # hadn't been fixed yet.
        blocking_earlier = next(
            (p for p in in_progress if p["phase"].order < phase.order),
            None,
        )
        if blocking_earlier:
            logger.info(
                f"[PHASE-ADVANCE] {phase.name} (order {phase.order}) is "
                f"in_progress but so is {blocking_earlier['phase'].name} "
                f"(order {blocking_earlier['phase'].order}) -- skipping a "
                "fresh dispatch until the earlier phase's own goto "
                "re-targets it"
            )
            continue

        # Scoped to this phase's CURRENT cycle (Task.created_at >=
        # execution.started_at), matching _case_completed_with_successor's
        # own cycle_filter -- an unscoped count sees a stale, terminal task
        # from a PRIOR cycle (e.g. "duplicated", left behind when a ticket-
        # blocked git_expert/doc_review task got routed to development,
        # see _retry_failed_tasks/_maybe_retry_failed_tasks) and treats the
        # phase as "already has a task," permanently blocking a fresh
        # dispatch even though this cycle's own task count is genuinely
        # zero. Observed live: workflow b7bd02cc's git_expert phase sat
        # in_progress for hours with only a "duplicated" task from a
        # resolved ticket, deploy never budging, because this exact count
        # never dropped to zero.
        #
        # cycle_filter alone isn't a reliable belt: it depends on
        # execution.started_at actually being refreshed to a timestamp
        # AFTER the stale task the moment this phase re-enters
        # "in_progress" -- if anything sets status="in_progress" directly
        # without going through reopen_phase_execution/_create_phase_task's
        # own reopening logic (which always stamps started_at="now"), the
        # boundary stays stale and a "duplicated" task still satisfies
        # `>=` it forever. "duplicated" is this codebase's own established
        # convention for "does not count as this phase's real work" (see
        # _retry_failed_tasks's sibling check, _case_in_progress_complete's
        # own incomplete-count query) -- excluding it directly here closes
        # that gap regardless of why started_at didn't advance. Observed
        # live: workflow 81b399c7's git_expert phase stuck exactly this
        # way, cycle_filter notwithstanding.
        execution = ps.get("execution")
        cycle_start = execution.started_at if execution else None
        cycle_start, cycle_filter = _correct_skewed_cycle_start(db, phase, execution, cycle_start, logger)
        task_count = (
            db.query(Task)
            .filter(Task.phase_id == phase.id, Task.status != "duplicated", *cycle_filter)
            .count()
        )
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
            # Re-check immediately before creating, on a fresh query --
            # same TOCTOU gap _case_start_first_phase closed (see its own
            # comment): the task_count read above happens BEFORE the claim
            # attempt, so a task committed by another claim-protected path
            # on a separate DB session in that window would otherwise still
            # look like zero here.
            #
            # Deliberately UNSCOPED (no cycle_filter), matching
            # _case_start_first_phase's own re-check exactly -- this is a
            # last-resort safety net right before an unconditional create,
            # not a cycle-correctness decision (that's the job of the
            # task_count read above, which legitimately needs cycle_filter
            # to ignore a stale "duplicated" task from a PRIOR cycle). If
            # task_count's own cycle-scoped read is ever wrong for a
            # reason not yet understood, this net must not inherit the
            # same blind spot -- it exists specifically to catch "any task
            # committed since," full stop. Observed live: workflow
            # 0be376f2's product_requirements phase got a duplicate task
            # (abf3f36f) ~15s after the real one (d66b39ab) despite
            # started_at correctly anchored to d66b39ab's own created_at --
            # cycle_filter still somehow missed it, and this scoped
            # re-check inherited the identical miss instead of catching it
            # independently.
            if db.query(Task).filter(Task.phase_id == phase.id, Task.status != "duplicated").count() > 0:
                # We won the claim ourselves -- release it, since
                # _create_phase_task's own success path (which normally
                # does so) is never reached for this phase.
                _release_phase_task_creation_claim(db, phase.id)
                continue
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
        # Pick by MOST RECENT completion, not highest phase order. A
        # long-running, multi-cycle (goto-heavy) workflow can have a
        # downstream phase (e.g. forensics_analysis, order 12) still
        # sitting "completed" from many hours/cycles ago, while an
        # UPSTREAM phase (e.g. development, order 5) just NOW
        # re-completed via a goto loop. Picking by order landed on the
        # stale downstream phase's own action/action_target_phase
        # instead of the one that actually just fired, silently
        # dropping the real goto -- the workflow then stalls forever
        # with no case ever recognizing it needs to advance. completed_at
        # is only None for a completed execution that predates this
        # column, or in a test that doesn't set it; order is used as a
        # tiebreaker for that case, preserving the old behavior when
        # recency genuinely can't be determined (and matching every
        # existing test, which only ever has one completed phase at a
        # time -- order-vs-recency is unobservable there). Observed
        # live: workflow ca539a75's development phase (order 5) fixed a
        # ticket-blocked git_expert failure and goto'd back to
        # git_expert, but forensics_analysis (order 12, completed ~7
        # hours earlier) was picked instead -- its own unrelated
        # "continue" action bore no relation to development's goto, and
        # git_expert (the real successor) was never found, even though
        # it was sitting right there in `pending`.
        last_completed = max(
            completed,
            key=lambda p: (p["execution"].completed_at or datetime.min, p["phase"].order),
        )

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
        if successor is None:
            # last_completed is picked by MOST RECENT completion, which can
            # legitimately be a HIGHER-order phase than a phase still
            # sitting "pending" -- e.g. reset_failed_phase_executions
            # (engine_client.py) resets only the ONE phase whose execution
            # was stuck "failed", not everything downstream of it, so an
            # earlier-order phase can come back "pending" while a
            # later-order one is still "completed" from whatever run
            # actually finished the pipeline while the failed phase's own
            # bookkeeping never got closed out. The by-order search above
            # (order > last_completed's order) then finds nothing, and this
            # case falls through to `return None` as though there were
            # genuinely no pending work -- even with a fully actionable
            # pending phase sitting right there. Fall back to the
            # lowest-order pending phase in the whole workflow, exactly
            # what _case_start_first_phase picks for a brand-new one; the
            # existing_tasks/claim guards below are the same either way, so
            # this can't double-dispatch a phase that already has a live
            # cycle's task. Observed live: workflow 72ed4df8's development
            # (order 5) came back "pending" this way while deploy (order
            # 14) was still "completed" from the run that actually
            # finished -- _try_advance_phases returned False against the
            # real, current data with nothing further to explain why.
            successor = min(pending, key=lambda p: p["phase"].order, default=None)
            successor_action = "continue"
        if successor and successor["phase"].order > last_completed["phase"].order + 1:
            # Same jump-over-intermediate-phases case _start_next_phase's
            # own action_target_phase handling covers -- an explicit goto
            # target (above) or a by-order pick that lands past a phase
            # still sitting "pending" leaves it there forever otherwise.
            # See mark_skipped_over_phases for the full rationale. Observed
            # live: workflow c1f0839c's design_review (order 4) sat
            # "pending" from 2026-08-23 after this exact path jumped
            # architecture_design (order 3) straight to development
            # (order 5), permanently blocking derive_workflow_status's
            # completeness check even after deploy (order 14) finished.
            mark_skipped_over_phases(
                db, workflow_id, last_completed["phase"].order, successor["phase"].order, logger
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
            # 10s grace window on the boundary itself -- same clock-skew
            # class _correct_skewed_cycle_start guards against elsewhere
            # (independent utc_now() calls, here last_completed_execution's
            # own completed_at vs. the successor's first task's created_at,
            # can land a few milliseconds apart in either order). A bare
            # `>=` would otherwise exclude a successor task created moments
            # before completed_at was stamped, making this see 0 existing
            # tasks and create a duplicate. 10s is negligible against the
            # "weeks-old task" case this cycle_filter exists to exclude
            # (see the comment above) so it doesn't reopen that gap.
            last_completed_at = last_completed_execution.completed_at if last_completed_execution else None
            cycle_filter = (
                (Task.created_at >= last_completed_at - timedelta(seconds=10),)
                if last_completed_at
                else ()
            )
            existing_tasks = db.query(Task).filter(Task.phase_id == successor["phase"].id, Task.status != "duplicated", *cycle_filter).count()
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
            won_claim = False
            if existing_tasks == 0:
                won_claim = _claim_phase_task_creation(db, successor["phase"].id)
                if not won_claim:
                    existing_tasks = 1
            if existing_tasks == 0:
                # Re-check immediately before creating, on a fresh query --
                # same TOCTOU gap _case_start_first_phase closed (see its
                # own comment): the existing_tasks read above happens
                # BEFORE the claim attempt, so a task committed by another
                # claim-protected path on a separate DB session in that
                # window would otherwise still look like zero here.
                existing_tasks = db.query(Task).filter(Task.phase_id == successor["phase"].id, Task.status != "duplicated", *cycle_filter).count()
            if existing_tasks > 0:
                if won_claim:
                    # We won the claim ourselves -- release it, since
                    # _create_phase_task's own success path (which
                    # normally does so) is never reached below.
                    _release_phase_task_creation_claim(db, successor["phase"].id)
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
        cycle_start, cycle_filter = _correct_skewed_cycle_start(db, phase, execution, cycle_start, logger)

        # Stale-task cleanup (orphaned pending, terminated-agent pending,
        # retry-cap-exceeded pending) -- the orphan/cycle-scope comments above
        # apply to this whole block; see _mark_orphaned_and_stale_pending_tasks_failed.
        _mark_orphaned_and_stale_pending_tasks_failed(db, phase, logger, cycle_filter)

        incomplete = (
            db.query(Task)
            .filter(
                Task.phase_id == phase.id,
                # "queued" included alongside "pending"/"assigned"/
                # "in_progress" -- every other query in this module that
                # asks "does this phase have real outstanding work"
                # (_create_phase_task's own existing-task check,
                # check_phase_sibling_active, the corrective-task path)
                # already does; this one didn't. A subtask an agent creates
                # via create_task can sit "queued" (QueueService's own
                # capacity-gated status, distinct from "pending") for real,
                # legitimate reasons -- a busy per-cli/model concurrency
                # slot, not an orphan. Omitting it here meant a phase whose
                # own dispatched task finished, while it still had sibling
                # subtasks sitting "queued" and never dispatched, was
                # wrongly declared complete and the pipeline advanced past
                # it -- the queued subtasks then sat orphaned forever
                # (nothing re-checks a phase already advanced past), and a
                # later phase reviewed work that was never actually
                # finished. Confirmed live: task 4bf4518f (development)
                # completed having spawned 5 subtasks (C1 through C10) for
                # the remainder of its own assigned work; all 5 sat
                # "queued" while adversarial_review ran and completed
                # against the incomplete implementation.
                Task.status.in_(["pending", "assigned", "in_progress", "queued", "blocked"]),
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
            # Excludes "duplicated" for the same reason Case 0b's own
            # count does (see _case_in_progress_no_tasks) -- a leftover
            # duplicated task from a ticket-blocked routing to development
            # would otherwise still read as "has a task" and this branch
            # would never fire.
            total_cycle_tasks = db.query(Task).filter(Task.phase_id == phase.id, Task.status != "duplicated", *cycle_filter).count()
            if total_cycle_tasks == 0:
                if not _claim_phase_task_creation(db, phase.id):
                    continue
                # Re-check immediately before creating, on a fresh query --
                # same TOCTOU gap _case_start_first_phase closed (see its
                # own comment): total_cycle_tasks above was read BEFORE the
                # claim attempt, so a task committed by another
                # claim-protected path on a separate DB session in that
                # window would otherwise still look like zero here.
                if db.query(Task).filter(Task.phase_id == phase.id, Task.status != "duplicated", *cycle_filter).count() > 0:
                    # We won the claim ourselves -- release it, since
                    # _create_phase_task (whose own success path would
                    # normally do so) is never reached on this branch.
                    _release_phase_task_creation_claim(db, phase.id)
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

        # If this phase's most recent task is a DONE-but-unresolved
        # arbitration decision, resolve it directly instead of falling
        # through to the generic "phase complete, evaluate transition"
        # path below. That path (_fire_phase_transition -> the engine's
        # own retry-budget evaluation) has no idea a "done" task here is
        # itself an arbitration attempt -- it just recomputes "retry
        # budget exhausted" fresh and requests a BRAND NEW arbitration for
        # the exact question this one just answered. Mirrors the
        # equivalent guard fire_spec_gate_if_ready already has for the
        # event-driven path (this is the periodic-sweep sibling of that
        # same fix) -- see that function's docstring for the prior
        # incident (workflow ca539a75) it closed for THAT path only.
        # Observed live (workflow e9019930, phase design_review): this
        # sweep tick landed ~0.4-0.7s before fire_spec_gate_if_ready's own
        # resolution of the same task completion, winning the race twice
        # in a row and dispatching two redundant arbitration agents before
        # the retry-budget-exhausted 3-arbitration cap finally forced a
        # resolution instead of a 4th.
        latest_task = (
            db.query(Task)
            .filter(Task.phase_id == phase.id, *cycle_filter)
            .order_by(Task.created_at.desc())
            .first()
        )
        if (
            latest_task
            and latest_task.created_by_agent_id == ARBITRATION_CREATED_BY
            and latest_task.status == "done"
        ):
            _maybe_resolve_arbitration(workflow_id, logger)
            continue

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
            # Has failed tasks — try to retry them before marking complete
            if not _claim_phase_task_creation(db, phase.id):
                continue
            retried = None
            try:
                retried = _retry_failed_tasks_with_done(
                    db, phase, workflow_id, execution, logger,
                    failed_count, done_count, cycle_filter,
                )
                if retried is True:
                    return True
                continue
            finally:
                # _retry_failed_tasks_with_done's exhaustion branch
                # (retried is not True) deliberately sets
                # execution.status="failed" as a terminal decision.
                # _release_phase_task_creation_claim's reopen-eligible set
                # now includes "failed" (this session's fix for the
                # tombstone bug), so calling it unconditionally here would
                # flip that terminal decision straight back to
                # "in_progress" one line after it was made. Only
                # release-and-maybe-reopen on the genuine retry path; the
                # exhausted path just clears the claim field directly, so
                # the deliberately-"failed" status sticks.
                if retried is True:
                    _release_phase_task_creation_claim(db, phase.id)
                elif execution:
                    execution.task_creation_claimed_at = None
                    db.commit()

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
            # EXCEPT when the action was "arbitrate": _trigger_arbitration
            # (called from inside _fire_phase_transition above) deliberately
            # reuses this SAME claim to mark "arbitration in flight" for
            # _maybe_resolve_arbitration to find once the arbiter agent
            # finishes (see _trigger_arbitration's and _resolve_arbitration_
            # outcome's own docstrings). Clearing it here unconditionally
            # wiped that out within milliseconds of the arbiter being
            # dispatched -- long before it could finish, let alone before
            # CLAIM_STALE_TIMEOUT_SECONDS. Observed live (workflow
            # a7695dc5): an arbiter's "continue" decision was written to
            # arbitration_result.json, but _maybe_resolve_arbitration never
            # found a claimed phase to resolve it against -- this exact
            # clear had already beaten it to it -- so the decision was
            # silently dropped, the workflow sat with zero agent activity
            # for hours, and the abandonment sweep eventually failed it
            # with a misleading "lost mid-flight across a backend restart"
            # reason. Skip the clear whenever the phase's latest task is a
            # still-in-flight (not yet "done") arbitration task; once it
            # resolves, _resolve_arbitration_outcome's own finally clears it.
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
            if not _phase_has_arbitration_in_flight(db, phase_id):
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
        from src.autopilot.spec import get_max_task_retries

        max_retry_count = get_max_task_retries(phase.workflow_id)
        failed_tasks = (
            db.query(Task)
            .filter(Task.phase_id == phase.id, Task.status == "failed", *cycle_filter)
            .all()
        )

        # git_expert/doc_review can't fix a bug ticket themselves --
        # verify_no_open_tickets (task_completion/verification.py) rejects
        # their "done" call and leaves them failed with an "open bug
        # ticket(s)" reason. Left to the retryable/exhausted classification
        # below, this either retries the same phase forever (never
        # resolves) or wrongly pauses the whole workflow once retries are
        # exhausted. Route to development instead -- the phase equipped to
        # fix it, mirroring product_validation's own spec-gate goto. Same
        # fix as _retry_failed_tasks's identical check; this function is a
        # separate retry path (triggered when EVERY task in the phase is
        # failed, vs that one's "any individual failed task") so both need
        # it independently. Observed live: workflow ca539a75's git_expert
        # task got retried here even after already being fixed in
        # _retry_failed_tasks, because this is a genuinely different code
        # path with its own reset-to-pending loop.
        if phase.name in ("git_expert", "doc_review"):
            ticket_blocked = [
                t for t in failed_tasks
                if "open bug ticket" in (t.failure_reason or "").lower()
            ]
            if ticket_blocked:
                dev_phase = (
                    db.query(Phase)
                    .filter_by(workflow_id=phase.workflow_id, name="development")
                    .first()
                )
                if dev_phase:
                    for t in ticket_blocked:
                        logger.info(
                            f"[PHASE-ADVANCE] Task {t.id[:8]} ({phase.name}) blocked "
                            "by an open bug ticket -- routing to development instead "
                            "of retrying in place"
                        )
                        _create_phase_task(
                            phase.workflow_id, dev_phase.id, "development", "goto", logger,
                            feedback=t.failure_reason, source_phase_name=phase.name,
                        )
                        # Must leave "failed" -- otherwise this same task
                        # matches Task.status == "failed" again on the very
                        # next sweep tick and fires ANOTHER goto, forever.
                        # See the identical fix (and its own "observed
                        # live" note) in _retry_failed_tasks.
                        t.status = "duplicated"
                        t.failure_reason = (
                            "Routed to development via goto to resolve blocking "
                            "ticket(s); this task itself is not being retried"
                        )
                    # This phase's own PhaseExecution is still "in_progress"
                    # from the run that just failed -- reset it to "pending"
                    # so that when development's own goto eventually targets
                    # this phase name again, _case_completed_with_successor's
                    # target search (which only matches phases in the
                    # `pending` list) can actually find it. Left in_progress,
                    # the workflow permanently stalls once development
                    # returns: the only task this phase has is "duplicated"
                    # (not "done", not "failed", not live), a status none of
                    # _case_in_progress_no_tasks/_case_in_progress_complete/
                    # _case_completed_with_successor treat as "needs a fresh
                    # task" -- see the identical fix in _retry_failed_tasks.
                    # Observed live: workflow ca539a75's git_expert phase
                    # sat in_progress for 12+ hours after this exact branch
                    # fired, with development's later goto back to it
                    # silently never creating a new task.
                    execution = db.query(PhaseExecution).filter_by(phase_id=phase.id).first()
                    if execution:
                        reopen_phase_execution(execution, status="pending", started_at="clear")
                    db.commit()
                    failed_tasks = [t for t in failed_tasks if t not in ticket_blocked]
                    if not failed_tasks:
                        return True
                else:
                    logger.warning(
                        f"[PHASE-ADVANCE] {phase.name} has {len(ticket_blocked)} "
                        "ticket-blocked task(s), but this workflow has no development "
                        "phase to route back to -- falling through to normal retry handling"
                    )

        # Orphaned tasks (never dispatched), session/spend limit failures,
        # and stuck-task failures are not agent faults -- they should always
        # be retryable. Session limit failures will use the fallback model on retry.
        def _limit_failure(r):
            return "session limit" in (r or "").lower() or "spend limit" in (r or "").lower()
        def _stuck_failure(r):
            return "task stuck" in (r or "").lower()
        retryable_tasks = [
            t for t in failed_tasks
            if (t.retry_count or 0) < max_retry_count
            or "orphaned" in (t.failure_reason or "").lower()
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
                from src.autopilot.orchestrator.engine_client import pause_workflow

                pause_workflow(
                    phase.workflow_id,
                    reason="system",
                    status_reason=f"{phase.name}: exhausted retries -- {reason_text}",
                    session=db,
                )
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
            # "Orphaned: ..." means no agent ever actually received this
            # task -- a scheduling/claim-race artifact (see
            # _create_phase_task's own orphan-detection), not a real
            # attempt that made a mistake. Framing it as "your previous
            # attempt failed... fix it rather than repeating the same
            # mistake" is actively misleading on what is, from the next
            # agent's own point of view, genuinely its FIRST prompt for
            # this task -- there is nothing to "fix" or "stop repeating".
            # Skip the RETRY banner for this case; dispatch it as a plain
            # first attempt.
            if task.failure_reason and "orphaned" not in task.failure_reason.lower():
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
                        if cfg.agents.default_fallback_cli_tool:
                            session_limit_override_cli = cfg.agents.default_fallback_cli_tool
                            session_limit_override_model = cfg.agents.default_fallback_cli_model
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
                retry_task.started_at = utc_now()
                retry_db.commit()
        return True
    return None


def _fire_phase_transition(
    workflow_id: str,
    phase_id: str,
    phase_name: str,
    logger: "OrchestratorLogger",
    force_continue: bool = False,
    completion_summary: str = "Phase completed",
) -> bool:
    """Fire the phase transition: mark complete, evaluate, create next task/agent.

    completion_summary: text stored on the PhaseExecution row. Callers with
    an unusual reason for completing (e.g. _cap_out_review_phase's "ran out
    the review-run cap instead of a real pass") should say so here --
    otherwise the row reads identically to a normal completion and a goto
    that got capped out looks, to anyone reading the phase history, exactly
    like one that was genuinely honored.

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
        pm = PhaseManager(get_default_db_manager(), workflow_id=workflow_id)

        # security_review's own gate (score_security_review) only scores
        # unresolved_count -- critical/high left unfixed -- by design;
        # medium/low findings it deliberately tickets instead of fixing
        # are NOT gate input, so a clean pass here says nothing about
        # whether an open bug ticket still exists. Without this, that
        # ticket rides through qa_validation and product_validation
        # untouched (neither phase's own "done" claim is gated on open
        # tickets either -- verify_no_open_tickets deliberately excludes
        # them, see its own docstring) and only gets caught once doc_review
        # tries to mark done, two full review passes later than the ticket
        # was already known. Checked BEFORE the normal evaluation runs,
        # forcing the exact same goto machinery (_handle_force_goto) a
        # real gate decision uses -- not a post-hoc override of mark_
        # phase_complete's return value. The normal "continue" path
        # (_handle_force_continue -> _start_next_phase) already flips the
        # NEXT phase BY ORDER (qa_validation) to "in_progress" as part of
        # its own bookkeeping before this function would ever see the
        # result to override; patching target_phase_id afterward would
        # leave qa_validation's PhaseExecution stuck in_progress with no
        # task while development also starts, exactly the concurrent-
        # phase state _start_next_phase's own in-progress guard exists to
        # prevent. Skipping the normal evaluation entirely and going
        # straight to force_action="goto" avoids that path altogether.
        forced_ticket_goto = False
        if phase_name == "security_review":
            from src.services.task_completion.verification import get_open_bug_tickets

            with get_db() as _ticket_db:
                open_tickets = get_open_bug_tickets(_ticket_db, workflow_id)
            if open_tickets:
                titles = [f"{t.id}: {t.title}" for t in open_tickets[:5]]
                # Directive, not just descriptive -- matches verify_no_open_
                # tickets's own phrasing for the identical requirement, so
                # the agent is told to fix it right in this task's initial
                # dispatch instead of only discovering that requirement
                # after a first "done" attempt gets rejected.
                reason = (
                    f"Fix the underlying issue for each of these {len(open_tickets)} "
                    "open bug ticket(s) left by security review, then call "
                    "update_ticket_status(new_status='shipped') for each before "
                    "marking this task done. If a ticket genuinely has no available "
                    "fix right now (e.g. no upstream patch exists, or the fix needs a "
                    "separate human-supervised pass), don't leave it open indefinitely "
                    "-- call update_ticket_status(new_status='wontfix', comment=<why no "
                    "fix is possible/appropriate right now>) instead: " + "; ".join(titles)
                )
                logger.warning(
                    f"[PHASE-ADVANCE] security_review passed its own gate but "
                    f"{len(open_tickets)} bug ticket(s) remain open -- routing to "
                    f"development instead of continuing ({'; '.join(titles)})"
                )
                result = pm.mark_phase_complete(
                    phase_id, completion_summary,
                    force_action="goto", force_target_phase="development",
                    force_reason=reason,
                )
                forced_ticket_goto = True

        if not forced_ticket_goto:
            # Build phase output for gated phases -- skipped entirely for a
            # forced continue, which doesn't read it (_handle_force_continue
            # takes no phase_output) and shouldn't pay for computing it.
            phase_output = {}
            if not force_continue and phase_name in get_gated_phases():
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
                            workflow_id=workflow_id,
                        )

            # Mark phase complete and get engine decision
            result = (
                pm.mark_phase_complete(phase_id, completion_summary, force_action="continue")
                if force_continue
                else pm.mark_phase_complete(
                    phase_id,
                    completion_summary,
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


def _stage_forensics_inputs(worktree: Path, workflow, health: dict, logger: "OrchestratorLogger") -> None:
    """Write the two inputs forensics_analysis.yaml reads but nothing wrote.

    forensics_analysis is a post-hoc process-improvement phase: it compares
    what each agent was TOLD to do (the phase YAMLs) against what actually
    happened (the artifacts and tmux logs) and proposes prompt rewrites. It
    can only do that if it can read the prompts -- and its prompt pointed at
    a `phase_prompts/` directory and a `run_health.json` that no code path
    in this repo has ever created. Both go into the worktree's .hephaestus/,
    the same "Artifacts Path" every other phase reads and writes, and get
    swept into the feature record afterwards like any other artifact.

    Best-effort by design: forensics is an optional phase (workflow.yaml's
    optional_phases) whose own prompt already handles a missing
    run_health.json by defaulting to FULL MODE. Failing to stage its inputs
    must not take down task creation for it, so every failure here is logged
    and swallowed rather than raised.
    """
    import json as _json
    import shutil as _shutil

    artifacts = worktree / CONTEXT_DIR_NAME
    try:
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "run_health.json").write_text(_json.dumps(health, indent=2, default=str))
    except Exception as e:
        logger.warning(f"[PHASE-TASK] Could not write run_health.json for forensics: {e}")

    definition_id = getattr(workflow, "definition_id", None)
    if not definition_id:
        logger.warning("[PHASE-TASK] Workflow has no definition_id — skipping phase_prompts/ staging for forensics")
        return
    try:
        from src.workflow_registry import _WORKFLOWS_DIR

        source = _WORKFLOWS_DIR / definition_id
        if not source.is_dir():
            logger.warning(f"[PHASE-TASK] No workflow config dir at {source} — skipping phase_prompts/ staging")
            return
        dest = artifacts / "phase_prompts"
        dest.mkdir(parents=True, exist_ok=True)
        # workflow.yaml is the orchestration config (evaluation points,
        # thresholds), not a prompt given to any agent -- copied too, since
        # "why did this phase loop four times" is exactly the kind of
        # question forensics is asked to answer and the answer lives in its
        # evaluation_points.
        copied = 0
        for phase_file in sorted(source.glob("*.yaml")):
            _shutil.copy2(str(phase_file), str(dest / phase_file.name))
            copied += 1
        logger.info(f"[PHASE-TASK] Staged {copied} phase prompt(s) + run_health.json for forensics_analysis")
    except Exception as e:
        logger.warning(f"[PHASE-TASK] Could not stage phase_prompts/ for forensics: {e}")


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

    A phase with no GATE_RESULT_ARTIFACTS entry (today: doc_review --
    opted into max_review_runs in workflow.yaml but not scored via a gate
    artifact the way architectural_review/adversarial_review/
    security_review/qa_validation/product_validation are) has nothing for a
    scorer to re-read, so there's no synthetic result file to write -- but
    the cap must still apply. _fire_phase_transition doesn't require one
    either: it only calls build_phase_output (which reads
    GATE_RESULT_ARTIFACTS) for phases in GATED_PHASES, and _create_phase_
    task already relies on this exact same path with zero synthetic
    artifacts for forensics_analysis's clean-run shortcut. Previously this
    branch returned None here ("isn't a known gated phase"), which meant
    the cap silently never engaged for security_review/doc_review -- a live
    run hit 25 re-entries of security_review with max_review_runs: 4
    configured and doing nothing. (security_review has since become a
    genuinely gated phase and now takes the synthetic-artifact branch
    above; doc_review is the sole remaining user of this one.)
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
    caveats = "\n".join(f"- Run {h['run_number']}: {h['blocker_count']} unresolved finding(s) -- {h['summary'][:200]}" for h in history) or "(no findings history recorded)"
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
    return _fire_phase_transition(
        workflow_id, phase.id, phase.name, logger, force_continue=True,
        completion_summary=f"Capped after {run_count}/{max_runs} runs (max_review_runs) -- not re-reviewed",
    )


def _get_phase_max_retries(workflow_id: str, phase_name: str) -> Optional[int]:
    """Look up eval_point.max_retries for a phase, the same config-driven
    value WorkflowOrchestrator.evaluate() checks phase_retry_counts
    against.

    _create_phase_task's own retry/goto bound (below) used to be a
    hardcoded constant, completely disconnected from this value -- two
    independent counting mechanisms (this one via a DB Task-row count,
    evaluate()'s via an in-memory per-call counter) for what's supposed to
    be the same per-phase retry budget, able to disagree whenever
    workflow.yaml configured a max_retries different from the hardcoded
    number. Returns None if the workflow has no orchestrator config or no
    evaluation point for this phase (e.g. sequential mode) -- the caller
    falls back to a sane default in that case, same role the hardcoded
    constant used to play.
    """

    pm = PhaseManager(get_default_db_manager())
    with get_db() as db:
        try:
            orchestrator = pm._get_orchestrator(db, workflow_id)
        except Exception as e:
            # _get_orchestrator raises rather than returning None on failure,
            # so the gate-evaluation path can escalate instead of silently
            # going sequential. Here the documented contract is different --
            # "falls back to 5 when no orchestrator config exists" -- and
            # defaulting a retry budget is safe in a way that skipping every
            # gate is not, so this caller keeps its fallback deliberately.
            logger.error(f"[PHASE-TASK] max_retries lookup failed for {phase_name}, using default: {e}")
            return None
        if not orchestrator:
            return None
        eval_point = orchestrator._find_evaluation_point(phase_name)
        return eval_point.max_retries if eval_point else None


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
                was_stale = _clear_stale_task_creation_claim(_claim_db, phase_id, repair_status=True)
                if not was_stale or not _claim_phase_task_creation(_claim_db, phase_id):
                    logger.info(f"[PHASE-TASK] {phase_name} task creation already claimed by another caller -- skipping")
                    return False
        own_claim = True
    try:

        with get_db() as db:
            # Note: security_review's mandatory ash scan used to run here,
            # unconditionally before this function ever created a Task row
            # -- moved to launch_pipeline.py's create_agent_for_task (see
            # PRE_DISPATCH_BLOCKING_STEPS in worktree_integration.py) so the
            # Task/Agent/tmux session exist and show real activity for the
            # phase during the scan, instead of nothing at all.

            # forensics_analysis reviews every artifact + full tmux transcript
            # of a completed feature run to propose prompt/methodology fixes —
            # expensive (whole-pipeline review) and only actionable when
            # something actually went wrong. Skip spawning that agent on a
            # clean run (no tmux error patterns) and advance straight to the
            # next phase instead, using the same completion path a real agent
            # would trigger via update_task_status.
            if phase_name == "forensics_analysis":
                wf = db.query(Workflow).filter_by(id=workflow_id).first()
                # Worktree may already be gone by now (working_directory
                # cleared by _cleanup_worktree, or the directory itself
                # removed) -- _assess_run_health's own DB-based checks
                # don't need it, so call it regardless instead of only
                # when the worktree happens to still exist. Previously
                # gating the whole call on that meant this ran unassessed
                # in the vast majority of cases (confirmed live: 64 of 65
                # forensics_analysis tasks ever created had no working
                # worktree at this point) and forensics spawned every time.
                worktree_path = (
                    Path(wf.working_directory)
                    if wf and wf.working_directory and Path(wf.working_directory).exists()
                    else None
                )
                health = _assess_run_health(worktree_path, workflow_id, None, logger)
                if health["clean"]:
                    logger.info("[PHASE-TASK] forensics_analysis skipped — run was clean (no task failures/retries/arbitration/tmux errors detected)")
                    # _fire_phase_transition marks this phase complete via
                    # PhaseManager itself and advances to the next phase —
                    # the same completion path a real agent would trigger
                    # via update_task_status, just fired synthetically.
                    return _fire_phase_transition(workflow_id, phase_id, phase_name, logger)
                # Not clean, so the agent IS about to run. Materialise the
                # two inputs its prompt requires: run_health.json (the
                # health dict just computed above — otherwise thrown away
                # after this branch) and phase_prompts/ (the phase YAMLs
                # it compares outcomes against). Both were read by
                # forensics_analysis.yaml and written by nothing, anywhere
                # in the codebase; STEP 1 even made listing phase_prompts/
                # a "MANDATORY FIRST ACTION", so the agent's first act was
                # a guaranteed failure on a directory that never existed.
                # Only possible when the worktree is actually still there
                # to write into -- if it's gone, the agent runs without
                # these (same as it always has in that case).
                if worktree_path:
                    _stage_forensics_inputs(worktree_path, wf, health, logger)

            # deploy phase: skip entirely if DEPLOY.md doesn't exist
            if phase_name == "deploy":
                wf = db.query(Workflow).filter_by(id=workflow_id).first()
                if wf and wf.working_directory:
                    deploy_md = Path(wf.working_directory) / "DEPLOY.md"
                    if not deploy_md.exists():
                        logger.info(f"[PHASE-TASK] deploy skipped — DEPLOY.md not found in {wf.working_directory}")
                        return _fire_phase_transition(workflow_id, phase_id, phase_name, logger)

            # Diff-stable review phase (adversarial_review/architectural_review/
            # security_review) re-entered with zero commits since its last
            # clean pass -- see spec.DIFF_STABLE_REVIEW_PHASES's docstring for
            # why only these three. The verdict can't have changed, so skip
            # spending a full agent turn just to have it rediscover "no new
            # commits" on its own -- observed live: adversarial_review and
            # architectural_review each burned ~5 minutes doing exactly that
            # after a downstream goto sent the pipeline back through
            # development with no actual changes in their scope.
            if phase_name in DIFF_STABLE_REVIEW_PHASES:
                from src.autopilot.okf_markdown import write_okf
                from src.autopilot.spec import (
                    GATE_RESULT_ARTIFACTS,
                    get_review_pass_sha,
                    synthetic_clean_result,
                )

                pass_sha = get_review_pass_sha(workflow_id, phase_name)
                if pass_sha:
                    wf = db.query(Workflow).filter_by(id=workflow_id).first()
                    if wf and wf.working_directory and Path(wf.working_directory).is_dir():
                        try:
                            from git import Repo

                            current_sha = Repo(wf.working_directory).head.commit.hexsha
                        except Exception as e:
                            logger.warning(f"[PHASE-TASK] Could not read HEAD for {phase_name} skip-check: {e}")
                            current_sha = None
                        if current_sha and current_sha == pass_sha:
                            logger.info(
                                f"[PHASE-TASK] {phase_name} skipped -- no commits since "
                                f"last clean pass ({pass_sha[:8]})"
                            )
                            docs_dir = Path(wf.working_directory) / ".hephaestus" / phase_name
                            docs_dir.mkdir(parents=True, exist_ok=True)
                            artifacts = GATE_RESULT_ARTIFACTS.get(phase_name, ())
                            if artifacts:
                                write_okf(
                                    docs_dir / artifacts[0],
                                    synthetic_clean_result(phase_name, 0),
                                    f"# {phase_name} -- skipped, no commits since last "
                                    f"clean pass ({pass_sha[:8]})\n",
                                )
                            return _fire_phase_transition(
                                workflow_id, phase_id, phase_name, logger, force_continue=True,
                                completion_summary=(
                                    f"No commits since last clean pass ({pass_sha[:8]}) "
                                    "-- carried forward, not re-reviewed"
                                ),
                            )

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
                orphan_cutoff = utc_now() - timedelta(minutes=1)
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
                # "queued" staleness self-heal (ticket-25436cfd): unlike
                # "pending", a "queued" task has this same guard's own
                # dispatch check as its ONLY way out -- QueueService.
                # get_next_queued_task/process_queue eventually flips it to
                # "assigned", but if that never happens (queue processing
                # missed it, or the project genuinely stays at capacity
                # indefinitely), nothing else ever re-evaluates it, and this
                # guard blocks fresh task creation for the phase forever
                # (confirmed live: workflow b7bd02cc, engine_client.py:620's
                # own incident writeup). Safe to treat the same as an
                # orphaned "pending" task once stale: a "queued" task by
                # definition has no live agent and has done zero work yet
                # (QueueService's own docstring -- "capacity-gated, never
                # dispatched yet"), so failing it loses nothing a fresh task
                # doesn't immediately reproduce. Reuses the same grace
                # window as the stranded-"assigned" sweep
                # (features.py:_clean_stale_assigned_tasks) rather than the
                # much shorter 1-minute pending cutoff, since sitting
                # "queued" under real capacity pressure for a while is
                # normal and NOT itself a bug -- only genuinely excessive
                # staleness should trigger this.
                queued_stale_cutoff = utc_now() - timedelta(
                    seconds=get_config().monitoring.stranded_task_grace_seconds
                )
                is_stale_queued = (
                    existing.status == "queued"
                    and existing.queued_at is not None
                    and existing.queued_at < queued_stale_cutoff
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
                elif is_stale_queued:
                    logger.info(
                        f"[PHASE-TASK] {phase_name} has a queued task {existing.id[:8]} "
                        f"stale since {existing.queued_at} (never dequeued) -- "
                        "marking failed and creating a fresh one"
                    )
                    existing.status = "failed"
                    existing.failure_reason = "Orphaned: sat queued past the staleness grace window without ever being dequeued"
                    db.commit()
                else:
                    logger.info(f"[PHASE-TASK] {phase_name} already has active task {existing.id[:8]}, skipping")
                    return False

            # Check for active agent on this phase
            active_agent = db.query(Agent).filter(Agent.status.in_(["working", "idle", "starting"])).join(Task, Task.assigned_agent_id == Agent.id).filter(Task.phase_id == phase_id).first()
            if active_agent:
                logger.info(f"[PHASE-TASK] {phase_name} has active agent {active_agent.id[:8]}, skipping")
                return False

            # Check retry/goto bounds. Reads the same config-driven budget
            # WorkflowOrchestrator.evaluate() enforces (eval_point.max_retries)
            # instead of an independent hardcoded number, so this DB-row-count
            # safety net (durable across a process restart, unlike evaluate's
            # in-memory counter) agrees with the configured budget rather than
            # silently overriding it. Falls back to 5 when no orchestrator
            # config/evaluation point exists for this phase.
            max_phase_attempts = _get_phase_max_retries(workflow_id, phase_name) or 5
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

            capped, prior_findings_block = _review_run_cap_and_findings(
                db, workflow_id, phase, phase_id, logger
            )
            if capped is not None:
                return capped


            # Create task
            task = _build_phase_task(
                db, workflow_id, phase, phase_id, action,
                source_phase_name, feedback, prior_findings_block,
            )
            task_id = task.id


            # Update phase execution to in_progress. "skipped" is included
            # alongside "pending"/"completed" -- a phase the pipeline
            # originally skipped (e.g. adversarial_review under some
            # workflow.yaml configs) can still be the target of a real task
            # later (a goto/redo cycle sending work back through it). Left
            # at "skipped", derive_workflow_status's phase-completeness
            # check (which treats "skipped" as terminal, same as
            # "completed") sees nothing incomplete and marks the whole
            # workflow "completed" while this task is still actively
            # running -- which then gets the task itself killed as a false
            # "Orphaned: workflow already completed", and lets the design
            # queue advance to the next feature before this redo cycle (and
            # any pending human review of it) ever finished. Confirmed
            # live: task 860508ac (adversarial_review, workflow ca539a75).
            execution = db.query(PhaseExecution).filter_by(phase_id=phase_id).first()
            if execution:
                # "failed" included alongside pending/completed/skipped: a
                # goto/retry dispatches a real task onto this phase
                # regardless of the execution's own status, but leaving a
                # "failed" execution unreopened means it never becomes
                # "in_progress" while that task is genuinely running --
                # invisible to every _advance_phases case that checks the
                # workflow-wide in_progress list (e.g.
                # _case_completed_with_successor's `not in_progress` guard,
                # meant to block dispatching a LATER phase while an earlier
                # one is still active). Observed live: workflow 72ed4df8's
                # development phase (order 5) sat "failed" while its
                # goto-retried task ran for real, and _advance_phases
                # dispatched product_validation (order 10) as if nothing
                # were in progress -- twice, minutes apart -- burning two
                # full redundant agent runs.
                if execution.status in ("pending", "completed", "skipped", "failed"):
                    reopen_phase_execution(execution, status="in_progress", started_at="now")
                else:
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
                task.started_at = utc_now()
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
        #
        # EXCEPT when one of the bail-out branches above called
        # _fire_phase_transition (the forensics_analysis clean-skip path)
        # WITHOUT force_continue, and that evaluation came back "arbitrate"
        # for this SAME phase_id -- _trigger_arbitration (invoked from
        # inside it) then deliberately reuses this exact claim to mark
        # "arbitration in flight" for _maybe_resolve_arbitration to find
        # later. See _phase_has_arbitration_in_flight's docstring for the
        # live incident (workflow a7695dc5) this guards against.
        if own_claim:
            with get_db() as _release_db:
                if not _phase_has_arbitration_in_flight(_release_db, phase_id):
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
            was_stale = _clear_stale_task_creation_claim(_claim_db, phase_id, repair_status=True)
            if not was_stale or not _claim_phase_task_creation(_claim_db, phase_id):
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
    from src.autopilot.orchestrator.runtime_registries import _get_orchestrator_agent_id
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
            # Sync feature status -- same class of bug as the sweep gaps.
            from src.core.database import Feature
            for feat in db.query(Feature).filter_by(workflow_id=wf.id, status="paused").all():
                feat.status = "active"

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
        # See _build_phase_task's identical fix (_phase_case_steps.py) for
        # why: task.done_definition, not raw_description, is what feeds
        # /goal's persistent self-check -- without the concrete validation
        # failure folded in here, the goal condition only re-verifies the
        # generic phase checklist, never "did you fix THIS specific thing,"
        # letting an agent drift off-task for a long stretch with nothing
        # pulling it back. Points at the instructions file (same
        # ".hephaestus/tasks/{task_id}.md" launch_pipeline.py's
        # _write_task_instructions writes this task's raw_description to)
        # rather than inlining feedback, for the same reason: keeps /goal
        # short and sends the agent back to the full, authoritative detail.
        goal_def = (
            f"{done_def} AND the specific validation failure described in "
            f".hephaestus/tasks/{task_id}.md has been resolved -- read that "
            "file and fix what it identifies"
        )

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
            done_definition=goal_def,
            status="pending",
            priority="high",
            phase_id=phase_id,
            workflow_id=workflow_id,
            created_by_agent_id=_get_orchestrator_agent_id(wf.project_id),  # see _create_phase_task
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
    # The agent is already live at this point -- caught separately from the
    # agent_data check above so a failure here isn't misreported as "agent
    # creation failed". Left "pending" with assigned_agent_id still None on
    # failure, this task is invisible to every sweep -- _clean_stale_
    # assigned_tasks's terminated-agent pass requires assigned_agent_id
    # isnot(None), and _retry_failed_tasks only re-queries status="failed".
    # Revert to "failed" so the latter picks it up on its next ~20s tick.
    # "Orphaned:" prefix matches that function's own is_orphan check, so the
    # real, still-live agent_id doesn't burn this task's retry budget for a
    # DB write failure unrelated to the agent's own work.
    try:
        with get_db() as db:
            t = db.query(Task).filter_by(id=task_id).first()
            if t:
                t.assigned_agent_id = agent_id
                t.status = "in_progress"
                t.started_at = utc_now()
                db.commit()
    except Exception as e:
        logger.error(f"[CORRECTIVE-TASK] Agent {agent_id[:8]} created for task {task_id[:8]} but failed to link it to the task row: {e}")
        try:
            with get_db() as db:
                t = db.query(Task).filter_by(id=task_id).first()
                if t and t.status == "pending":
                    t.status = "failed"
                    t.failure_reason = f"Orphaned: agent {agent_id[:8]} created but failed to link to task row: {e}"
                    db.commit()
        except Exception as e2:
            logger.error(f"[CORRECTIVE-TASK] Failed to revert task {task_id[:8]} to failed after link failure: {e2}")
        return None

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
        if wf.status == "paused":
            # Reached only when paused_by is already None (the guard above
            # returned early otherwise) -- force=True is safe here and
            # keeps this call correct even if that guard is ever loosened.
            from src.autopilot.orchestrator.engine_client import resume_workflow

            resume_workflow(workflow_id, force=True, session=db)
        elif wf.status == "failed":
            wf.status = "active"
            # Sync feature status -- a failed workflow's feature may have
            # been cascaded to "paused" or left as "failed" by
            # derive_feature_status; either way, resuming the workflow
            # should also resume its feature.
            from src.core.database import Feature
            for feat in db.query(Feature).filter_by(workflow_id=wf.id).filter(Feature.status.in_(["paused", "failed"])).all():
                feat.status = "active"
            # Same gap as resume_feature's identical branch (feature_routes.py)
            # -- see reset_failed_phase_executions' own docstring for why a
            # "failed" PhaseExecution left behind permanently blocks this
            # workflow from ever deriving "completed" again, even once the
            # phase is successfully retried.
            from src.autopilot.orchestrator.engine_client import reset_failed_phase_executions
            reset_failed_phase_executions(workflow_id, session=db)

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
                elif t.created_at and (utc_now() - t.created_at) > timedelta(minutes=pending_stuck_minutes):
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
            # Separate try -- the agent is already live at this point, so a
            # failure here must not fall into the outer except below, which
            # (correctly, for create_agent_for_task_direct itself failing)
            # just logs and leaves the task "pending" for the *next* resume
            # of this same workflow to retry. Left "pending" with
            # assigned_agent_id still None, this task is invisible to every
            # OTHER sweep too -- _clean_stale_assigned_tasks's terminated-
            # agent pass requires assigned_agent_id isnot(None), and
            # _retry_failed_tasks only re-queries status="failed". Revert to
            # "failed" so that ~20s sweep picks it up without waiting on a
            # human to explicitly resume this workflow again. "Orphaned:"
            # prefix matches _retry_failed_tasks's own is_orphan check, so
            # the real, still-live agent_id doesn't burn this task's retry
            # budget for a DB write failure unrelated to the agent's work.
            try:
                with get_db() as db:
                    task = db.query(Task).filter_by(id=task_id).first()
                    if task:
                        task.assigned_agent_id = agent_id
                        task.status = "in_progress"
                        task.started_at = utc_now()
                        db.commit()
            except Exception as e:
                logger.error(f"[RESUME] Agent {agent_id[:8]} created for task {task_id[:8]} but failed to link it to the task row: {e}")
                try:
                    with get_db() as db:
                        task = db.query(Task).filter_by(id=task_id).first()
                        if task and task.status == "pending":
                            task.status = "failed"
                            task.failure_reason = f"Orphaned: agent {agent_id[:8]} created but failed to link to task row: {e}"
                            db.commit()
                except Exception as e2:
                    logger.error(f"[RESUME] Failed to revert task {task_id[:8]} to failed after link failure: {e2}")
                continue
            logger.info(f"[RESUME] Restarted task {task_id[:8]} with agent {agent_id[:8]}")
            restarted += 1
        except Exception as e:
            logger.warning(f"[RESUME] Failed to restart task {task_id[:8]}: {e}")
    return restarted


async def fire_spec_gate_if_ready(session, task) -> None:
    """When a gated phase's last task completes, fire the phase-completion
    gate immediately instead of waiting for the monitor's next poll.

    The orchestrator's _advance_phases only fires when the next phase is
    still pending — if it's already in_progress, the gate would be
    missed without this.

    build_phase_output may run pytest (Enhancement 1: independent test
    verification), which can block for up to several minutes. This method
    is async so it can offload that work to a thread pool executor rather
    than blocking the event loop.

    Migrated from src.services.task_completion_service.TaskCompletionService
    per Phase 1b decomposition plan (design_docs/phase_1b_decomposition.md
    section 4.4).
    """
    from src.core.log_context import set_log_context

    phase = session.query(Phase).filter_by(id=task.phase_id).first()
    if not phase:
        return
    # An ungated phase gets the same completion-driven advancement, just
    # without a gate artifact to score: build_phase_output is skipped below
    # and mark_phase_complete evaluates on the workflow's own evaluation
    # points instead.
    #
    # It used to return here, which left an ungated phase with NO
    # synchronous advancement at all -- its only path forward was the
    # background sweep, and that sweep filters to workflows whose project is
    # is_active. Observed live (workflow 72ed4df8): development completed at
    # 00:25 with its agent's work committed, ParentChat was not one of the
    # two active projects, and the pipeline sat there for 8h52m. It advanced
    # 8 seconds after the project was activated, on the next sweep tick. A
    # phase's own completion should not depend on which projects happen to
    # hold an activation slot.
    gated = phase.name in get_gated_phases()

    # An arbitration task completing is NOT a normal phase-completion
    # event. The generic gate below evaluates the phase's own artifacts
    # (review.md/challenge.md -- which consume_gate_artifacts deletes after
    # every goto, so they're stale or missing here by construction) and
    # re-runs the same orchestrator evaluation that already exhausted the
    # retry budget and requested arbitration in the first place: it can
    # only ever return "arbitrate" again, and _trigger_arbitration then
    # spawns yet another arbitration agent to re-answer the question the
    # completing one JUST answered in arbitration_result.json. Observed
    # live (workflow ca539a75): design_review hit its retry cap, and each
    # arbitration task's completion re-fired this gate (score 0.4,
    # "no challenge.md found"), dispatching a fresh arbiter every cycle --
    # 3 consecutive arbitrations all independently concluding "continue"
    # against the same already-fixed architecture.md. Resolve the decision
    # the arbiter just wrote instead of re-evaluating the phase:
    # _maybe_resolve_arbitration acts on it (continue/goto/fail), consumes
    # the result file, and releases the phase's arbitration claim. The
    # periodic sweep calls the same function, so this is just closing the
    # gap between task completion and the next sweep tick.
    if task.created_by_agent_id == ARBITRATION_CREATED_BY:
        logger.info(
            f"[SPEC-GATE] {phase.name}: arbitration task completed -- resolving its decision instead of re-running the phase gate"
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, _maybe_resolve_arbitration, task.workflow_id, logger
        )
        return

    incomplete = session.query(Task).filter_by(phase_id=phase.id).filter(Task.status.in_(["pending", "queued", "blocked", "assigned", "in_progress", "failed"])).count()
    if incomplete != 0:
        return

    set_log_context(task=task.id, phase=phase.name, workflow=task.workflow_id or "")

    # Phase complete — fire the gate now
    wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
    if not (wf and wf.working_directory):
        return

    # Claim this phase's transition before doing the slow work below
    # (build_phase_output can run pytest for minutes, and
    # mark_phase_complete's own evaluate() call can be an LLM call).
    # _case_in_progress_complete's periodic-sweep path (orchestrator.py)
    # already takes this same claim before calling mark_phase_complete
    # -- but this synchronous path (fired straight from
    # update_task_status) never did, so the sweep's claim check never
    # actually excluded it: while this path was mid-evaluation, a
    # concurrent sweep tick saw no claim held, took it itself, and
    # re-evaluated the same phase completion a second time.
    # mark_phase_complete's execution.status == "completed" idempotency
    # guard doesn't catch this either -- _handle_evaluation_goto resets
    # that SAME status back to "pending" as part of its own
    # stale-execution reset (the phase being closed is always included,
    # by design, since it needs a fresh cycle for its next run) before
    # a second caller's status check ever runs. Observed live:
    # architecture_design still "in_progress" while three design_review
    # tasks were created back to back off the same evaluation cycle.
    if not _claim_phase_task_creation(session, phase.id):
        logger.info(f"[SPEC-GATE] {phase.name}: transition already being evaluated by another caller — skipping")
        return

    import functools
    from pathlib import Path

    try:
        loop = asyncio.get_event_loop()
        if gated:
            # build_phase_output may run pytest (Enhancement 1: independent
            # test verification). Run it in a thread pool executor so the
            # async event loop is not blocked by a potentially multi-minute
            # subprocess call.
            phase_output = await loop.run_in_executor(
                None,
                functools.partial(
                    build_phase_output, phase.name, Path(wf.working_directory),
                    workflow_id=task.workflow_id,
                ),
            )
        else:
            # Nothing to score: build_phase_output returns {} for a phase with
            # no gate anyway (see get_gated_phases' docstring), and running it
            # would pay pytest's cost for a result that cannot change.
            phase_output = {}
        logger.info(f"[SPEC-GATE] {phase.name}: gate fired from completion path, phase_output={phase_output}")
        pm = PhaseManager(get_default_db_manager(), workflow_id=task.workflow_id)
        # mark_phase_complete can itself run an LLM evaluate() call and,
        # on completing the whole workflow, cascade into
        # _populate_feature_folder's recursive filesystem copies of the
        # worktree's .hephaestus/ directory -- offloaded, same reasoning
        # as build_phase_output above. Operates on its own fresh
        # PhaseManager/DatabaseManager, not this function's `session`, so
        # running it in a different thread doesn't share a transaction.
        result = await loop.run_in_executor(
            None,
            functools.partial(
                pm.mark_phase_complete,
                phase.id,
                "Phase completed (spec gate fired from update_task_status)",
                phase_output=phase_output,
            ),
        )
    finally:
        # Release only the claim -- not via _release_phase_task_
        # creation_claim, which also flips status to "in_progress" and
        # stamps started_at. That's correct for its own "claimed to
        # create a NEW task" use case but wrong here: mark_phase_complete
        # has already left this phase's execution in whatever state the
        # evaluation decided (e.g. "pending", reset by
        # _handle_evaluation_goto for its next cycle), and flipping it
        # back to "in_progress" would corrupt that.
        _clear_stale_task_creation_claim(session, phase.id, repair_status=False)
    # Result dispatch (already_completed / arbitrate / goto / continue) --
    # see _handle_spec_gate_result.
    await _handle_spec_gate_result(session, task, phase, loop, result)
