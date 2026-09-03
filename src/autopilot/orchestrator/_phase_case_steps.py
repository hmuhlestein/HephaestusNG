"""Named steps for phase_transitions._case_in_progress_complete -- extracted
from its 409-line body (docs/GOD_FUNCTION_DECOMPOSITION_CANDIDATES.md #3,
pass 1 of 5).

Each function below is a verbatim-logic extraction of one section of the
original body -- behavior-preserving, not a rewrite. The claim/release race
protection and the per-phase loop stay in _case_in_progress_complete itself
(see the comments there for why the claims must not outlive their guarded
sections).

Characterization + regression coverage for the extracted behavior:
tests/test_advance_phases.py, tests/test_phase_manager.py.
"""

import functools
import logging
import uuid
from datetime import timedelta

from src.autopilot.orchestrator.arbitration import _trigger_arbitration
from src.autopilot.orchestrator.engine_client import create_agent_for_task_direct
from src.autopilot.spec import get_max_task_retries
from src.core.constants import DIAGNOSTIC_TASK_PREFIX, GOTO_REASON_PREFIX
from src.core.database import Agent, Phase, Task, Workflow, utc_now

logger = logging.getLogger(__name__)

def _mark_orphaned_and_stale_pending_tasks_failed(db, phase, logger, cycle_filter) -> None:
    """Mark this cycle's orphaned/stale pending tasks failed so they stop
    blocking completion and become retry-eligible. Verbatim extraction of
    _case_in_progress_complete's stale-task cleanup: never-dispatched or
    dead-agent pending tasks (>1 min), pending tasks whose agent terminated
    (any age), and pending tasks past the retry cap. The why-comments live
    at the call site in phase_transitions.py."""
    orphan_cutoff = utc_now() - timedelta(minutes=1)
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

    _max_retry = get_max_task_retries(phase.workflow_id)
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


def _retry_failed_tasks_with_done(db, phase, workflow_id, execution, logger,
                                 failed_count: int, done_count: int, cycle_filter):
    """done+failed retry path. Verbatim extraction of
    _case_in_progress_complete's failed-retry block: retry the failed tasks
    that are still retryable (reset + re-dispatch), or mark phase+workflow
    failed when every failed task exhausted its retry cap. Returns True when
    a retry was dispatched, None when the phase was marked failed (caller
    `continue`s)."""
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
    # Filter to retryable tasks (orphaned, session/spend/usage limits, and
    # stuck tasks are always retryable)
    # Read max_task_retries from workflow config, default to 5

    max_retry_count = get_max_task_retries(phase.workflow_id)
    # "usage limit" must be checked alongside "session limit"/"spend limit"
    # -- see phase_transitions.py's identical copy of this classifier for
    # why (mechanical_recovery.py's _check_spend_or_session_limit produces
    # that third wording too, and missing it here left it wrongly subject
    # to the plain retry cap instead of always-retryable).
    def _limit_failure(r):
        r = (r or "").lower()
        return "session limit" in r or "spend limit" in r or "usage limit" in r
    def _stuck_failure(r):
        return "task stuck" in (r or "").lower()
    retryable_tasks = [
        t for t in failed_tasks
        if (t.retry_count or 0) < max_retry_count
        or "orphaned" in (t.failure_reason or "").lower()
        or _limit_failure(t.failure_reason)
        or _stuck_failure(t.failure_reason)
    ]
    if retryable_tasks:
        logger.info(f"[PHASE-ADVANCE] {phase.name} has {done_count} done but {len(retryable_tasks)} failed tasks to retry")
        for task in retryable_tasks:
            # "Orphaned: ..." means no agent ever actually received this
            # task (a scheduling/claim-race artifact, not a real attempt)
            # -- see phase_transitions.py's own _maybe_retry_failed_tasks
            # for the fuller reasoning. Skip the RETRY banner for this
            # case; from the next agent's point of view this is genuinely
            # its first prompt for the task.
            if task.failure_reason and "orphaned" not in task.failure_reason.lower():
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
                    task.started_at = utc_now()
            except Exception as e:
                logger.error(f"[PHASE-ADVANCE] Failed to dispatch retry agent for task {task.id[:8]}: {e}")
        db.commit()
        return True
    else:
        # All failed tasks past retry cap. Bug (original version of this
        # fix): this used to only set execution.status = "failed" and fall
        # straight through into the "phase complete, fire transition"
        # section below -- _fire_phase_transition calls PhaseManager.
        # mark_phase_complete, which evaluates the engine decision from the
        # failed task's own stale action/completion data (e.g. "continue",
        # written by the agent's own self-report before the output
        # validator rejected it), NOT from execution.status. Observed live:
        # architectural_review exhausted its retry cap on a real
        # frontmatter-schema defect and the pipeline advanced straight to
        # qa_validation as if the review had passed.
        #
        # That first fix then directly set wf.status="failed" -- a
        # terminal state with no path back, since nothing anywhere resets
        # retry_count. Let the arbiter decide instead (force through,
        # goto, or -- only once ITS OWN MAX_ARBITRATIONS_PER_PHASE cap is
        # also exhausted -- escalate to a human/fail), mirroring _maybe_
        # retry_failed_tasks's identical fix for its own "all tasks
        # failed" sibling path (same task.retry_count cap; this is the
        # "done + failed" variant) and _create_phase_task's original fix
        # for the orchestrator's evaluation-driven retry/goto counter.
        # _trigger_arbitration owns PhaseExecution's own transition
        # internally (keeps it "in_progress" while arbitration is
        # pending, see its own docstring), so this function must not also
        # transition it to "failed" first -- that would fight arbitration's
        # own state management.
        logger.warning(f"[PHASE-ADVANCE] {phase.name} has {failed_count} failed tasks all past retry cap — triggering arbitration")
        # already_claimed=True: this function's own caller already holds
        # phase.id's task_creation_claimed_at claim for the whole duration
        # of this call (see the claim/finally block around this function's
        # own call site in phase_transitions.py) -- without this,
        # _trigger_arbitration's own internal claim attempt on the SAME
        # phase_id silently fails and no arbitration task is ever created.
        # See _trigger_arbitration's own docstring for the live
        # reproduction that found this class of bug.
        _trigger_arbitration(
            workflow_id, phase.id, phase.name,
            f"{failed_count} task(s) exhausted the retry cap without producing a valid output",
            logger,
            already_claimed=True,
        )
    return None


