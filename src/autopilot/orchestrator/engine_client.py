"""Orchestrator <-> backend/LiteLLM I/O helpers."""

import asyncio
import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional

import requests

from src.autopilot.orchestrator.state import (
    _workflow_belongs_to_project,
)
from src.core.database import (
    Agent,
    Phase,
    Task,
    Workflow,
    get_db,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


API_BASE = os.environ.get("HEPHAESTUS_API_BASE", "http://127.0.0.1:8300")


def get_litellm_config() -> Dict[str, str]:
    """Read LiteLLM proxy config from environment variables."""
    return {
        "url": os.environ.get("LITELLM_PROXY_URL", ""),
        "api_key": os.environ.get("LITELLM_API_KEY", ""),
        "cost_api_key": os.environ.get("LITELLM_MASTER_KEY", ""),
        "cost_tracking": os.environ.get("LITELLM_COST_TRACKING", "false").lower() == "true",
    }


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def api_post(endpoint: str, data: dict = None, timeout: int = 5, headers: dict = None) -> Optional[dict]:
    """Legacy HTTP POST - prefer direct DB access functions below."""
    try:
        r = requests.post(f"{API_BASE}{endpoint}", json=data, timeout=timeout, headers=headers or {})
        if r.status_code == 200:
            return r.json()
        else:
            logger.debug(f"[api_post] {endpoint} returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.debug(f"[api_post] {endpoint} failed: {e}")
    return None


def update_task_status(task_id: str, status: str) -> bool:
    """Update task status directly in database (H-2 fix)."""
    try:
        with get_db() as session:
            task = session.query(Task).filter_by(id=task_id).first()
            if task:
                task.status = status
                return True
        return False
    except Exception as e:
        logger.debug(f"[update_task_status] Failed: {e}")
        return False


def increment_task_retry_count(task_id: str) -> int:
    """Persist +1 to a task's retry_count, returning the new value.

    attempt_recovery's "stop retrying after 2 attempts" guard reads this
    column via get_tasks() — without actually persisting the increment here,
    the column stays 0 forever and a permanently-broken task (e.g. its
    worktree deleted out from under it) retries indefinitely, every ~60s.
    """
    try:
        with get_db() as session:
            task = session.query(Task).filter_by(id=task_id).first()
            if task:
                task.retry_count = (task.retry_count or 0) + 1
                return task.retry_count
        return 0
    except Exception as e:
        logger.debug(f"[increment_task_retry_count] Failed: {e}")
        return 0


def terminate_agent(
    agent_id: str,
    *,
    reason: str = "",
    session=None,
) -> bool:
    """Terminate agent: set the three-field invariant and reset stray tasks.

    This is the single shared primitive for agent termination. Every raw
    ``agent.status = "terminated"`` write site must call this instead of
    hand-rolling the invariant — the bug class it closes has independently
    recurred eight times in this codebase's history.

    Ordering: resets stray tasks BEFORE flipping the agent row, not after.
    Two independent live incidents (91699b1, 92caa82) trace the same race:
    if the DB write commits before the task reset, a dying agent's own
    in-flight completion call can land in the gap, get rejected as coming
    from a terminated agent, and permanently lose real completed work.

    session: pass an existing SQLAlchemy session to participate in the
    caller's transaction (no auto-commit). Omit to create a standalone
    session that auto-commits.

    CAUTION when passing a session: this project's sessions are
    autoflush=False, so the stray-task query below reads the DATABASE, not
    your uncommitted in-memory changes. If you have already set a task to a
    terminal state (done/failed) without flushing, that write is invisible
    here -- the task still looks in_progress, matches, and gets reset to
    "pending", silently clobbering it. Either call session.flush() first, or
    set the task's terminal state AFTER this returns.

    Scope is the DB invariant only. Full tmux teardown (WIP commit,
    transcript capture, SIGINT/SIGKILL) belongs to Terminator.terminate_agent
    via AgentManager; call that when a session must actually be killed.
    """

    def _do_terminate(s):
        agent = s.query(Agent).filter_by(id=agent_id).first()
        if not agent:
            return False

        # 1. Reset stray tasks FIRST (before flipping agent row). Includes
        # under_review/needs_work, not just assigned/in_progress/pending --
        # a task can be under_review (kept-alive-for-validation) or
        # needs_work (validator sent feedback to the same still-running
        # agent) while this same agent_id still owns it. Without these two,
        # terminating that agent leaves the task permanently pointing at a
        # dead agent, invisible to every self-heal sweep that scopes to
        # assigned_agent_id (see mechanical_recovery.py's identical
        # STUCK_TASK_STATUSES fix for the same gap in its own detectors).
        stray_tasks = (
            s.query(Task)
            .filter_by(assigned_agent_id=agent_id)
            .filter(Task.status.in_(["assigned", "in_progress", "pending", "under_review", "needs_work"]))
            .all()
        )
        for stray in stray_tasks:
            stray.status = "pending"
            stray.assigned_agent_id = None

        # 2. Set the three-field invariant.
        agent.status = "terminated"
        agent.current_task_id = None
        agent.terminated_at = datetime.utcnow()
        return True

    # A caller-supplied session means the caller owns the transaction, so
    # errors must propagate: swallowing here would hand back False into a
    # transaction the caller goes on to commit, leaving the invariant
    # half-applied with nothing raised anywhere. Only the standalone path
    # below, which owns its own transaction and can cleanly abandon it,
    # degrades to a logged False.
    if session is not None:
        return _do_terminate(session)

    try:
        with get_db() as s:
            result = _do_terminate(s)
            s.commit()
            return result
    except Exception as e:
        logger.error(
            f"[terminate_agent] Failed for {agent_id[:8] if agent_id else '?'}: {e}",
            exc_info=True,
        )
        return False


# Backward-compatible alias for existing callers.
terminate_agent_direct = terminate_agent


# The complete set of values that may be stored in Workflow.paused_by.
# "system-exhausted" is the terminal give-up state written by
# _retry_exhausted_paused_workflows; it is a valid stored value even though
# that site does not route through this primitive.
PAUSE_REASONS = frozenset({"user", "budget", "review", "system", "system-exhausted"})


def pause_workflow(
    workflow_id: str,
    *,
    reason: str,
    cascade_to_feature: bool = True,
    status_reason: Optional[str] = None,
    session=None,
) -> bool:
    """Pause workflow: set the status/paused_by/paused_at invariant together.

    This is the single shared primitive for workflow pause writes -- the
    pause-state sibling to terminate_agent. Every raw ``wf.status =
    "paused"`` write site must call this instead of hand-rolling the
    triad -- the gap has independently recurred at multiple call sites:
    9aa2a19 fixed pause_workflow_direct never setting paused_by/paused_at;
    22178b1 fixed pause_project_workflows never syncing Feature.status.
    Neither fix built a shared primitive, so the same two gaps kept
    reappearing at other call sites never touched by those commits (found
    at this handoff: stop_workflow, pause_feature, _pause_feature_for_review
    and _pause_phase0_for_review all omitted paused_at; queue_routes'
    requeue/rerun pause omitted paused_by entirely, which silently defeats
    _try_auto_resume_paused_workflow's paused_by-based guard since it
    treats "no paused_by" as eligible for auto-resume the same as a
    "system" pause).

    reason: 'user', 'budget', 'review', or 'system' -- stored verbatim as
    paused_by, and validated against that set. Every consumer compares
    paused_by against exact string literals (resume_workflow's
    force-narrowing, _wait_for_phase0_review_clearance's "review" poll,
    the budget sweep's "budget" filter), so an unrecognised value does not
    fail loudly -- it silently makes all of those guards miss, leaving a
    workflow paused with nothing able to resume it.

    cascade_to_feature: when True (default), also sets status="paused" on
    every Feature linked to this workflow -- otherwise derive_feature_status
    has no branch mapping "workflow paused" to "feature paused" (its only
    PAUSED-preserving check is feature.status already being PAUSED), so
    the feature keeps showing "Active" in the UI even though nothing is
    working on it. Pass False when the caller already owns a specific
    feature's status write (e.g. pause_feature only touches the one
    feature it was called for, not every feature sharing this workflow_id).

    session: pass an existing SQLAlchemy session to participate in the
    caller's transaction (no auto-commit). Omit to create a standalone
    session that auto-commits.
    """

    if reason not in PAUSE_REASONS:
        raise ValueError(
            f"pause_workflow: unknown reason {reason!r}; "
            f"expected one of {sorted(PAUSE_REASONS)}"
        )

    def _do_pause(s):
        wf = s.query(Workflow).filter_by(id=workflow_id).first()
        if not wf:
            return False
        wf.status = "paused"
        wf.paused_by = reason
        wf.paused_at = datetime.utcnow()
        if status_reason is not None:
            wf.status_reason = status_reason
        if cascade_to_feature:
            from src.core.database import Feature, FeatureStatus

            # Only pause work that is actually still in flight. A feature
            # in a terminal state has nothing left to pause, and pausing
            # it is not recoverable: derive_feature_status returns early
            # on PAUSED (it is the one status it never re-derives), so
            # nothing ever repairs the row, and resume_workflow's mirror
            # cascade sends every paused feature to "active" -- turning a
            # completed feature into live-looking work. This is ce0c4a7's
            # bug class ("re-paused an already-approved feature"), which
            # this primitive exists to make unrepresentable.
            cascadable = (FeatureStatus.PENDING, FeatureStatus.ACTIVE)
            for feature in (
                s.query(Feature)
                .filter(
                    Feature.workflow_id == workflow_id,
                    Feature.status.in_(cascadable),
                )
                .all()
            ):
                feature.status = FeatureStatus.PAUSED
        return True

    # A caller-supplied session means the caller owns the transaction, so
    # errors must propagate: swallowing here would hand back False into a
    # transaction the caller goes on to commit, leaving the invariant
    # half-applied with nothing raised anywhere. Only the standalone path
    # below, which owns its own transaction and can cleanly abandon it,
    # degrades to a logged False.
    if session is not None:
        return _do_pause(session)

    try:
        with get_db() as s:
            result = _do_pause(s)
            s.commit()
            return result
    except Exception as e:
        logger.error(
            f"[pause_workflow] Failed for {workflow_id[:8] if workflow_id else '?'}: {e}",
            exc_info=True,
        )
        return False


def resume_workflow(
    workflow_id: str,
    *,
    force: bool = False,
    cascade_to_feature: bool = True,
    session=None,
) -> bool:
    """Resume a paused workflow: clear the pause invariant together.

    Pairs with pause_workflow. Narrows on paused_by per a333616's fix:
    "system" pauses are a heuristic give-up (not operator intent) and are
    eligible to resume without force; "user", "budget", "review", and the
    permanent "system-exhausted" give-up state require force=True. Without
    force, resuming a workflow not currently paused, or paused for a
    reason this call isn't allowed to override, is a no-op returning
    False -- callers that need to unconditionally resume (an explicit
    Resume-button click) must pass force=True.

    cascade_to_feature: when True (default), also resumes any linked
    Feature currently showing status="paused" back to "active" -- the
    resume-side mirror of pause_workflow's cascade, so a workflow this
    primitive paused-and-cascaded doesn't leave its feature stuck showing
    "Paused" after the workflow itself resumes.

    Every successful resume also zeroes Workflow.paused_retry_count.
    _retry_exhausted_paused_workflows increments that counter on its own
    self-heal cycles and gives up permanently once it hits
    paused_workflow_max_retry_cycles -- a stale count left over from an
    earlier, now-resolved pause episode would let the very next "system"
    pause trip that cap immediately, with zero real retries this time
    around. This primitive is the only other writer of paused_by/
    paused_at, so it is the one place that can reliably tell "this pause
    episode is over" and reset the counter for it.

    session: see pause_workflow.
    """

    def _do_resume(s):
        wf = s.query(Workflow).filter_by(id=workflow_id).first()
        if not wf or wf.status != "paused":
            return False
        if not force and wf.paused_by not in (None, "system"):
            return False
        wf.status = "active"
        wf.paused_by = None
        wf.paused_at = None
        wf.status_reason = None
        wf.paused_retry_count = 0
        if cascade_to_feature:
            from src.core.database import Feature, FeatureStatus

            for feature in (
                s.query(Feature)
                .filter_by(workflow_id=workflow_id, status=FeatureStatus.PAUSED)
                .all()
            ):
                feature.status = FeatureStatus.ACTIVE
        return True

    # A caller-supplied session means the caller owns the transaction, so
    # errors must propagate: swallowing here would hand back False into a
    # transaction the caller goes on to commit, leaving the invariant
    # half-applied with nothing raised anywhere. Only the standalone path
    # below, which owns its own transaction and can cleanly abandon it,
    # degrades to a logged False.
    if session is not None:
        return _do_resume(session)

    try:
        with get_db() as s:
            result = _do_resume(s)
            s.commit()
            return result
    except Exception as e:
        logger.error(
            f"[resume_workflow] Failed for {workflow_id[:8] if workflow_id else '?'}: {e}",
            exc_info=True,
        )
        return False


def pause_workflow_direct(workflow_id: str) -> bool:
    """Pause workflow directly in database (H-2 fix).

    Thin wrapper over pause_workflow for existing callers -- see that
    function for the shared invariant this closes.
    """
    return pause_workflow(workflow_id, reason="user")


def complete_workflow_direct(workflow_id: str) -> bool:
    """Complete workflow directly in database (H-2 fix)."""
    try:
        with get_db() as session:
            wf = session.query(Workflow).filter_by(id=workflow_id).first()
            if wf:
                wf.status = "completed"
                return True
        return False
    except Exception as e:
        logger.debug(f"[complete_workflow_direct] Failed: {e}")
        return False


def fail_workflow_direct(workflow_id: str) -> bool:
    """Mark workflow as failed directly in database.

    For workflows that never actually finished (e.g. still "active" with
    unfinished phases when the backend restarts) -- distinct from
    complete_workflow_direct, which asserts the pipeline genuinely
    succeeded. Mislabeling an abandoned/interrupted workflow "completed"
    corrupts downstream status derivation (feature status, design
    completeness checks) that trusts that value.
    """
    try:
        with get_db() as session:
            wf = session.query(Workflow).filter_by(id=workflow_id).first()
            if wf:
                wf.status = "failed"
                return True
        return False
    except Exception as e:
        logger.debug(f"[fail_workflow_direct] Failed: {e}")
        return False


def pause_project_workflows(db, project_id: str, paused_by: str, definition_ids: tuple = None) -> int:
    """Pause all active workflows for a project and terminate their agents.

    Resets in-progress tasks back to pending so they get re-dispatched
    on resume. Called from both the user stop-button path
    (autopilot_api.py) and the budget-enforcement path
    (cost_derivation.py).

    Args:
        db: Database session
        project_id: Project ID to pause workflows for
        paused_by: Who/what paused ('user', 'budget', 'system')
        definition_ids: Workflow definition IDs to match. Defaults to
            DESIGN_WORKFLOW_DEFINITION_IDS (autopilot + phase0 + feature_architect).

    Returns:
        Number of workflows paused.
    """
    from src.core.constants import DESIGN_WORKFLOW_DEFINITION_IDS
    from src.core.database import Agent, Task

    if definition_ids is None:
        definition_ids = DESIGN_WORKFLOW_DEFINITION_IDS

    active_workflows = (
        db.query(Workflow)
        .filter(
            Workflow.project_id == project_id,
            Workflow.definition_id.in_(definition_ids),
            Workflow.status == "active",
        )
        .all()
    )

    paused_count = 0
    workflow_ids = []
    for wf in active_workflows:
        # cascade_to_feature=True closes the same gap 22178b1 patched here
        # specifically (see pause_workflow's docstring) -- kept inline
        # rather than deferred to a loop below so each workflow's pause and
        # its feature's pause land in the same primitive call.
        pause_workflow(wf.id, reason=paused_by, session=db)
        # pause_workflow's status_reason param only sets a value, it never
        # clears one (None means "leave whatever's there alone", so other
        # callers that don't care about status_reason don't blow away a
        # legitimate existing one) -- this call site needs an explicit
        # clear on a user pause, e.g. a stale "Budget limit reached" left
        # over from an earlier budget pause that got user-overridden, so
        # it's set directly here instead.
        wf.status_reason = "Budget limit reached" if paused_by == "budget" else None
        paused_count += 1
        workflow_ids.append(wf.id)

    if paused_count > 0:
        agents_to_terminate = (
            db.query(Agent)
            .join(Task, Agent.current_task_id == Task.id)
            .filter(
                Task.workflow_id.in_(workflow_ids),
                Agent.status.in_(["working", "starting", "idle"]),
            )
            .all()
        )
        for agent in agents_to_terminate:
            terminate_agent(agent.id, session=db)
            logger.info(f"[PAUSE] Terminated agent {agent.id[:8]}")

        tasks_to_reset = (
            db.query(Task)
            .filter(
                Task.workflow_id.in_(workflow_ids),
                Task.status == "in_progress",
            )
            .all()
        )
        for task in tasks_to_reset:
            task.status = "pending"
            task.assigned_agent_id = None
            logger.info(f"[PAUSE] Reset task {task.id[:8]} to pending")

        logger.info(f"[PAUSE] Paused {paused_count} workflows for project {project_id[:8]}")
    return paused_count


def check_phase_sibling_active(
    session,
    task_id: str,
    phase_id: Optional[str],
    *,
    created_by_filter: bool = True,
    orchestrator_agent_id: Optional[str] = None,
) -> Optional["Task"]:
    """Check if another active task exists on the same phase.

    Returns the sibling task if found, None otherwise.

    created_by_filter: if True, only consider tasks created by the
    orchestrator (created_by_agent_id in (None, orchestrator)). This
    prevents blocking legitimate subtasks a phase agent creates within
    its own phase. Set to False for callers that want to block ANY
    active task on the phase (e.g. validator spawn path).
    """
    if not phase_id:
        return None

    from src.core.database import Task as _Task

    # Includes under_review/validation_in_progress/needs_work, not just
    # the plainly-active statuses -- a sibling task mid-review or mid-
    # validation still owns this phase; missing it here means a second
    # task/agent can get spawned onto the same phase concurrently.
    query = session.query(_Task).filter(
        _Task.phase_id == phase_id,
        _Task.id != task_id,
        _Task.status.in_([
            "pending", "assigned", "in_progress", "queued",
            "under_review", "validation_in_progress", "needs_work",
        ]),
    )
    if created_by_filter:
        from sqlalchemy import or_

        if orchestrator_agent_id is None:
            from src.autopilot.orchestrator import _orchestrator_agent_id
            orchestrator_agent_id = _orchestrator_agent_id
        query = query.filter(
            or_
            (
                _Task.created_by_agent_id.is_(None),
                _Task.created_by_agent_id == orchestrator_agent_id,
            ),
        )
    return query.first()


def create_agent_for_task_direct(
    task_id: str,
    workflow_id: str,
    phase_id: Optional[str] = None,
    agent_type: str = "phase",
    enriched_data_override: Optional[dict] = None,
    phase_cli_tool_override: Optional[str] = None,
    phase_cli_model_override: Optional[str] = None,
) -> Optional[dict]:
    """Create an agent for a pending task directly in-process (H-2 fix).

    Mirrors /api/create_agent_for_task (src/mcp/server.py) without a
    self-HTTP round trip. Callers here run in a background thread (not the
    asyncio event loop), so a fresh event loop is spun up to drive the
    async AgentManager.create_agent_for_task call.

    agent_type/enriched_data_override: for non-"phase" agents (e.g.
    "arbitration") dispatched from this same background-thread context --
    mirrors validator_agent.py's pattern of passing a fully-custom initial
    prompt via enriched_data["validation_prompt"], which
    AgentPromptBuilder.format_initial_message returns verbatim for these
    agent types instead of building the normal phase-task message.
    """
    from src.autopilot.orchestrator import _orchestrator_agent_id
    from src.core.app_context import get_app_state
    from src.core.database import Task

    try:
        # get_app_state() itself can raise (RuntimeError: "App state not
        # initialized") -- must be inside this try, not before it, or every
        # caller (self-heal task creation, and _create_corrective_task's
        # negotiation retries) gets an unhandled exception instead of the
        # documented "return None on failure" contract.
        server_state = get_app_state()
        session = server_state.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                logger.debug(f"[create_agent_for_task_direct] Task {task_id} not found")
                return None

            if enriched_data_override is not None:
                enriched_data = enriched_data_override
            else:
                enriched_data = {}
                if task.enriched_description:
                    enriched_data["enriched_description"] = task.enriched_description
                if getattr(task, "completion_criteria", None):
                    enriched_data["completion_criteria"] = task.completion_criteria

            # Guard: don't dispatch if the phase already has another active
            # SYSTEM-created task. _retry_failed_tasks (task-level, no
            # claim) and _advance_phases (phase-level, with claim) can both
            # decide to dispatch for the same phase in adjacent sweep ticks
            # -- the former retries the old failed task while the latter's
            # _fire_phase_transition created a fresh one via goto. This
            # check is the last line of defense before the tmux session is
            # created. Observed live: two development agents invoked
            # simultaneously for the same workflow.
            #
            # Scoped to created_by_agent_id in (None, orchestrator) --
            # i.e. tasks _fire_phase_transition/_create_phase_task create --
            # not to every task sharing this phase_id. create_task's own
            # contract has phase agents pass their OWN phase_id to spawn
            # legitimate subtasks within their phase (see mcp_client.py's
            # create_task docstring); scoping to Task.id != task_id alone
            # would block dispatch of a second such subtask just because
            # the first is still active, which isn't the race this guard
            # exists for.
            phase_sibling = check_phase_sibling_active(
                session, task_id, task.phase_id,
                created_by_filter=True,
                orchestrator_agent_id=_orchestrator_agent_id,
            )
            if phase_sibling is not None:
                logger.warning(
                    f"[create_agent_for_task_direct] Skipping dispatch for task "
                    f"{task_id[:8]}: phase {task.phase_id[:8]} already has active "
                    f"task {phase_sibling.id[:8]} ({phase_sibling.status}) -- "
                    f"avoiding duplicate agent"
                )
                return None

            # Per-cli/model concurrency gate (e.g. a local model's single
            # inference slot) -- this is the orchestrator's OWN direct
            # dispatch path for phase transitions, entirely bypassing
            # QueueService.get_next_queued_task's equivalent check, even
            # though this is the path that actually creates most phase
            # tasks (scope_review, development, etc.) in a live run.
            # resolve_cli_model_dispatch atomically reserves whichever
            # combo it picks -- the caller-supplied phase_cli_tool_override
            # (e.g. session-limit escalation) always wins and skips this
            # gate entirely, since it's a bug for this gate's own decision
            # to overwrite one the caller already made (see the regression
            # test test_caller_supplied_override_is_respected_not_discarded
            # for the incident this guards against).
            _reservation = None
            phase_glm_token_env = None
            phase_thinking_level = None
            qs = getattr(server_state, "queue_service", None)
            if qs and qs.cli_model_concurrency_limits and not phase_cli_tool_override:
                cli_override, model_override, _reservation, saturated = qs.resolve_cli_model_dispatch(
                    session, task
                )
                if saturated:
                    # No usable fallback -- dispatch on the primary anyway
                    # rather than block this phase transition entirely
                    # (this function has no "queue and retry later" path
                    # the way process_queue does).
                    logger.warning(
                        f"[create_agent_for_task_direct] Task {task_id[:8]}'s combo is at its "
                        "concurrency limit with no usable fallback -- dispatching anyway"
                    )
                elif cli_override:
                    phase_cli_tool_override = cli_override
                    phase_cli_model_override = model_override
                    # Passing phase_cli_tool/_model explicitly below
                    # short-circuits create_agent_for_task's own
                    # auto-fetch-from-Phase-row block (it only fires when
                    # all four phase_* args are None) -- fetch
                    # glm_token_env/thinking_level here too so overriding
                    # the CLI/model doesn't also silently drop this
                    # phase's other config for this one dispatch.
                    if task.phase_id:
                        _phase_row = session.query(Phase).filter_by(id=task.phase_id).first()
                        if _phase_row:
                            phase_glm_token_env = _phase_row.glm_api_token_env
                            phase_thinking_level = _phase_row.thinking_level
                    logger.info(
                        f"[create_agent_for_task_direct] Task {task_id[:8]}'s primary combo at its "
                        f"concurrency limit -- dispatching on fallback model {model_override} instead"
                    )

            # _reservation (if any) must be released once this dispatch
            # attempt finishes, success or not.
            try:
                agent = asyncio.run(
                    server_state.agent_manager.create_agent_for_task(
                        task=task,
                        enriched_data=enriched_data,
                        memories=[],
                        project_context="",
                        agent_type=agent_type,
                        use_existing_worktree=True,
                        phase_cli_tool=phase_cli_tool_override,
                        phase_cli_model=phase_cli_model_override,
                        phase_glm_token_env=phase_glm_token_env,
                        phase_thinking_level=phase_thinking_level,
                    )
                )
            finally:
                if _reservation and qs:
                    qs.release_cli_model_slot(*_reservation)
            # create_agent_for_task mutates task.assigned_agent_id/status on
            # THIS object, but commits its own separate session (which owns
            # the new Agent row) -- not this one. Without committing here
            # too, closing this session below silently discards those
            # mutations: the Agent row persists as "working" with
            # current_task_id set, while the Task row is left exactly as it
            # was (pending, no agent) forever. This was the actual root
            # cause behind tasks staying stuck at "pending" indefinitely
            # despite a real, live, working agent already assigned to them.
            session.commit()
            return {"agent_id": agent.id, "status": "created"}
        finally:
            session.close()
    except Exception as e:
        # Was logger.debug -- invisible at this app's default log level, so
        # every dispatch failure here (self-heal task creation, negotiation
        # retries, arbitration) was completely silent apart from whatever
        # generic message the caller derived from a bare None return (e.g.
        # _trigger_arbitration's "Failed to dispatch arbitration agent",
        # with no indication of why). Elevated so the actual exception is
        # visible without needing to reproduce it manually outside the
        # running process.
        logger.warning(f"[create_agent_for_task_direct] Failed: {e}")
        return None


def _update_orchestrator_status(status: str) -> None:
    """Update the orchestrator agent's status in the database.

    Args:
        status: New status ("working", "idle", or "terminated")
    """
    from src.autopilot.orchestrator import _orchestrator_agent_id
    if not _orchestrator_agent_id:
        return
    try:
        with get_db() as session:
            agent = session.query(Agent).filter_by(id=_orchestrator_agent_id).first()
            if agent:
                agent.status = status
                agent.last_activity = datetime.utcnow()
    except Exception as e:
        # Non-critical — don't break the pipeline if status update fails
        logger.debug(f"[orchestrator] Failed to update status to {status}: {e}")


def get_tasks(status: str = None, workflow_id: str = None) -> list:
    """Get tasks directly from database instead of HTTP (H-2 fix)."""
    try:
        with get_db() as session:
            query = session.query(Task)
            if status:
                query = query.filter(Task.status == status)
            if workflow_id:
                query = query.filter(Task.workflow_id == workflow_id)
            tasks = query.all()
            return [
                {
                    "id": t.id,
                    "workflow_id": t.workflow_id,
                    "phase_id": t.phase_id,
                    "status": t.status,
                    "raw_description": t.raw_description,
                    "enriched_description": t.enriched_description,
                    "assigned_agent_id": t.assigned_agent_id,
                    "created_by_agent_id": t.created_by_agent_id,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "started_at": t.started_at.isoformat() if t.started_at else None,
                    "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                    "retry_count": t.retry_count or 0,
                    "failure_reason": t.failure_reason,
                }
                for t in tasks
            ]
    except Exception as e:
        logger.debug(f"[get_tasks] Failed: {e}")
        return []


def get_agents(workflow_id: str = None) -> list:
    """Get agents directly from database instead of HTTP (H-2 fix)."""
    try:
        with get_db() as session:
            query = session.query(Agent)
            if workflow_id:
                # Filter agents by workflow through their assigned tasks
                agent_ids = session.query(Task.assigned_agent_id).filter(Task.workflow_id == workflow_id, Task.assigned_agent_id.isnot(None)).distinct().all()
                agent_ids = [a[0] for a in agent_ids]
                query = query.filter(Agent.id.in_(agent_ids))
            agents = query.all()
            return [
                {
                    "id": a.id,
                    "status": a.status,
                    "cli_type": a.cli_type,
                    "agent_type": a.agent_type if hasattr(a, "agent_type") else None,
                    "tmux_session_name": a.tmux_session_name,
                    "current_task_id": a.current_task_id,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                    "last_activity": a.last_activity.isoformat() if a.last_activity else None,
                    "health_check_failures": a.health_check_failures,
                    "restart_count": a.restart_count,
                }
                for a in agents
            ]
    except Exception as e:
        logger.debug(f"[get_agents] Failed: {e}")
        return []


def peek_agent_output(agent_id: str, lines: int = 30) -> str:
    """Peek at the last N lines of an agent's tmux output."""
    try:
        with get_db() as session:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            if not agent or not agent.tmux_session_name:
                return ""
            # Get output from tmux directly
            try:
                import libtmux

                server = libtmux.Server()
                tmux_session = server.sessions.get(agent.tmux_session_name)
                if tmux_session:
                    pane = tmux_session.attached_window.attached_pane
                    output_lines = pane.cmd("capture-pane", "-p", "-S", f"-{lines}").stdout
                    return "\n".join(output_lines)
            except Exception as e:
                logger.debug(f"[peek_agent_output] tmux error: {e}")
            return ""
    except Exception as e:
        logger.debug(f"[peek_agent_output] Failed: {e}")
        return ""


def get_task_progress(agent_id: str) -> dict:
    """Check an agent's task progress."""
    tasks = get_tasks(status="done")
    agent_done = [t for t in tasks if t.get("assigned_agent_id") == agent_id]
    tasks_in_progress = get_tasks(status="in_progress")
    agent_active = [t for t in tasks_in_progress if t.get("assigned_agent_id") == agent_id]
    return {"done": len(agent_done), "in_progress": len(agent_active)}


def get_workflow_status(workflow_id: str) -> dict:
    """Get workflow status directly from database (H-2 fix)."""
    try:
        with get_db() as session:
            wf = session.query(Workflow).filter_by(id=workflow_id).first()
            if not wf:
                return {}
            return {
                "id": wf.id,
                "status": wf.status,
                "status_reason": wf.status_reason,
                "name": wf.name if hasattr(wf, "name") else None,
                "created_at": wf.created_at.isoformat() if wf.created_at else None,
                "project_id": wf.project_id,
                "working_directory": wf.working_directory,
            }
    except Exception as e:
        logger.debug(f"[get_workflow_status] Failed: {e}")
        return {}


def get_active_workflows(project_path: Optional[str] = None, project_id: Optional[str] = None) -> list:
    """Get list of active workflows directly from database (H-2 fix).

    project_path/project_id: if given, only return workflows belonging to
    this project (see _workflow_belongs_to_project). Without this, a
    design-queue loop running against one project would see (and block
    behind, or on stop -- see run_continuous_pipeline's "Pause all active
    autopilot workflows" cleanup -- forcibly pause, or -- see
    run_single_workflow's pause_existing branch -- terminate the agents of)
    an unrelated ACTIVE workflow belonging to a completely different
    project, with no escalation/timeout on the "waiting" branch to ever
    recover from it.
    """
    try:
        with get_db() as session:
            workflows = session.query(Workflow).filter(Workflow.status == "active").all()
            if project_path:
                workflows = [wf for wf in workflows if _workflow_belongs_to_project(wf.project_id, wf.working_directory, project_id, project_path)]
            return [
                {
                    "id": wf.id,
                    "status": wf.status,
                    "name": wf.name if hasattr(wf, "name") else None,
                    "created_at": wf.created_at.isoformat() if wf.created_at else None,
                    "working_directory": wf.working_directory,
                    "project_id": wf.project_id,
                }
                for wf in workflows
            ]
    except Exception as e:
        logger.debug(f"[get_active_workflows] Failed: {e}")
        return []
