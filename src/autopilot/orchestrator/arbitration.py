"""Arbitration: what happens when a phase exhausts its automatic
retry/goto budget. Spawns a one-shot LLM agent to decide continue/goto/
fail from the phase's own attempt history, resolves its decision once
written, and enforces a hard per-phase cap so a persistently-confused
arbiter can't loop forever.

Extracted from phase_transitions.py (SOLID review 2.3/3.4-shaped finding:
that module had grown to 3539 lines, the largest in the repo, absorbing
this ~620-line subsystem alongside task-creation-claim primitives, the
phase-advance dispatch/self-heal sweep, and phase-task creation -- never
held to Phase 1c's own "~800 lines per module" criterion). Three
functions here call back into phase_transitions.py
(_fire_phase_transition, _claim_phase_task_creation, _create_phase_task)
via deferred imports inside their own function bodies, not at module
level, to avoid a load-time circular import (phase_transitions.py
imports this module's public names at its own top level, to re-export
them under their original names -- see that module's own comment).
"""

import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from src.autopilot.orchestrator.engine_client import create_agent_for_task_direct
from src.autopilot.spec import GATED_PHASES, build_phase_output
from src.core.constants import CONTEXT_DIR_NAME
from src.core.database import (
    Agent,
    DatabaseManager,
    Phase,
    PhaseExecution,
    Task,
    Workflow,
    get_db,
)
from src.phases import PhaseManager

if TYPE_CHECKING:
    from src.autopilot.orchestrator import OrchestratorLogger

logger = logging.getLogger(__name__)

# Hard per-phase arbitration cap (SOLID review 2.6) -- was a local variable
# reinstantiated inline where it's checked, despite already being referenced
# by name in comments/docstrings elsewhere in this module and in
# phase_manager.py as if it were a real constant.
MAX_ARBITRATIONS_PER_PHASE = 3


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
    from src.autopilot.orchestrator.phase_transitions import (
        _claim_phase_task_creation,
        _fire_phase_transition,
    )

    with get_db() as db:
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
        if prior_arbitrations >= MAX_ARBITRATIONS_PER_PHASE:
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
                        f"[ARBITRATE] {phase_name} hit the {MAX_ARBITRATIONS_PER_PHASE}-arbitration cap, "
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
                        f"[ARBITRATE] {phase_name} exhausted its {MAX_ARBITRATIONS_PER_PHASE}-arbitration "
                        f"cap with nothing pending to resolve, but its current output already passes "
                        f"({fresh_reason}) -- advancing instead of failing the workflow."
                    )
                    return _fire_phase_transition(workflow_id, phase_id, phase_name, logger, force_continue=True)
            logger.error(f"[ARBITRATE] {phase_name} has already been arbitrated {prior_arbitrations} times without converging -- failing the workflow instead of arbitrating again")
            wf = db.query(Workflow).filter_by(id=workflow_id).first()
            if wf:
                wf.status = "failed"
                wf.status_reason = f"{phase_name}: arbitrated {prior_arbitrations} times without converging (last reason: {reason})"
                # See the matching fix in phase_transitions.py's retry-cap
                # exhaustion path: a concurrent, unrelated phase's review
                # gate can leave paused_by="review" set here, which then
                # permanently blocks resume_workflow (requires
                # status=="paused") while feature_routes' approve handler
                # doesn't check its return value -- silent no-op Approve,
                # workflow stuck "failed" forever with a stale "review"
                # marker.
                wf.paused_by = None
                wf.paused_at = None
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
        # MAX_ARBITRATIONS_PER_PHASE cap above) -- unaffected by this.
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

        pm = PhaseManager(DatabaseManager(None), workflow_id=workflow_id)
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
    time it's invoked past MAX_ARBITRATIONS_PER_PHASE, with no record of
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
    from src.autopilot.orchestrator.phase_transitions import _create_phase_task

    logger.warning(f"[ARBITRATE] {phase_name}: decision={decision} -- {reason}")

    pm = PhaseManager(DatabaseManager(None), workflow_id=workflow_id)
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
                # an LLM-hallucinated or mis-cased name won't match) now
                # returns action "fail" from _escalate_unresolvable_goto,
                # which used to be a silent _advance_or_complete. Either way
                # it is action != "goto" -- check the ACTUAL returned action,
                # not the raw decision, or a failed goto gets treated as a
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
        # Mirror _fire_phase_transition's feedback-derivation: prefer
        # the gate's own specific finding over the static reason, and
        # substitute completion_notes when the gate reason is
        # "result_missing" (the file read came up empty at this
        # evaluation instant -- says nothing about whether the agent
        # actually did the work).
        metadata = result.get("metadata") or {}
        spec_gate = metadata.get("spec_gate", {})
        feedback = spec_gate.get("reason") or result.get("reason") or None
        if spec_gate.get("result_missing"):
            with get_db() as db:
                completing_task = db.query(Task).filter(
                    Task.phase_id == phase_id, Task.status == "done"
                ).order_by(Task.completed_at.desc()).first()
            if completing_task and completing_task.completion_notes:
                feedback = completing_task.completion_notes
        dispatched = _create_phase_task(
            workflow_id,
            target_phase_id,
            target_phase_name,
            action,
            logger,
            feedback=feedback,
            source_phase_name=phase_name,
        )
        if not dispatched:
            logger.error(f"[ARBITRATE] {phase_name}: resolved to {action} -> {target_phase_name}, but failed to create its task -- pipeline may be stalled")