def _review_run_cap_and_findings(db, workflow_id, phase, phase_id, logger):
    """Review-run cap + prior-findings injection for _create_phase_task.
    Verbatim extraction: enforces workflow.yaml's max_review_runs (cap-out
    via _cap_out_review_phase) and builds the PRIOR FINDINGS prompt block
    from earlier runs' findings history. Returns (capped, prior_findings_block)
    where capped is the _cap_out_review_phase result (caller returns it) or
    None to proceed normally."""
    # Lazy: _cap_out_review_phase lives in phase_transitions.py, which
    # imports this module -- a top-level import here would be circular.
    from src.autopilot.orchestrator.phase_transitions import _cap_out_review_phase

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
                return capped, ""
            # None: couldn't safely cap out (see its own docstring)
            # -- fall through to a normal task rather than
            # stranding the phase with no forward progress.
        if run_count > 0:
            history = get_review_findings_history(workflow_id, phase.name)
            if history:
                findings_lines = "\n".join(f"- Run {h['run_number']}: {h['blocker_count']} unresolved finding(s) -- {h['summary'][:200]}" for h in history)
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
    return None, prior_findings_block

def _build_phase_task(db, workflow_id, phase, phase_id, action, source_phase_name, feedback, prior_findings_block) -> Task:
    """Build + add this phase's Task row for _create_phase_task. Verbatim
    extraction: description assembly (goto/retry feedback, the phase's
    existing declared inputs via build_input_manifest, prior review
    findings), orchestrator-agent attribution, and db.add. The
    PhaseExecution in_progress/claim handling stays at the call site."""
    # Create task
    task_id = str(uuid.uuid4())
    base_description = f"Execute {phase.name}: {phase.description}"
    # Which of this phase's declared inputs actually exist right now.
    # Phase prompts have always named their inputs in prose, which
    # cannot distinguish "not produced this run" from "you guessed the
    # path wrong" -- see build_input_manifest.
    from src.autopilot.spec import build_input_manifest

    wf_for_inputs = db.query(Workflow).filter_by(id=workflow_id).first()
    input_manifest = (
        build_input_manifest(workflow_id, phase.name, wf_for_inputs.working_directory)
        if wf_for_inputs and wf_for_inputs.working_directory
        else ""
    )
    description = (
        f"{base_description}\n\n{GOTO_REASON_PREFIX}{feedback}\nAddress this specifically -- this is not a fresh implementation pass, it's a return from review with a concrete issue to fix."
        if feedback
        else base_description
    ) + input_manifest + prior_findings_block
    from src.autopilot.orchestrator.runtime_registries import _get_orchestrator_agent_id
    from src.core.database import get_project_info_for_workflow

    _own_project_id, _ = get_project_info_for_workflow(db, workflow_id)
    done_definition = " AND ".join(phase.done_definitions) if phase.done_definitions else "Complete phase objectives"
    if feedback:
        # The generic phase checklist alone is what /goal gets built from
        # (_send_goal_command reads task.done_definition, not
        # raw_description) -- without the concrete reason this task exists
        # folded in here too, Claude Code's own self-checked-completion
        # hook only ever re-verifies "did you do the generic phase things,"
        # never "did you fix the SPECIFIC issue you were sent back for."
        # The one-time initial prompt (which DOES have this detail, via
        # GOTO_REASON_PREFIX above) can get buried under an agent's own
        # exploration long before the turn ends -- /goal is what stays
        # persistently checked regardless. Observed live: a development
        # agent given a list of 4 specific failing test files drifted into
        # debugging an unrelated timeout in a 5th file for 10+ minutes
        # with nothing pulling it back to the actual assigned list.
        #
        # Feedback is NOT inlined here -- /goal is re-sent to the CLI on
        # every attempted stop, so a long or multi-issue feedback string
        # bloats every single re-check. Points at the instructions file
        # instead (see launch_pipeline.py's _write_task_instructions --
        # same ".hephaestus/tasks/{task_id}.md" path it writes this task's
        # full raw_description, feedback included, to) and tells the agent
        # it must fix what that file identifies.
        done_definition = (
            f"{done_definition} AND the specific issue described in "
            f".hephaestus/tasks/{task_id}.md has been resolved -- read that "
            "file and fix what it identifies"
        )
    task = Task(
        id=task_id,
        raw_description=description,
        enriched_description=description,
        done_definition=done_definition,
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
        created_by_agent_id=_get_orchestrator_agent_id(_own_project_id),
        action=action,
        action_target_phase=(source_phase_name if action in ("goto", "retry") else None),
    )
    db.add(task)
    return task



async def _handle_spec_gate_result(session, task, phase, loop, result) -> None:
    """fire_spec_gate_if_ready's result dispatch. Verbatim extraction: handles
    the four mark_phase_complete outcomes (already_completed, arbitrate,
    goto-with-feedback-and-target-task, continue-with-next-task) after the
    gate's own try/finally claim section. The why-comments (arbitration
    leak, goto skip, completion_notes preference, stale-execution reset)
    travel with the code below."""
    # Lazy: both live in phase_transitions.py, which imports this module --
    # a top-level import here would be circular.
    # Lazy: all three live in phase_transitions.py (which imports this
    # module -- a top-level import here would be circular). Imported from
    # phase_transitions's namespace, not arbitration's, so test patches of
    # `phase_transitions._trigger_arbitration` keep working exactly as they
    # did when this code was inline there.
    from src.autopilot.orchestrator.phase_transitions import (
        _create_phase_task,
        _trigger_arbitration,
        reset_stale_executions_on_goto,
    )
    if result.get("action") == "already_completed":
        logger.info(f"[SPEC-GATE] {phase.name}: already completed by another caller")
    elif result.get("action") == "arbitrate":
        # Regression: this branch didn't exist -- mark_phase_complete's
        # own evaluate() call already incremented total_gotos and
        # logged "[ARBITRATE] ... requesting LLM arbitration" as a
        # side effect of being called at all, but nothing here ever
        # invoked _trigger_arbitration (the thing that actually spawns
        # a capped arbitration agent and, past the cap, fails the
        # workflow instead of looping forever). Every other action
        # this function checks for was handled; "arbitrate" silently
        # fell through to no-op. Observed live: this path fires once
        # per task completion (unlike _fire_phase_transition's sweep,
        # which DOES handle "arbitrate" correctly), so a phase stuck
        # needing arbitration re-hit this exact leak on every
        # completion -- 1100+ times over ~30 hours on one workflow,
        # total_gotos climbing the whole time, zero arbitration tasks
        # ever actually created.
        logger.warning(f"[SPEC-GATE] {phase.name}: arbitration needed")
        reason = result.get("reason") or f"{phase.name} exhausted its retry budget"

        await loop.run_in_executor(
            None,
            functools.partial(
                _trigger_arbitration,
                task.workflow_id,
                result.get("target_phase_id"),
                phase.name,
                reason,
                logger,
            ),
        )
    elif result.get("action") == "goto" and result.get("target_phase_id"):
        logger.info(f"[SPEC-GATE] {phase.name}: GOTO {result.get('target_phase')} (score too low)")
        # task.action/action_target_phase already set by
        # mark_phase_complete's own _tag_completing_task -- only
        # has_results is this caller's own responsibility.
        task.has_results = True
        session.commit()

        # mark_phase_complete only decides the goto and closes THIS
        # phase's execution -- it deliberately doesn't create the
        # target phase's task itself (that's _fire_phase_transition's
        # job, mirrored here). Without this, the target phase's
        # PhaseExecution stays "completed" from its own earlier,
        # unrelated pass, so _advance_phases's background sweep never
        # sees it as needing new work -- it just keeps marching
        # forward by phase order from the highest completed phase and
        # picks the next PENDING phase instead, silently skipping the
        # goto entirely. Observed live: an adversarial_review gate
        # found 4 BLOCKER findings and decided "GOTO development", but
        # the pipeline proceeded straight to security_review with the
        # blockers never addressed.
        metadata = result.get("metadata") or {}
        spec_gate = metadata.get("spec_gate", {})
        feedback = spec_gate.get("reason") or result.get("reason") or None

        # Same fix as _fire_phase_transition's identical block in
        # orchestrator.py: a "result_missing" gate reason only means
        # the file read came up empty at this evaluation instant, not
        # that the agent didn't do the work -- if it left a real
        # completion_notes summary, that's a more accurate account of
        # what actually happened and the next phase's corrective task
        # should see that instead of a "missing" message that
        # contradicts the real work already done. `task` here is
        # already the completing task itself (this function runs from
        # its own update_task_status call), so no extra lookup needed.
        if spec_gate.get("result_missing") and task.completion_notes:
            feedback = task.completion_notes

        # Reset stale phase executions at/after the target phase.
        # Same fix as _handle_evaluation_goto in phase_manager.py:
        # without this, phases after the target keep their "completed"
        # status from a prior pass and get re-evaluated without running.
        target_order = session.query(Phase.order).filter_by(id=result["target_phase_id"]).scalar()
        if target_order is not None:
            reset_stale_executions_on_goto(
                session, task.workflow_id, target_order,
                exclude_phase_id=phase.id,
            )

        await loop.run_in_executor(
            None,
            functools.partial(
                _create_phase_task,
                task.workflow_id,
                result["target_phase_id"],
                result.get("target_phase"),
                "goto",
                logger,
                feedback=feedback,
                source_phase_name=phase.name,
            ),
        )
    elif result.get("action") == "continue":
        logger.info(f"[SPEC-GATE] {phase.name}: PASSED (score >= 0.7)")
        # _start_next_phase (called by mark_phase_complete's
        # _advance_or_complete_with_phase_info) flips the next phase's
        # PhaseExecution to "in_progress" but does NOT create its Task.
        # The sweep's _advance_phases would pick that up on its next
        # tick via Case 0b (in_progress with no tasks) or Case 1
        # (completed + pending successor), but if a concurrent sweep
        # tick already iterated and acted on a stale view it won't
        # re-read the snapshot. Same as the goto path above (which
        # always calls _create_phase_task directly): create the task
        # here so the spec gate doesn't depend on a lucky sweep tick.
        target_phase_id = result.get("target_phase_id")
        target_phase_name = result.get("target_phase")
        if target_phase_id:
            await loop.run_in_executor(
                None,
                functools.partial(
                    _create_phase_task,
                    task.workflow_id,
                    target_phase_id,
                    target_phase_name,
                    "continue",
                    logger,
                    source_phase_name=phase.name,
                ),
            )
