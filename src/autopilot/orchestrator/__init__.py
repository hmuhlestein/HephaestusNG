"""
Autopilot Orchestrator

A continuous multi-agent workflow engine that:
1. Watches a design queue directory for new design documents
2. Picks the next logical design to process
3. Runs the full pipeline: product → architect → developer → review → security → QA → product validation
4. Generates an HTML feature report for human review
5. Repeats until stopped or queue is empty

Designed to run for days/weeks, processing designs as they arrive.
"""
from src.autopilot.orchestrator.policy import ACTIVE_AGENT_STATUSES
import json
import shutil
import sys
from src.core.constants import AUTOPILOT_STATE_DIR
from typing import Any
from src.core.constants import CONTEXT_DIR_NAME
from src.core.constants import DESIGN_CONTEXT_SUBDIR
from src.core.database import DatabaseManager
from src.core.database import Workflow
from src.core.database import get_db
from src.core.simple_config import get_config
from src.autopilot.orchestrator.state import DesignEntry
from src.autopilot.orchestrator.state import DesignStatus
from typing import Dict
from typing import NamedTuple
from src.autopilot.orchestrator.state import FeatureReport
from src.autopilot.orchestrator.state import FeatureRunStatus
from typing import Optional
from src.core.constants import PHASE0_DEFINITION_IDS
from src.autopilot.orchestrator.phase_transitions import POLL_INTERVAL
from pathlib import Path
from src.autopilot.orchestrator.state import PersistentPipelineState
from src.autopilot.orchestrator.state import PipelineState
from src.autopilot.orchestrator.state import StopReason
from typing import Tuple
from src.autopilot.orchestrator.features import _clean_stale_assigned_tasks
from src.autopilot.orchestrator.worktree_integration import _cleanup_worktree
from src.autopilot.orchestrator.worktree_integration import _create_designs_folder
from src.autopilot.orchestrator.features import _create_feature_records
from src.autopilot.orchestrator.worktree_integration import _create_integration_worktree
from src.autopilot.orchestrator.state import _delete_project_context
from src.autopilot.orchestrator.reporting import _empty_report
from src.autopilot.orchestrator.policy import _escalate_stale_active_workflows
from src.autopilot.orchestrator.reporting import _generate_design_report_html
from src.autopilot.orchestrator.config import (
    _get_paused_workflow_max_retry_cycles as _get_paused_workflow_max_retry_cycles,
)
from src.autopilot.orchestrator.config import (
    _get_paused_workflow_retry_cooldown_seconds as _get_paused_workflow_retry_cooldown_seconds,
)
from src.autopilot.orchestrator.queue import _get_phase0_completion
from src.autopilot.orchestrator.config import _get_phase0_timeout
from src.autopilot.orchestrator.config import _get_workflow_timeout
from src.autopilot.orchestrator.queue import _has_resumable_active_design
from src.autopilot.orchestrator.runtime_registries import _interruptible_sleep
from src.autopilot.orchestrator.runtime_registries import (
    _is_workflow_monitored as _is_workflow_monitored,
)
from src.autopilot.orchestrator.phase_transitions import _negotiate_validation_fix
from src.autopilot.orchestrator.runtime_registries import _get_orchestrator_agent_id
from src.autopilot.orchestrator.runtime_registries import _orchestrator_agent_ids
from src.autopilot.orchestrator.runtime_registries import _register_monitored_workflow
from src.autopilot.orchestrator.agent_registration import _register_orchestrator_agent
from src.autopilot.orchestrator.features import _relink_features_to_workflows
from src.autopilot.orchestrator.features import _resolve_execution_order
from src.autopilot.orchestrator.phase_transitions import _resume_stuck_workflow_tasks
from src.autopilot.orchestrator.queue import _set_workflow_type
from src.autopilot.orchestrator.runtime_registries import _should_stop
from src.autopilot.orchestrator.runtime_registries import _stop_events as _stop_events
from src.autopilot.orchestrator.phase_transitions import _try_advance_phases
from src.autopilot.orchestrator.runtime_registries import _unregister_monitored_workflow
from src.autopilot.orchestrator.queue import _update_design_status
from src.autopilot.orchestrator.features import _update_feature_status
from src.autopilot.orchestrator.engine_client import _update_orchestrator_status
from src.autopilot.orchestrator.policy import _update_resumed_workflow_recovery_attempts
from src.autopilot.orchestrator.features import _validate_features_json
from src.autopilot.orchestrator.state import _workflow_belongs_to_project
import asyncio
from src.autopilot.orchestrator.policy import attempt_recovery
from src.autopilot.orchestrator.policy import check_api_credits
import copy
from datetime import datetime
from src.autopilot.orchestrator.policy import detect_hard_error
from src.autopilot.orchestrator.policy import detect_impasse
from src.autopilot.orchestrator.engine_client import get_active_workflows
from src.autopilot.orchestrator.engine_client import get_agents
from src.autopilot.orchestrator.engine_client import get_tasks
from src.autopilot.orchestrator.engine_client import get_workflow_status
from src.autopilot.orchestrator.queue import is_design_fully_complete
import logging
import os
from src.autopilot.orchestrator.engine_client import pause_workflow_direct
from src.autopilot.orchestrator.engine_client import peek_agent_output
from src.autopilot.orchestrator.queue import pick_next_design
from src.autopilot.orchestrator.human_escalation import prompt_human
from src.autopilot.orchestrator.engine_client import terminate_agent_direct
import threading
import time






# Module-level logger for persistent state operations
logger = logging.getLogger(__name__)

HEPHAESTUS_DIR = Path(__file__).parent.parent.parent.parent  # one .parent deeper: now a package __init__


STUCK_THRESHOLD = 3
DESIGN_QUEUE_SCAN_INTERVAL = 60
HEARTBEAT_INTERVAL = 300
PARENT_PEEK_INTERVAL = int(os.environ.get("HEPH_PEEK_INTERVAL", "60"))  # seconds between parent peeks

# Feature Model constants
MAX_PARALLEL_FEATURES = 4  # max concurrent feature pipelines
# How many CONSECUTIVE design-queue scans (each DESIGN_QUEUE_SCAN_INTERVAL
# apart) a workflow can show zero agent/task activity while "active" before
# the "wait for active workflow" gate gives up on it as abandoned -- see
# _escalate_stale_active_workflows. Consecutive, not elapsed-time-since-
# first-seen: a single activity blip resets the streak, so this only fires
# on genuinely sustained abandonment, matching the same
# "self-healing an infinite wait" pattern as the state.current_workflow_id
# escalation nearby (5 consecutive not-yet-complete checks).

# How long a PhaseExecution's task_creation_claimed_at can be held before
# _case_in_progress_complete treats it as abandoned rather than "still being
# created elsewhere" -- see the staleness check there. A legitimate holder
# (first-task creation, or an arbitration task's whole lifetime) finishes in
# well under this; anything still holding it this long had its releaser
# crash, get killed, or (as observed live) simply predate the claim/release
# wiring being added at all, permanently hiding the phase from completion
# detection -- no matter how many times its task actually finished.
# STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS * DESIGN_QUEUE_SCAN_INTERVAL
# (10 * 60s = 600s), or a workflow whose only problem is a stuck claim gets
# killed by that other escalation before this one ever gets a chance to
# clear it and let the workflow self-heal instead.

# Orchestrator agent IDs are now tracked per-project in runtime_registries's
# _orchestrator_agent_ids (SOLID review 2.4) -- see that module for why a
# single bare global here was a live cross-project bug.






























# ProjectContext keys for AutopilotService's "was a pipeline running, with
# what args" resume marker -- see src/autopilot/service.py's
# _persist_running_state/load_persisted_state/clear_persisted_state/
# enumerate_persisted_states. Namespaced per-project (multiple pipelines
# can be running concurrently); _RUNNING_STATE_KEY_LEGACY is the single
# pre-multi-project bare key, migrated in place by enumerate_persisted_states
# the first time the backend reads it after this change deploys.






class OrchestratorLogger:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = log_dir / "orchestrator.log"
        self.events_file = log_dir / "events.jsonl"
        self.state_file = log_dir / "state.json"
        self._lock = threading.Lock()

    def log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] [{level}] {message}"
        try:
            pass
        except OSError:
            pass  # Broken pipe when running as subprocess with DEVNULL
        with self._lock:
            with open(self.log_file, "a") as f:
                f.write(line + "\n")

    def debug(self, message: str):
        # Three call sites already used logger.debug() on this class, which
        # has never had one -- each raised AttributeError instead of logging.
        # The worst was run_single_workflow's, sitting in the handler for a
        # failed pipeline_metrics.json patch: the AttributeError escaped into
        # the enclosing `except Exception`, which reported "Failed to launch
        # workflow" and returned FAILED, turning a cosmetic metrics problem
        # into a dead workflow. Found by mypy once it was unblocked (c38f143).
        self.log(message, "DEBUG")

    def info(self, message: str):
        self.log(message, "INFO")

    def warning(self, message: str):
        self.log(message, "WARNING")

    def error(self, message: str):
        self.log(message, "ERROR")

    def event(self, event_type: str, data: dict):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": event_type,
            **data,
        }
        with self._lock:
            with open(self.events_file, "a") as f:
                f.write(json.dumps(entry) + "\n")

    def save_state(self, state: PipelineState):
        with open(self.state_file, "w") as f:
            json.dump(
                {
                    "designs_processed": state.designs_processed,
                    "designs_succeeded": state.designs_succeeded,
                    "designs_failed": state.designs_failed,
                    "total_elapsed": state.total_elapsed,
                    "current_design": state.current_design,
                    "queue_status": state.queue_status,
                },
                f,
                indent=2,
            )




























































def _resync_pipeline_registry(logger: OrchestratorLogger, loop: "asyncio.AbstractEventLoop") -> int:
    """Self-heal for a project whose persisted "was running" marker
    (AutopilotService.enumerate_persisted_states) says its pipeline should
    be running, but AutopilotServiceRegistry has no live entry for it --
    the one-shot startup resume (_resume_interrupted_workflows) either
    never ran for it or failed silently. See
    docs/SAFE_RESTART_DESIGN.md §3.5.

    Runs from the same generic, restart-safe background sweep as
    _sync_stale_feature_statuses -- catches whatever the startup resume
    missed, on an ongoing basis instead of only once at boot. Observed
    live: several backend restarts in quick succession left a project's
    pipeline dead (no crash, no error -- it just never got another turn to
    pick up new work) while its own "is this project running" status
    still read healthy, derived from an unrelated still-active workflow
    rather than the pipeline loop itself.

    AutopilotService.start() is async and spawns its own long-lived
    background task (self._task) that must stay tied to the server's
    persistent event loop, not a throwaway one -- asyncio.run(...) (this
    module's usual sync-to-async bridge, see create_agent_for_task_direct)
    would create and then immediately close a temporary loop, silently
    orphaning that task the moment start() itself returns. Scheduling onto
    the real loop via run_coroutine_threadsafe avoids that.
    """
    from src.autopilot.service import AutopilotService, get_registry

    try:
        persisted = AutopilotService.enumerate_persisted_states()
    except Exception as e:
        logger.warning(f"[PIPELINE-RESYNC] Could not enumerate persisted state: {e}")
        return 0

    registry = get_registry()
    resumed = 0
    for project_id, state in persisted:
        project_path = state.get("project_path")
        if not project_path:
            continue

        existing = registry.get(project_id)
        if existing and existing.running:
            continue  # already tracked and alive -- nothing to do

        if _should_stop(project_id):
            # A pause_for_restart() (or an explicit stop()) is already
            # in-flight for this project -- its registry entry can look
            # exactly like "should restart" here (running momentarily
            # False, persisted marker deliberately left intact) while it's
            # still mid-drain. Restarting it now would race the graceful
            # pause itself. Let the NEXT sweep tick re-check once that
            # settles, rather than force a restart mid-shutdown.
            logger.debug(
                f"[PIPELINE-RESYNC] Project {project_id[:8]}: stop already "
                "in flight, skipping this tick"
            )
            continue

        logger.warning(
            f"[PIPELINE-RESYNC] Project {project_id[:8]}: persisted state "
            "says running but no live pipeline found -- restarting"
        )
        try:
            service = registry.get_or_create(project_id)
            future = asyncio.run_coroutine_threadsafe(
                service.start(
                    project_path=project_path,
                    design_queue=state.get("design_queue", ""),
                    max_iterations=state.get("max_iterations", 10),
                ),
                loop,
            )
            future.result(timeout=30.0)
            resumed += 1
        except Exception as e:
            logger.warning(
                f"[PIPELINE-RESYNC] Failed to restart project {project_id[:8]}: {e}"
            )
    return resumed


# ── Arbitration ──────────────────────────────────────────────────────
# When a phase's retry/goto budget is exhausted -- either the cross-source
# bound in _create_phase_task, or an eval_point's own max_retries via
# PhaseManager's "arbitrate" action -- the pipeline used to just pause the
# whole workflow silently: paused_by=None, no reason recorded anywhere but
# a single WARNING line in a multi-megabyte log file, and nothing to
# un-pause it short of a human noticing and intervening. These functions
# replace that with a real decision: spawn a one-shot LLM agent with the
# phase's actual attempt history and let IT choose continue/goto/fail --
# the workflow never sits paused waiting on a human. A genuine dead end
# becomes a clearly-explained "failed" state (terminal, and the reason is
# recorded on Workflow.status_reason), not a silent pause.





































class _WorkflowActivity(NamedTuple):
    """One poll's view of a workflow's agents and tasks."""

    agents: list
    active_agents: list
    pending: list
    in_progress: list
    done: list
    failed: list
    non_terminal: list

    @property
    def has_any_work(self) -> bool:
        return bool(
            self.active_agents
            or self.pending
            or self.in_progress
            or self.non_terminal
            or self.done
        )

    @property
    def is_idle(self) -> bool:
        """Nothing running and nothing left queued -- the precondition for
        declaring the workflow either complete or broken."""
        return not (
            self.active_agents or self.pending or self.in_progress or self.non_terminal
        )


def _snapshot_workflow_activity(exec_id: str) -> _WorkflowActivity:
    """Read this workflow's current agent/task counts.

    Scoped by workflow_id throughout so a concurrently-running workflow's
    tasks are never counted here.
    """
    agents = get_agents(workflow_id=exec_id)
    non_terminal: list = []
    for status in (
        "assigned",
        "queued",
        "under_review",
        "validation_in_progress",
        "needs_work",
        "blocked",
    ):
        non_terminal += get_tasks(status=status, workflow_id=exec_id)
    return _WorkflowActivity(
        agents=agents,
        active_agents=[a for a in agents if a.get("status") in ACTIVE_AGENT_STATUSES],
        pending=get_tasks(status="pending", workflow_id=exec_id),
        in_progress=get_tasks(status="in_progress", workflow_id=exec_id),
        done=get_tasks(status="done", workflow_id=exec_id),
        failed=get_tasks(status="failed", workflow_id=exec_id),
        non_terminal=non_terminal,
    )


def _log_agent_state_changes(agents: list, previous: dict, logger: OrchestratorLogger) -> dict:
    """Log spawns/terminations since the last poll; return the new states."""
    current = {a["id"]: (a.get("status", ""), a.get("agent_type", "")) for a in agents}
    for aid, (status, atype) in current.items():
        prev_status, _ = previous.get(aid, (None, None))
        if prev_status is None and status in ACTIVE_AGENT_STATUSES:
            logger.info(f"  [AGENT SPAWN] {aid[:8]} ({atype}) status={status}")
        elif prev_status in ACTIVE_AGENT_STATUSES and status == "terminated":
            logger.info(f"  [AGENT DONE]  {aid[:8]} ({atype}) terminated")
        elif prev_status is not None and prev_status != status:
            logger.info(f"  [AGENT]       {aid[:8]} ({atype}): {prev_status} → {status}")
    return current


def _peek_active_agent_output(active_agents: list, logger: OrchestratorLogger) -> None:
    """Parent peeks at children's output periodically for observability."""
    for agent in active_agents:
        aid = agent.get("id", "")
        output = peek_agent_output(aid, lines=15)
        if not output:
            continue
        lines = [ln.strip() for ln in output.strip().split("\n") if ln.strip()][-8:]
        if lines:
            preview = " | ".join(lines[-3:])  # last 3 lines
            logger.info(f"  [{aid[:8]}] {preview}")


def _has_unfinished_phases(exec_id: str, done_count: int, logger: OrchestratorLogger) -> bool:
    """Whether any phase is still pending/in_progress.

    Guards against declaring a workflow complete while the monitor simply
    hasn't created the next phase's task yet. A failure to check is reported
    as "no unfinished phases" so the caller falls through to its own
    completion handling, matching the original inline behavior.
    """
    try:
        from src.core.database import DatabaseManager, PhaseExecution

        _session = DatabaseManager(None).get_session()
        try:
            unfinished = (
                _session.query(PhaseExecution)
                .filter(
                    PhaseExecution.workflow_execution_id == exec_id,
                    PhaseExecution.status.in_(["pending", "in_progress"]),
                )
                .count()
            )
            if unfinished > 0:
                logger.info(f"{done_count} tasks done but {unfinished} phases still pending/in_progress — waiting")
                return True
            return False
        finally:
            _session.close()
    except Exception as e:
        logger.warning(f"Could not check phase status: {e}")
        return False


def _merge_design_branch_into_main(
    design_branch: Optional[str], project_path: str, logger: OrchestratorLogger
) -> None:
    """Merge the shared design branch into main once the workflow completes.

    A merge conflict aborts and preserves the branch for a manual merge/PR
    rather than failing the (already successful) workflow.
    """
    try:
        if not design_branch:
            logger.info("No design branch tracked — skipping final merge")
            return

        import git as _git

        from src.core.database import DatabaseManager as DbManager
        from src.core.simple_config import get_config
        from src.core.worktree_manager import WorktreeManager

        cfg = get_config()
        wt_mgr = WorktreeManager(db_manager=DbManager(str(cfg.paths.database_path)))
        wt_mgr.reload(Path(project_path))

        # Ensure main is clean
        wt_mgr.main_repo.heads[wt_mgr.config.git.base_branch].checkout()
        try:
            wt_mgr.main_repo.git.merge("--abort")
        except Exception:
            pass
        wt_mgr.main_repo.git.reset("--hard", "HEAD")
        wt_mgr.main_repo.git.clean("-fd")

        try:
            wt_mgr.main_repo.git.merge(
                design_branch,
                no_ff=True,
                m=f"Merge design branch {design_branch} into main",
            )
            merge_sha = wt_mgr.main_repo.head.commit.hexsha
            logger.info(f"Final merge complete: {design_branch} -> main ({merge_sha[:8]})")
        except _git.exc.GitCommandError as e:
            if "CONFLICT" in str(e):
                logger.warning(f"Merge conflict on {design_branch} -> main, aborting")
                wt_mgr.main_repo.git.merge("--abort")
                logger.info(f"Conflict detected — branch {design_branch} preserved for manual merge/PR")
            else:
                raise

        # Worktree is intentionally kept — UI references artifacts there
    except Exception as e:
        logger.warning(f"Final merge failed: {e}")


def run_single_workflow(
    sdk,
    workflow_id: str,
    project_path: str,
    description: str,
    logger: OrchestratorLogger,
    launch_params: Dict[str, Any] = None,
    state: PipelineState = None,
    max_iterations: Optional[int] = None,
    design_id: Optional[str] = None,
    timeout_seconds: int = None,
    pause_existing: bool = True,
    existing_workflow_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> FeatureRunStatus:
    """Run a single workflow execution.

    Args:
        max_iterations: Maps to the engine's max_total_gotos.
        timeout_seconds: Hard deadline for this workflow (default: from config).
            Pass 0 or a custom value for Phase 0 runs.
        project_id: AutopilotProject.id this workflow belongs to, for
            per-project stop-signal scoping (_should_stop). NOT the same
            as project_path above, which at both call sites is actually a
            worktree path, not the project root -- project_id must be
            passed explicitly by the caller, not derived from project_path.
        pause_existing: If False, skip pausing currently-active workflows. Set to
            False when running feature pipelines in parallel so threads don't
            clobber each other's workflows.
        existing_workflow_id: Resume this already-created workflow instead of
            launching a new one via sdk.start_workflow. Set when a design's
            feature pipeline was stopped mid-flight (service stop/pause) and
            is being resumed on a later run rather than started fresh --
            skips re-launching, resets any stuck tasks, and jumps straight
            into the monitor loop below.
    """
    # FIX: Get timeout from config if not specified
    if timeout_seconds is None:
        timeout_seconds = _get_workflow_timeout()
    # max_iterations maps to the engine's max_total_gotos, but PER THIS
    # WORKFLOW INSTANCE (via launch_params, read by
    # PhaseManager._get_orchestrator) -- NOT written into the shared
    # WorkflowDefinition.orchestrator_config row every "autopilot"-type
    # workflow reads from. That used to be a single global mutation
    # (_update_orchestrator_max_gotos), which corrupted every OTHER
    # concurrently-active workflow of the same definition every time ANY
    # workflow launched with a different max_iterations -- e.g. run_phase0
    # hardcodes max_iterations=3 for every Phase 0 launch, which slammed
    # every in-flight feature pipeline's real max_total_gotos (30, from
    # workflow.yaml) down to 3 each time Phase 0 ran, regardless of which
    # feature was actually being launched. Observed live: a workflow stuck
    # re-arbitrating scope_review forever, total_gotos climbing into the
    # hundreds, because its budget kept getting reset out from under it by
    # unrelated Phase 0 runs.
    #
    # max_iterations defaults to None (no override) rather than a hardcoded
    # number, and is only written into launch_params when a caller
    # EXPLICITLY passes one -- run_phase0 is the one deliberate case,
    # hardcoding max_iterations=3 to cap how many times ITS OWN
    # decomposition may goto/retry. Every regular feature pipeline launch
    # (_run_one_feature) used to pass this same parameter through from the
    # CLI's --max-iterations flag (default 3, semantically "how many times
    # to retry a whole DESIGN" -- see MAX_DESIGN_RETRIES for that actual
    # mechanism, which is unrelated and unaffected by this value) -- so
    # instead of an unset override letting workflow.yaml's own generous
    # max_total_gotos: 30 apply, EVERY feature workflow in the system got
    # silently capped at 3 total gotos across its entire 13-phase pipeline
    # lifetime. Observed live: adversarial_review found real BLOCKERs,
    # scored correctly, but total_gotos had already reached 6 from
    # legitimate earlier review cycles -- "GOTO limit exceeded (6/3).
    # Forcing continue to prevent infinite loop" silently waved the
    # findings through to security_review instead of sending them back to
    # development.
    launch_params = dict(launch_params or {})
    if max_iterations is not None:
        launch_params["max_iterations"] = max_iterations

    # Check for existing active workflows and stop them -- but never the
    # workflow we're about to resume ourselves. Without this exclusion, an
    # existing_workflow_id resume (e.g. after a backend restart) terminates
    # its own live, working agent here before ever reaching the resume logic
    # below, discarding whatever that agent was mid-task on (observed live:
    # a just-finished agent's final report got dropped because its
    # termination raced 35s ahead of it).
    #
    # Scoped to project_path (this function's own parameter): this is the
    # most destructive of the three get_active_workflows() call sites in
    # this file -- it doesn't just block or pause, it TERMINATES AGENTS for
    # every match below. Left unscoped, a workflow launch in one project
    # would kill live, working agents in a completely different project's
    # concurrently-running pipeline, the same class of cross-project
    # collateral damage fixed at this file's other two call sites (see
    # run_continuous_pipeline's "previous workflow" check and its "pause
    # all active workflows on stop" cleanup).
    if not pause_existing:
        existing_workflows = []
    else:
        existing_workflows = [wf for wf in get_active_workflows(project_path, project_id=project_id) if wf.get("id") != existing_workflow_id]
    if existing_workflows:
        logger.info(f"Found {len(existing_workflows)} active workflow(s) - stopping them...")
        for wf in existing_workflows:
            wf_id = wf.get("id", "")
            try:
                # Terminate agents for this workflow
                agents = get_agents(workflow_id=wf_id)
                for agent in agents:
                    if agent.get("status") in ACTIVE_AGENT_STATUSES:
                        try:
                            terminate_agent_direct(agent["id"])
                            logger.info(f"  Terminated agent {agent['id'][:8]} for workflow {wf_id[:8]}")
                        except Exception:
                            pass
                # Mark workflow as paused
                pause_workflow_direct(wf_id)
                logger.info(f"  Paused workflow {wf_id[:8]}")
            except Exception as e:
                logger.warning(f"  Failed to stop workflow {wf_id[:8]}: {e}")

    logger.info(f"Launching workflow: {workflow_id} (max_iterations={max_iterations})")
    # Extract design document from launch_params for the event
    design_doc = (launch_params or {}).get("design_document", "")
    design_name = Path(design_doc).stem.replace("_", " ").replace("-", " ") if design_doc else ""
    logger.event(
        "workflow_launch",
        {
            "workflow": workflow_id,
            "path": project_path,
            "design": design_name or design_doc,
        },
    )

    # Create a shared worktree for this design (all phases commit here)
    design_worktree_path = None
    design_branch_name = None
    try:
        from src.core.database import DatabaseManager as DbManager
        from src.core.simple_config import get_config
        from src.core.worktree_manager import WorktreeManager

        cfg = get_config()
        db = DbManager(str(cfg.paths.database_path))
        wt_mgr = WorktreeManager(db_manager=db)

        # FIX: If project_path is already a worktree (contains .worktrees/),
        # use it directly as the design worktree. Don't create a nested
        # worktree inside it — that would be destroyed when the parent
        # worktree is cleaned up.
        if ".worktrees/" in str(project_path):
            design_worktree_path = str(project_path)
            logger.info(f"Using existing worktree directly: {design_worktree_path}")
        else:
            # Reload to point at the actual project repo (not config.main_repo_path)
            wt_mgr.reload(Path(project_path))

            # Create feature branch from main
            import git as _git

            # Use design_entry name if available, otherwise derive from design_doc
            _design_label = design_name.replace(" ", "-").lower() if design_name else "design"
            feature_branch = f"feature/{_design_label}"
            # Ensure branch name is unique (append short hash if needed)
            try:
                wt_mgr.main_repo.git.branch(feature_branch)
            except _git.exc.GitCommandError:
                # Branch exists — use it (idempotent)
                pass

            # Create worktree for the feature branch
            # Use flattened name for worktree path (branch name has / which creates subdirs)
            safe_branch = feature_branch.replace("/", "-")
            wt_path = wt_mgr.worktree_base / f"wt_{safe_branch}"
            if not wt_path.exists():
                wt_mgr.main_repo.git.worktree("add", str(wt_path), feature_branch)
            design_worktree_path = str(wt_path)
            design_branch_name = feature_branch
            logger.info(f"Created shared worktree: {design_worktree_path} (branch: {feature_branch})")

        # Copy design doc into worktree as .hephaestus/design.md so all phases can read it
        wt_heph = Path(design_worktree_path) / CONTEXT_DIR_NAME
        wt_heph.mkdir(parents=True, exist_ok=True)
        if "design_document" in (launch_params or {}):
            _dd = Path(launch_params["design_document"])
            if _dd.exists():
                import shutil as _shutil

                _shutil.copy2(_dd, wt_heph / "design.md")
                logger.info(f"Copied design doc to worktree: {wt_heph / 'design.md'}")
    except Exception as e:
        logger.warning(f"Failed to create shared worktree, using project path: {e}")
        design_worktree_path = project_path

    try:
        if existing_workflow_id:
            exec_id = existing_workflow_id
            logger.info(f"Resuming existing workflow: {exec_id}")
            # The worktree-path computation above may have recreated the
            # deterministic path after an earlier failed attempt cleared
            # working_directory (see _cleanup_worktree) -- restore it here.
            # verify_output_artifact only reads Workflow.working_directory
            # (never phases_folder_path), so a resumed workflow with this
            # left None has every subsequent "done" claim rejected forever.
            with get_db() as _db_resume:
                _wf_resume = _db_resume.query(Workflow).filter_by(id=exec_id).first()
                if _wf_resume and _wf_resume.working_directory != design_worktree_path:
                    logger.info(f"Restoring working_directory for {exec_id[:8]}: {_wf_resume.working_directory!r} -> {design_worktree_path}")
                    _wf_resume.working_directory = design_worktree_path
            restarted = _resume_stuck_workflow_tasks(exec_id, logger)
            logger.info(f"Resume: reset {restarted} stuck task(s) for workflow {exec_id[:8]}")
        else:
            exec_id = sdk.start_workflow(
                definition_id=workflow_id,
                description=description,
                working_directory=design_worktree_path or project_path,
                launch_params=launch_params or {},
                design_id=design_id,
            )
            logger.info(f"Workflow launched: {exec_id}")

        if workflow_id == "feature_architect" and design_id:
            # Persist immediately, not only after this function later
            # returns "completed" (see run_phase0's analogous designs_folder
            # comment) -- run_phase0's own Tier 2 recovery path
            # (_get_phase0_completion) requires design.phase0_workflow_id to
            # already point at this workflow to find anything to recover.
            # Without this, a Phase 0 run that gets interrupted (crash,
            # restart, or the agent simply outliving an orchestrator-side
            # false "interrupted" read) but whose agent finishes anyway
            # leaves nothing for that recovery path to find, permanently
            # stranding otherwise-good work behind a "failed" design that
            # never gets retried.
            #
            # Gated on this literal definition id, not just design_id being
            # set: a design's per-feature "autopilot" workflow runs also
            # pass the same design_id and must never overwrite this field.
            try:
                from src.core.database import AutopilotDesign

                with get_db() as _db_p0:
                    _db_p0.query(AutopilotDesign).filter_by(id=design_id).update(
                        {AutopilotDesign.phase0_workflow_id: exec_id}
                    )
            except Exception as e:
                logger.warning(f"Failed to persist phase0_workflow_id immediately after launch: {e}")

        if state:
            state.current_workflow_id = exec_id
            # Store branch name for final merge
            state._design_branch = design_branch_name
            state._design_worktree = design_worktree_path
            # Checkpoint now, not just after run_single_design returns --
            # see PersistentPipelineState.save_state_only's docstring. The
            # status endpoint's current_workflow_id reads only this
            # persisted state (no live fallback), so without this it stays
            # pointed at the previous, already-finished workflow for this
            # run's entire duration.
            PersistentPipelineState(project_id=project_id).save_state_only(state)

        # Patch pipeline_metrics.json with the workflow_id so the UI can link tasks to features
        if state and state.current_feature_folder:
            try:
                _pm_path = Path(state.current_feature_folder) / "docs" / "pipeline_metrics.json"
                if _pm_path.exists():
                    import json as _json

                    _pm_data = _json.loads(_pm_path.read_text())
                    _pm_data["workflow_id"] = exec_id
                    _pm_path.write_text(_json.dumps(_pm_data, indent=2, default=str))
                    logger.info(f"Patched pipeline_metrics.json with workflow_id={exec_id[:8]}")
            except Exception as _pm_err:
                logger.debug(f"Could not patch pipeline_metrics.json: {_pm_err}")
    except Exception as e:
        logger.error(f"Failed to launch workflow {workflow_id}: {e}")
        return FeatureRunStatus.FAILED

    stuck_count = 0
    credit_stuck_count = 0
    # Consecutive-poll counter guarding the "no tasks exist" HARD_ERROR
    # verdict below against a single transient get_tasks() DB failure --
    # get_tasks() swallows its own exceptions and returns [] on failure,
    # indistinguishable from "genuinely no tasks in this status" to every
    # caller. A lone bad poll (e.g. SQLite write contention, a documented
    # recurring issue elsewhere in this codebase) landing on all of
    # pending/in_progress/non_terminal/done simultaneously previously
    # killed a healthy, actively-progressing workflow outright. Requiring
    # the same verdict on a second, independent poll before acting matches
    # this file's own established pattern for exactly this class of
    # false-positive (see STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS above).
    no_tasks_streak = 0
    start_time = time.time()
    _last_phase_states: dict = {}  # phase_name -> status, for transition detection
    _last_agent_states: dict = {}  # agent_id -> (status, phase_label), for spawn/terminate detection

    def _log_phase_transitions(exec_id: str) -> None:
        """Log any phase status changes since last poll, including GOTOs."""
        nonlocal _last_phase_states
        try:
            from src.core.database import DatabaseManager as _DbM
            from src.core.database import Phase, PhaseExecution

            _db = _DbM()
            _s = _db.get_session()
            try:
                rows = (
                    _s.query(Phase.name, PhaseExecution.status)
                    .join(PhaseExecution, PhaseExecution.phase_id == Phase.id)
                    .filter(PhaseExecution.workflow_execution_id == exec_id)
                    .order_by(Phase.order)
                    .all()
                )
                current = {name: status for name, status in rows}
                for name, status in current.items():
                    prev = _last_phase_states.get(name)
                    if prev is None:
                        continue  # first observation, no transition yet
                    if status == prev:
                        continue
                    # Detect GOTO: a previously completed phase rewound to in_progress
                    if prev == "completed" and status == "in_progress":
                        logger.info(f"  [GOTO] {name}: completed → in_progress (rewound by earlier phase)")
                    else:
                        logger.info(f"  [TRANSITION] {name}: {prev} → {status}")
                _last_phase_states = current
            finally:
                _s.close()
        except Exception as _e:
            logger.warning(f"Phase transition check failed: {_e}")

    _register_monitored_workflow(exec_id)
    try:
        while True:
            time.sleep(POLL_INTERVAL)

            # Check if in-process service requested a stop
            if _should_stop(project_id):
                logger.info("Stop requested during workflow execution")
                return FeatureRunStatus.INTERRUPTED

            # Timeout check
            elapsed = int(time.time() - start_time)
            if elapsed > timeout_seconds:
                logger.error(f"Workflow timed out after {timeout_seconds}s")
                return FeatureRunStatus.TIMEOUT

            wf_status = get_workflow_status(exec_id)
            activity = _snapshot_workflow_activity(exec_id)

            _log_phase_transitions(exec_id)
            _last_agent_states = _log_agent_state_changes(activity.agents, _last_agent_states, logger)

            logger.info(f"[{workflow_id}] [{elapsed}s] Agents: {len(activity.active_agents)} active | Tasks: {len(activity.pending)} pending, {len(activity.in_progress)} active, {len(activity.done)} done, {len(activity.failed)} failed")

            # Phase progression — the single source of truth for advancing phases.
            # This replaces the monitor's phase progression logic.
            _try_advance_phases(exec_id, logger)

            # Refresh ALL counts after phase advancement. _advance_phases
            # may have created a new task + agent — the pre-advance snapshot
            # is now stale, and acting on it could trick the completion check
            # into seeing "no agents, no work" before the new task appeared.
            activity = _snapshot_workflow_activity(exec_id)
            if activity.has_any_work:
                no_tasks_streak = 0

            # Agent scheduling is handled by the server's background_queue_processor.
            # Stuck-agent detection is handled by Guardian/Conductor.
            # The orchestrator only monitors and logs.

            if elapsed > 0 and elapsed % PARENT_PEEK_INTERVAL < POLL_INTERVAL:
                _peek_active_agent_output(activity.active_agents, logger)

            wf_state = wf_status.get("status", "")
            if wf_state in ("completed", "failed", "paused"):
                logger.info(f"Workflow {wf_state}: {exec_id}")
                return FeatureRunStatus(wf_state)

            # Check if workflow should be considered complete:
            # No active agents AND no pending/in-progress/non-terminal tasks
            if activity.is_idle:
                # All agents done, no more work to do
                if activity.done:
                    # Verify all phases are completed before declaring workflow
                    # done. This prevents premature completion when the monitor
                    # hasn't yet created the next phase's task.
                    if _has_unfinished_phases(exec_id, len(activity.done), logger):
                        time.sleep(POLL_INTERVAL)
                        continue

                    logger.info(f"Workflow complete: {len(activity.done)} tasks done, no agents active, all phases done")

                    _merge_design_branch_into_main(
                        getattr(state, "_design_branch", None), project_path, logger
                    )

                    if state:
                        state.current_workflow_id = None
                    return FeatureRunStatus.COMPLETED
                elif elapsed > 300 and not activity.done:
                    # No tasks AND no done tasks after 5 minutes — something
                    # is wrong. Confirmed on a second consecutive poll before
                    # acting -- see no_tasks_streak's declaration above for
                    # why a single poll isn't trusted alone.
                    no_tasks_streak += 1
                    if no_tasks_streak >= 2:
                        logger.error(f"No tasks exist after {elapsed}s (confirmed on {no_tasks_streak} consecutive polls) — workflow appears broken")
                        return FeatureRunStatus.HARD_ERROR
                    logger.warning(f"No tasks exist after {elapsed}s — reconfirming next poll before declaring the workflow broken")

            out_of_credits, credit_reason = check_api_credits()
            if out_of_credits:
                credit_stuck_count += 1
                stuck_count = 0  # reset impasse counter during credit issues
                if credit_stuck_count >= 1:
                    choice = prompt_human(credit_reason, logger, project_id=project_id)
                    if choice == "q":
                        return FeatureRunStatus.INTERRUPTED
                    elif choice == "s":
                        credit_stuck_count = 0
                continue
            else:
                credit_stuck_count = 0

            # Enhancement 4: Consume monitor signals for orchestrator feedback
            from src.monitoring.signals import SignalType, get_signal_queue

            signal_queue = get_signal_queue()
            high_confidence_signals = signal_queue.get_signals(
                workflow_id=exec_id,
                min_confidence=0.7,
                consume=True,
            )
            if high_confidence_signals:
                logger.info(f"[ORCHESTRATOR] Received {len(high_confidence_signals)} monitor signals for workflow {exec_id[:8]}")
                for sig in high_confidence_signals:
                    logger.info(f"[ORCHESTRATOR] Signal: {sig}")
                    # Signal metadata could be used for more nuanced decisions
                    # For now, signals factor into stuck_count below

            hard_error, error_reason = detect_hard_error(activity.agents, activity.failed, workflow_id=exec_id)
            if hard_error:
                logger.error(f"Hard error detected: {error_reason}")
                return FeatureRunStatus.HARD_ERROR

            impasse, impasse_reason = detect_impasse(activity.agents, activity.pending, activity.in_progress, elapsed)
            # Enhancement 4: Monitor signals can also indicate impasse.
            # Require at least 2 high-confidence stuck signals to avoid false
            # positives from a single Guardian assessment firing too aggressively.
            if not impasse and high_confidence_signals:
                stuck_signals = [s for s in high_confidence_signals if s.type in (SignalType.STUCK_PATTERN, SignalType.PHASE_STUCK)]
                if len(stuck_signals) >= 2:
                    impasse = True
                    impasse_reason = f"Monitor detected {len(stuck_signals)} stuck signals: {'; '.join(s.evidence[:50] for s in stuck_signals[:3])}"
                    logger.warning(f"[ORCHESTRATOR] Signal-driven impasse: {impasse_reason}")
            if impasse:
                stuck_count += 1
                if stuck_count >= STUCK_THRESHOLD:
                    choice = prompt_human(impasse_reason, logger, project_id=project_id)
                    if choice == "q":
                        return FeatureRunStatus.INTERRUPTED
                    elif choice == "s":
                        stuck_count = 0
                        # Skip this design - terminate all active agents for this workflow
                        for a in activity.active_agents:
                            try:
                                terminate_agent_direct(a["id"])
                                logger.info(f"Terminated agent {a['id'][:8]} (skip)")
                            except Exception:
                                pass
                        return FeatureRunStatus.SKIPPED
                    else:
                        # "c" (continue) or timeout — reset stuck count and keep watching
                        stuck_count = 0
            else:
                stuck_count = 0

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return FeatureRunStatus.INTERRUPTED
    finally:
        _unregister_monitored_workflow(exec_id)
        # Clean up: terminate all agents for this workflow and mark as paused
        if exec_id:
            try:
                # Terminate all agents for this workflow first
                agents = get_agents(workflow_id=exec_id)
                for agent in agents:
                    if agent.get("status") in ACTIVE_AGENT_STATUSES:
                        try:
                            terminate_agent_direct(agent["id"])
                            logger.info(f"  Terminated agent {agent['id'][:8]} on workflow cleanup")
                        except Exception:
                            pass

                wf_status = get_workflow_status(exec_id)
                if wf_status.get("status") == "active":
                    pause_workflow_direct(exec_id)
                    logger.info(f"Paused workflow {exec_id[:8]}")
            except Exception as e:
                logger.warning(f"Workflow cleanup failed: {e}")


def run_phase0(
    sdk,
    design_entry: DesignEntry,
    project_path: Path,
    logger: OrchestratorLogger,
    state: Optional[PipelineState] = None,
    project_id: Optional[str] = None,
) -> Tuple[Optional[dict], Optional[Path]]:
    """Run Phase 0: Feature Architect to decompose design into features.

    Args:
        sdk: HephaestusSDK instance
        design_entry: Design entry being processed
        project_path: Path to the project root
        logger: Orchestrator logger
        state: Pipeline state
        project_id: AutopilotProject.id, threaded down to run_single_workflow
            for per-project stop-signal scoping (see run_single_workflow's
            own project_id docstring).

    Returns:
        Tuple of (features_json dict, designs_folder path) or (None, None) on failure
    """
    logger.info("=" * 70)
    logger.info("STAGE 1: PHASE 0 - FEATURE ARCHITECT")
    logger.info("=" * 70)

    # Tier 1: Feature rows already exist for this design — skip re-running Phase 0.
    # This is the only thing preventing _create_feature_records from creating
    # duplicate Feature rows on a re-entrant call (that function is not itself
    # idempotent), so it must be checked first and preserved as-is.
    from src.core.database import Feature as FeatureModel
    from src.core.database import get_db as _get_db

    with _get_db() as _db:
        existing_features = _db.query(FeatureModel).filter_by(design_id=design_entry.db_id).all()
        # Copy data out of session to avoid DetachedInstanceError
        existing_feature_data = [{"id": f.feature_key, "name": f.name, "scope": f.scope, "files": f.files or [], "depends_on": f.depends_on or [], "execution": f.execution} for f in existing_features]
    if existing_feature_data:
        logger.info(f"Features already exist for {design_entry.name} ({len(existing_feature_data)} features) — skipping Phase 0")
        features_json = {
            "design_name": design_entry.name,
            "features": existing_feature_data,
        }
        designs_folder = _create_designs_folder(project_path, design_entry, logger)
        _update_design_status(design_entry.db_id, "active", error=None, logger=logger)
        return features_json, designs_folder

    # Tier 1.5: no Feature rows yet (review not approved) AND Phase 0's own
    # workflow is CURRENTLY paused for review -- a backend restart re-entered
    # this function mid-wait. Re-enter the wait directly instead of falling
    # through: Tier 2 below requires wf.status == "completed", which a
    # "paused" workflow fails, and would otherwise trigger a full,
    # wasteful re-decomposition of already-finished, already-reviewed work.
    from src.core.database import Workflow as _Wf0

    with _get_db() as _db:
        paused_phase0_wf = (
            _db.query(_Wf0)
            .filter_by(design_id=design_entry.db_id, definition_id="feature_architect", paused_by="review")
            .order_by(_Wf0.created_at.desc())
            .first()
        )
        paused_phase0_wf_id = paused_phase0_wf.id if paused_phase0_wf else None
    if paused_phase0_wf_id:
        logger.info(f"Phase 0 workflow {paused_phase0_wf_id[:8]} is already paused for review — re-entering wait")
        cleared = _wait_for_phase0_review_clearance(paused_phase0_wf_id, logger, project_id=project_id)
        if not cleared:
            return None, None
        _restore_phase0_completed_status(paused_phase0_wf_id, logger)
        # Falls through to Tier 2 below, which now finds wf.status == "completed".

    # Tier 2: no Feature rows yet, but Phase 0's workflow already completed (using
    # the same PhaseExecution-status idempotency concept every other phase gets via
    # PhaseManager.mark_phase_complete) — the Feature Architect agent already
    # finished and features.json exists on disk, but _create_feature_records never
    # ran (e.g. the process crashed in between). Resume from there instead of
    # re-running the whole agent, which would waste work and risk a second LLM
    # decomposition picking different feature boundaries than the first.
    completion = _get_phase0_completion(design_entry.db_id)
    if completion is not None:
        # Reuse the ALREADY-PERSISTED designs_folder from the completed run — do
        # NOT call _create_designs_folder here, it always mints a brand-new
        # timestamped directory and would never find the prior run's output.
        designs_folder = Path(completion["designs_folder"])
        features_json_path = designs_folder / "features.json"
        if features_json_path.exists():
            try:
                features_json = json.loads(features_json_path.read_text())
                _validate_features_json(features_json)
                logger.info(f"Phase 0 workflow {completion['workflow_id'][:8]} already completed for {design_entry.name} — resuming feature-record creation without re-running the agent")
                feature_records = _create_feature_records(design_entry.db_id, features_json, designs_folder, logger)
                logger.info(f"Phase 0 resumed: {len(feature_records)} features created")
                return features_json, designs_folder
            except (json.JSONDecodeError, ValueError, OSError) as e:
                logger.warning(f"Phase 0 workflow {completion['workflow_id'][:8]} completed but its features.json could not be resumed ({e}) — falling through to a full re-run")
        else:
            # features.json not in designs_folder — the server may have
            # crashed before the copy. Try extracting from the git branch
            # (which survives worktree cleanup) before falling through to
            # a full re-run.
            branch = f"feature_architect/{design_entry.db_id or 'unknown'}"
            try:
                import subprocess

                result = subprocess.run(
                    ["git", "show", f"{branch}:.hephaestus/features.json"],
                    cwd=str(project_path),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    features_json = json.loads(result.stdout)
                    _validate_features_json(features_json)
                    # Copy to designs_folder for future recovery
                    features_json_path.parent.mkdir(parents=True, exist_ok=True)
                    features_json_path.write_text(result.stdout)
                    # Also restore scope.md files from the branch
                    for feat in features_json.get("features", []):
                        feat_id = feat.get("id", "")
                        scope_result = subprocess.run(
                            ["git", "show", f"{branch}:.hephaestus/features/{feat_id}/scope.md"],
                            cwd=str(project_path),
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        if scope_result.returncode == 0:
                            scope_dest = designs_folder / "features" / feat_id / "scope.md"
                            scope_dest.parent.mkdir(parents=True, exist_ok=True)
                            scope_dest.write_text(scope_result.stdout)
                    logger.info(f"Recovered features.json from git branch {branch} — {len(features_json.get('features', []))} features")
                    feature_records = _create_feature_records(design_entry.db_id, features_json, designs_folder, logger)
                    logger.info(f"Phase 0 resumed from branch: {len(feature_records)} features created")
                    return features_json, designs_folder
                else:
                    logger.warning(
                        f"Phase 0 workflow {completion['workflow_id'][:8]} completed but no features.json found at {features_json_path} or branch {branch} — falling through to a full re-run"
                    )
            except Exception as branch_err:
                logger.warning(
                    f"Phase 0 workflow {completion['workflow_id'][:8]} completed but "
                    f"no features.json found at {features_json_path} and branch "
                    f"recovery failed ({branch_err}) — falling through to a full re-run"
                )

    # Create permanent designs folder
    designs_folder = _create_designs_folder(project_path, design_entry, logger)
    design_entry.designs_folder = designs_folder
    # Persist to the DB now, not only after the workflow below is confirmed
    # "completed" -- _get_phase0_completion (this function's own Tier 2
    # recovery path) requires design.designs_folder to already be set to
    # find anything to recover. Without this, a Phase 0 run that gets
    # interrupted (crash, restart, or the agent simply outliving an
    # orchestrator-side false "interrupted" read) but whose agent finishes
    # anyway leaves nothing for that recovery path to find, permanently
    # stranding otherwise-good work.
    _update_design_status(design_entry.db_id, "decomposing", designs_folder=str(designs_folder), logger=logger)

    # Copy design document to permanent storage
    dest = designs_folder / design_entry.path.name
    shutil.copy2(design_entry.path, dest)
    logger.info(f"Copied design document to: {dest}")

    # Create integration worktree for Phase 0
    branch = f"feature_architect/{design_entry.db_id or 'unknown'}"
    worktree = _create_integration_worktree(project_path, design_entry.db_id or "", branch, logger)

    if worktree is None:
        logger.error("Failed to create worktree for Phase 0")
        _update_design_status(
            design_entry.db_id,
            "failed",
            error="Worktree creation failed",
            logger=logger,
        )
        return None, None

    # Only True on the genuine, full-success return below -- guards the
    # finally block's cleanup the same way the sibling worktree-cleanup
    # call at the "wf_status == 'completed'" check further down in this
    # file does, and for the same reason (see that call's own comment):
    # this used to call _cleanup_worktree() unconditionally, so ANY exit
    # from this function -- wf_status coming back "interrupted" (e.g. a
    # backend restart while this workflow was still mid-run) or "timeout",
    # not just a genuine failure -- destroyed the shared worktree a still-
    # resumable workflow needed. Observed live: a Feature Architect workflow
    # actively being iterated on (goto/arbitration cycles in progress) had
    # its worktree deleted out from under it this way, permanently losing
    # its git-excluded .hephaestus/ state (features.json, scope.md) even
    # though the workflow itself was still legitimately in progress.
    phase0_succeeded = False
    try:
        # Copy design doc into worktree
        wt_heph = worktree / CONTEXT_DIR_NAME
        wt_heph.mkdir(parents=True, exist_ok=True)
        shutil.copy2(design_entry.path, wt_heph / "design.md")

        # Launch Phase 0 workflow
        launch_params = {
            "design_document": str(design_entry.path),
            "project_path": str(project_path),
            "design_id": design_entry.db_id or "",
        }

        description = f"Phase 0: Feature Architect for {design_entry.name}"

        wf_status = run_single_workflow(
            sdk,
            "feature_architect",
            str(worktree),
            description,
            logger,
            launch_params=launch_params,
            state=state,
            max_iterations=3,
            design_id=design_entry.db_id,
            timeout_seconds=_get_phase0_timeout(),
            project_id=project_id,
        )

        if wf_status != FeatureRunStatus.COMPLETED:
            logger.error(f"Phase 0 workflow failed with status: {wf_status.value}")
            _update_design_status(
                design_entry.db_id,
                "failed",
                error=f"Phase 0 failed: {wf_status.value}",
                logger=logger,
            )
            return None, None

        # phase0_workflow_id was already persisted immediately after launch,
        # inside run_single_workflow -- see its comment for why that can't
        # wait until this synchronous "completed" return.

        # Read and validate features.json
        features_json_path = worktree / CONTEXT_DIR_NAME / "features.json"
        if not features_json_path.exists():
            # Agent may have written to a different location inside the worktree.
            # Search the whole worktree as a fallback before giving up. Deliberately
            # NOT searching any other worktree (e.g. an agent's own isolated one) --
            # if the file isn't in the shared worktree this workflow was launched
            # with, that's a worktree-tracking bug to surface loudly (see
            # cleanup_all_stale_branches's fix in worktree_manager.py), not
            # something to route around by looking elsewhere.
            candidates = [p for p in worktree.rglob("features.json") if p.stat().st_size > 0]
            if candidates:
                features_json_path = candidates[0]
                logger.warning(f"features.json not at expected path; found at {features_json_path}")
            else:
                logger.error("Phase 0 completed but features.json not found anywhere in worktree")
                _update_design_status(
                    design_entry.db_id,
                    "failed",
                    error="features.json not found",
                    logger=logger,
                )
                return None, None

        try:
            features_json = json.loads(features_json_path.read_text())
            _validate_features_json(features_json)
        except (json.JSONDecodeError, ValueError) as e:
            # Don't discard a whole Phase 0 run (worktree, agent analysis,
            # scope docs) over a fixable validation problem — ask the same
            # worktree's agent to correct it in place first. Only fail the
            # design outright if negotiation is unavailable or exhausted.
            logger.warning(f"Invalid features.json: {e} — attempting corrective negotiation")

            # Negotiation touches the DB, spawns an agent, and polls for up
            # to max_attempts * timeout_seconds -- any unexpected failure in
            # that path (e.g. create_agent_for_task_direct's app-state
            # lookup failing) must not propagate past this except block and
            # skip the design-failed bookkeeping below; treat it the same
            # as "negotiation didn't fix it" and fall through with the
            # *original* validation error, not whatever broke internally.
            fixed = False
            try:
                from src.core.database import Phase as _NegPhase
                from src.core.database import Workflow as _NegWF

                with _get_db() as _ndb:
                    neg_wf = _ndb.query(_NegWF).filter_by(design_id=design_entry.db_id, definition_id="feature_architect").order_by(_NegWF.created_at.desc()).first()
                    neg_phase = _ndb.query(_NegPhase).filter_by(workflow_id=neg_wf.id).order_by(_NegPhase.order).first() if neg_wf else None

                if neg_wf and neg_phase:
                    fixed, negotiated_json = _negotiate_validation_fix(
                        neg_wf.id,
                        neg_phase.id,
                        neg_phase.name,
                        features_json_path,
                        _validate_features_json,
                        str(e),
                        logger,
                        project_id=project_id,
                    )
                    if fixed:
                        features_json = negotiated_json
                else:
                    logger.warning("Could not locate Phase 0 workflow/phase for corrective negotiation — failing outright")
            except Exception as negotiate_err:
                logger.error(f"Corrective negotiation itself failed unexpectedly: {negotiate_err} — failing design with the original validation error")
                fixed = False

            if not fixed:
                logger.error(f"Invalid features.json (uncorrected): {e}")
                _update_design_status(
                    design_entry.db_id,
                    "failed",
                    error=f"Invalid features.json: {e}",
                    logger=logger,
                )
                return None, None

        # Copy Phase 0 outputs to permanent storage
        shutil.copy2(features_json_path, designs_folder / "features.json")

        # Copy scope.md files. Derived from features_json_path's own parent
        # (.hephaestus/), not hardcoded to the shared `worktree` -- when
        # features_json_path was found via the agent-worktree fallback above,
        # the scope.md files live next to it there too, not in the shared
        # worktree this used to assume unconditionally.
        features_dir = features_json_path.parent / "features"
        if features_dir.exists():
            for feat in features_json.get("features", []):
                feat_id = feat.get("id", "")
                scope_src = features_dir / feat_id / "scope.md"
                scope_dest = designs_folder / "features" / feat_id / "scope.md"
                scope_dest.parent.mkdir(parents=True, exist_ok=True)
                if scope_src.exists():
                    shutil.copy2(scope_src, scope_dest)
                else:
                    logger.warning(f"scope.md not found for feature {feat_id}")

        # Copy feature_review's report/result out too, same reason as
        # features.json/scope.md above: .hephaestus/ is git-excluded and
        # gets deleted entirely by _cleanup_worktree once this workflow
        # finishes, with no merge step to preserve it the way docs/*.md
        # reports survive. Without this, a clean feature_review pass (no
        # goto ever fired, so the report text never got embedded in a
        # corrective task's description either) leaves no audit trail at
        # all of what the reviewer actually checked and confirmed was fine.
        # feature_review writes to its own .hephaestus/feature_review/
        # subdirectory (Phase 2 §4.9 follow-up), the same convention every
        # other gated phase uses.
        feature_review_dir = features_json_path.parent / "feature_review"
        review_src = feature_review_dir / "feature_review.md"
        if not review_src.exists():
            # TEMPORARY (Phase 2 §4.9 follow-up) -- an in-flight Phase 0
            # run started before the normalization may still be writing
            # to the old flat .hephaestus/review.md. Remove once no such
            # run can still be active.
            legacy_review_src = features_json_path.parent / "review.md"
            if legacy_review_src.exists():
                review_src = legacy_review_src
        if review_src.exists():
            shutil.copy2(review_src, designs_folder / review_src.name)

        # Copy feature_review's HTML decomposition synopsis out too, same
        # reason and same durability requirement as feature_review.md above
        # -- this is what get_workflow_feature_report serves for the
        # "Feature Architect" row's report button, and what a human needs
        # to actually look at during the review-mode pause below.
        synopsis_src = feature_review_dir / "feature_report.html"
        if not synopsis_src.exists():
            legacy_synopsis_src = features_json_path.parent / "feature_report.html"
            if legacy_synopsis_src.exists():
                synopsis_src = legacy_synopsis_src
        if synopsis_src.exists():
            shutil.copy2(synopsis_src, designs_folder / synopsis_src.name)

        # Persist designs_folder BEFORE creating feature records so recovery is possible
        # if _create_feature_records raises (e.g. disk full). Also persist
        # phase0_workflow_id here — this is the durable completion marker
        # _get_phase0_completion checks on a future re-entrant call, so that a
        # crash between here and _create_feature_records resumes from the
        # already-completed workflow's output instead of re-running the agent.
        #
        # NOTE: deliberately NOT using state.current_workflow_id here —
        # run_single_workflow clears it back to None right before returning
        # "completed" (see its final success branch), so by this point it's
        # already gone (the same reason _run_one_feature's feature-linking
        # call now goes through _relink_features_to_workflows instead of
        # reading that field directly). Query the just-created Workflow row
        # directly instead, via the design_id/definition_id it was created
        # with — robust regardless of that state-clearing behavior.
        # Clear any stale error from a prior failed attempt on this same
        # design (e.g. a validation failure that negotiation then fixed, or
        # an earlier run that failed before a later retry succeeded) --
        # otherwise a resolved problem keeps showing up in the design modal
        # forever, since nothing else ever clears this column.
        # phase0_workflow_id was already persisted immediately after
        # run_single_workflow returned (see above). Now update the remaining
        # fields: designs_folder, error, status.
        update_kwargs = {"designs_folder": str(designs_folder), "error": None}
        from src.core.database import Workflow

        with _get_db() as _db:
            phase0_wf = _db.query(Workflow).filter_by(design_id=design_entry.db_id, definition_id="feature_architect").order_by(Workflow.created_at.desc()).first()
            phase0_wf_id = phase0_wf.id if phase0_wf else None
        if phase0_wf_id:
            _set_workflow_type(phase0_wf_id, "design")
        _update_design_status(
            design_entry.db_id,
            "active",
            logger=logger,
            **update_kwargs,
        )

        # In review mode, pause here for human review of the decomposition
        # itself, before any per-feature pipeline launches from it -- a bad
        # decomposition approved sight-unseen propagates into every
        # downstream feature. Mirrors _run_one_feature's own review gate
        # (pause after full completion, wait for clearance) but at the
        # workflow level: Feature rows don't exist yet at this point, Phase
        # 0 is what creates them. Cleared the same way a feature's pause
        # is -- the "Feature Architect" row's existing Resume action
        # (recover-workflow) already clears Workflow.paused_by for any
        # paused workflow, no new endpoint needed.
        if phase0_wf_id and _should_pause_for_review(project_id):
            _pause_phase0_for_review(phase0_wf_id, logger)
            cleared = _wait_for_phase0_review_clearance(phase0_wf_id, logger, project_id=project_id)
            if not cleared:
                # Stop signal fired, or the workflow vanished -- do not
                # create Feature rows from a decomposition nobody approved.
                return None, None
            _restore_phase0_completed_status(phase0_wf_id, logger)

        # Create Feature DB records
        feature_records = _create_feature_records(design_entry.db_id, features_json, designs_folder, logger)

        logger.info(f"Phase 0 complete: {len(feature_records)} features created")
        phase0_succeeded = True
        return features_json, designs_folder

    finally:
        # Only clean up once Phase 0 has genuinely, fully completed -- see
        # phase0_succeeded's own comment above for why this can't be
        # unconditional.
        if phase0_succeeded:
            _cleanup_worktree(worktree, branch, project_path, logger)


# ── Review mode helpers ───────────────────────────────────────────────────────


def _should_pause_for_review(project_id: str) -> bool:
    """Return True if this project has review_mode enabled.

    Fails safe (True) on a DB error, not silently False. review_mode is a
    deliberate operator setting gating risky autonomous actions (Phase 0
    creating Feature rows from an unapproved decomposition, a feature
    proceeding without human sign-off) behind human approval -- for every
    caller of this function, True routes into a normal, visible "paused for
    review" state a human clears the same way a genuine review pause is
    cleared, while a wrongly-False result here means the gate is silently
    skipped entirely, with no visible sign anything was bypassed.
    """
    try:
        from src.core.database import AutopilotProject, get_db
        with get_db() as db:
            proj = db.query(AutopilotProject).get(project_id)
            return bool(proj and getattr(proj, "review_mode", False))
    except Exception:
        return True


def _pause_feature_for_review(feature_id: str, logger: "OrchestratorLogger") -> None:
    """Pause a feature's workflow with paused_by='review'.

    The self-heal sweep skips any workflow with paused_by set, so this
    prevents auto-resume without touching any other code paths.
    """
    try:
        from src.core.database import Feature, Workflow, get_db
        with get_db() as db:
            feat = db.query(Feature).filter_by(id=feature_id).first()
            if feat and feat.workflow_id:
                # Don't pause for review if feature was already approved
                if feat.review_status == "approved":
                    logger.debug(f"[REVIEW] Feature {feature_id} already approved — skipping review pause")
                    return
                wf = db.query(Workflow).filter_by(id=feat.workflow_id).first()
                if wf and wf.paused_by != "review" and wf.status != "failed":
                    from src.autopilot.orchestrator.engine_client import pause_workflow
                    # cascade_to_feature=False: this function already owns
                    # the write for `feat` specifically, below.
                    pause_workflow(wf.id, reason="review", cascade_to_feature=False, session=db)
                    feat.status = "paused"
                    db.commit()
                    logger.info(f"[REVIEW] Feature {feature_id} paused for review")
    except Exception as e:
        logger.error(f"[REVIEW] Failed to pause feature {feature_id} for review: {e}")


def _wait_for_review_clearance(
    feature_id: str,
    logger: "OrchestratorLogger",
    project_id: Optional[str] = None,
    poll_interval: int = 30,
) -> None:
    """Block until the feature's review pause is cleared (paused_by != 'review').

    Polls the DB every poll_interval seconds. Returns when:
    - the user approves or requests changes (paused_by cleared), OR
    - the pipeline stop signal fires (_should_stop returns True).
    Waits indefinitely otherwise — Review Mode requires explicit human action.
    """
    logger.info(f"[REVIEW] Waiting for human review of feature {feature_id}")
    while True:
        if project_id and _should_stop(project_id):
            logger.info(f"[REVIEW] Stop signal — exiting review wait for {feature_id}")
            return
        try:
            from src.core.database import Feature, Workflow, get_db
            with get_db() as db:
                feat = db.query(Feature).filter_by(id=feature_id).first()
                if not feat or not feat.workflow_id:
                    logger.info(f"[REVIEW] Feature {feature_id} no longer exists — exiting review wait")
                    return
                wf = db.query(Workflow).filter_by(id=feat.workflow_id).first()
                if not wf or wf.paused_by != "review":
                    logger.info(f"[REVIEW] Review cleared for feature {feature_id}")
                    return
        except Exception as e:
            logger.error(f"[REVIEW] Error checking review status: {e}")
        time.sleep(poll_interval)


def _restore_phase0_completed_status(workflow_id: str, logger: "OrchestratorLogger") -> None:
    """Restore Workflow.status to "completed" after a review pause clears.

    The generic resume/recover-workflow action that clears
    Workflow.paused_by (the "Feature Architect" row's Resume button) sets
    Workflow.status="active", not "completed" -- correct for a workflow
    that genuinely has more work to do, but Phase 0's decomposition work
    is already fully done at this point; "paused for review" was an
    additional gate on top of completion, not a different lifecycle state.
    Left as "active", _get_phase0_completion's Tier-2 recovery check
    (`wf.status != "completed"`) would never recognize this workflow as
    done again, wastefully re-running the whole decomposition from scratch
    on any later, unrelated backend restart.
    """
    try:
        from src.core.database import Workflow, get_db
        with get_db() as db:
            wf = db.query(Workflow).filter_by(id=workflow_id).first()
            if wf and wf.status != "completed":
                wf.status = "completed"
                db.commit()
                logger.info(f"[REVIEW] Restored Phase 0 workflow {workflow_id[:8]} status to completed after review clearance")
    except Exception as e:
        logger.error(f"[REVIEW] Failed to restore Phase 0 workflow {workflow_id[:8]} status after review: {e}")


def _pause_phase0_for_review(workflow_id: str, logger: "OrchestratorLogger") -> None:
    """Pause Phase 0's own workflow with paused_by='review'.

    Mirrors _pause_feature_for_review, but there's no Feature row to flip
    to "paused" at this point -- Phase 0 is what creates them, so the
    workflow itself is paused directly instead of through one.
    """
    try:
        from src.core.database import Workflow, get_db
        with get_db() as db:
            wf = db.query(Workflow).filter_by(id=workflow_id).first()
            # "completed" is a valid source state, not just "active" --
            # finalize_phase0_workflow calls this AFTER _complete_workflow
            # has already set wf.status="completed" (the out-of-band
            # completion path, as opposed to run_phase0's own call, which
            # pauses before that status is set). "Paused for review" is an
            # additional gate layered on top of an already-finished
            # decomposition either way -- see _restore_phase0_completed_
            # status's identical reasoning. Only "failed" is excluded: a
            # workflow that didn't actually finish has nothing to review.
            if wf and wf.paused_by != "review" and wf.status != "failed":
                from src.autopilot.orchestrator.engine_client import pause_workflow
                # No Feature row to cascade to at this point -- see this
                # function's own docstring.
                pause_workflow(wf.id, reason="review", cascade_to_feature=False, session=db)
                db.commit()
                logger.info(f"[REVIEW] Phase 0 workflow {workflow_id[:8]} paused for review")
    except Exception as e:
        logger.error(f"[REVIEW] Failed to pause Phase 0 workflow {workflow_id[:8]} for review: {e}")


def _wait_for_phase0_review_clearance(
    workflow_id: str,
    logger: "OrchestratorLogger",
    project_id: Optional[str] = None,
    poll_interval: int = 30,
) -> bool:
    """Block until Phase 0's review pause is cleared (paused_by != 'review').

    Mirrors _wait_for_review_clearance, polling the workflow directly since
    Phase 0 has no Feature row to key off of at this point.

    Returns True when the user clears the pause (e.g. via the same
    resume/recover action the "Feature Architect" row's Resume button
    already sends -- that generic action sets Workflow.status="active",
    NOT "completed"; the caller must restore "completed" itself, since
    _get_phase0_completion's own Tier-2 recovery check depends on it and
    Phase 0's work is genuinely done, not merely resumed). Returns False if
    the pipeline stop signal fired first or the workflow disappeared, in
    which case the caller must not proceed to _create_feature_records.
    Waits indefinitely otherwise -- Review Mode requires explicit human action.
    """
    logger.info(f"[REVIEW] Waiting for human review of Phase 0 decomposition (workflow {workflow_id[:8]})")
    while True:
        if project_id and _should_stop(project_id):
            logger.info(f"[REVIEW] Stop signal — exiting Phase 0 review wait for {workflow_id[:8]}")
            return False
        try:
            from src.core.database import Workflow, get_db
            with get_db() as db:
                wf = db.query(Workflow).filter_by(id=workflow_id).first()
                if not wf:
                    logger.info(f"[REVIEW] Phase 0 workflow {workflow_id[:8]} no longer exists — exiting review wait")
                    return False
                if wf.paused_by != "review":
                    logger.info(f"[REVIEW] Review cleared for Phase 0 workflow {workflow_id[:8]}")
                    return True
        except Exception as e:
            logger.error(f"[REVIEW] Error checking Phase 0 review status: {e}")
        time.sleep(poll_interval)


def finalize_phase0_workflow(
    workflow_id: str,
    logger: "OrchestratorLogger",
    project_id: Optional[str] = None,
    skip_review_gate: bool = False,
) -> bool:
    """Copy a completed Phase 0 workflow's outputs to permanent storage and
    create its Feature DB records.

    run_phase0's own tail does this synchronously while it still holds the
    live worktree, but that path only runs if run_phase0's own call to
    run_single_workflow is the thing that observes the workflow reach
    "completed" -- e.g. a backend restart mid-wait leaves the workflow
    genuinely completed in the DB with no Feature rows and no one left to
    finish the bookkeeping (root cause of the FRONTEND_DESIGN.md incident:
    zero Feature rows despite a fully completed Phase 0 workflow, with no
    recovery short of "Rerun"). This is a second, independent path to the
    same result, callable from anywhere a phase0-type Workflow is observed
    transitioning to "completed" -- not just run_phase0's synchronous wait.

    Idempotent: returns True immediately if Feature rows already exist for
    the design, so it's safe to call even when run_phase0's own path also
    eventually completes the same workflow.
    """
    from src.core.database import AutopilotDesign, Feature, Workflow, get_db

    with get_db() as db:
        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        if not wf or not wf.design_id:
            return False
        design_id = wf.design_id
        if db.query(Feature).filter_by(design_id=design_id).first():
            return True
        design = db.query(AutopilotDesign).filter_by(id=design_id).first()
        if not design:
            return False
        designs_folder = Path(design.designs_folder) if design.designs_folder else None
        working_directory = Path(wf.working_directory) if wf.working_directory else None

    if not designs_folder:
        logger.warning(f"[PHASE0-FINALIZE] Workflow {workflow_id[:8]}: no designs_folder recorded, cannot finalize")
        return False
    designs_folder.mkdir(parents=True, exist_ok=True)

    features_json_path = None
    for candidate_base in filter(None, [working_directory and working_directory / CONTEXT_DIR_NAME, designs_folder]):
        candidate = candidate_base / "features.json"
        if candidate.is_file():
            features_json_path = candidate
            break
    if not features_json_path:
        logger.warning(f"[PHASE0-FINALIZE] Workflow {workflow_id[:8]}: features.json not found in worktree or designs_folder -- nothing to recover")
        return False

    try:
        features_json = json.loads(features_json_path.read_text())
        _validate_features_json(features_json)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"[PHASE0-FINALIZE] Workflow {workflow_id[:8]}: invalid features.json ({e})")
        return False

    if features_json_path != designs_folder / "features.json":
        shutil.copy2(features_json_path, designs_folder / "features.json")
    features_dir = features_json_path.parent / "features"
    if features_dir.exists():
        for feat in features_json.get("features", []):
            feat_id = feat.get("id", "")
            scope_src = features_dir / feat_id / "scope.md"
            scope_dest = designs_folder / "features" / feat_id / "scope.md"
            scope_dest.parent.mkdir(parents=True, exist_ok=True)
            if scope_src.exists() and scope_src != scope_dest:
                shutil.copy2(scope_src, scope_dest)
    # feature_review writes to its own .hephaestus/feature_review/
    # subdirectory (Phase 2 §4.9 follow-up), same as the mirrored copy in
    # run_phase0 above.
    feature_review_dir = features_json_path.parent / "feature_review"
    review_src = feature_review_dir / "feature_review.md"
    if not review_src.exists():
        # TEMPORARY (Phase 2 §4.9 follow-up) -- see the matching fallback
        # in run_phase0 above. Remove once no in-flight run predating the
        # normalization can still be active.
        legacy_review_src = features_json_path.parent / "review.md"
        if legacy_review_src.exists():
            review_src = legacy_review_src
    if review_src.exists() and review_src != designs_folder / review_src.name:
        shutil.copy2(review_src, designs_folder / review_src.name)
    synopsis_src = feature_review_dir / "feature_report.html"
    if not synopsis_src.exists():
        legacy_synopsis_src = features_json_path.parent / "feature_report.html"
        if legacy_synopsis_src.exists():
            synopsis_src = legacy_synopsis_src
    if synopsis_src.exists() and synopsis_src != designs_folder / synopsis_src.name:
        shutil.copy2(synopsis_src, designs_folder / synopsis_src.name)

    _set_workflow_type(workflow_id, "design")
    _update_design_status(design_id, "active", designs_folder=str(designs_folder), error=None, logger=logger)

    if not skip_review_gate and _should_pause_for_review(project_id):
        _pause_phase0_for_review(workflow_id, logger)
        # Non-blocking: unlike run_phase0's own path, this is called from
        # inline phase-advancement code (PhaseManager._complete_workflow),
        # not a dedicated background wait loop. The workflow stays paused
        # for review; the human's eventual Approve action re-invokes this
        # function with skip_review_gate=True to finish the job.
        return False

    feature_records = _create_feature_records(design_id, features_json, designs_folder, logger)
    logger.info(f"[PHASE0-FINALIZE] Workflow {workflow_id[:8]}: {len(feature_records)} feature(s) created")
    return True


def _wait_for_pending_reviews(
    project_id: str,
    logger: "OrchestratorLogger",
    poll_interval: int = 30,
) -> None:
    """Block until all features -- and any Phase 0 decomposition -- pending
    review are approved.

    Called before starting new features to ensure review mode gates
    the entire pipeline, not just individual features.
    """
    from src.core.database import Feature, Workflow, get_db

    while True:
        try:
            with get_db() as db:
                pending_feature_reviews = (
                    db.query(Feature)
                    .join(Workflow, Feature.workflow_id == Workflow.id)
                    .filter(
                        Workflow.project_id == project_id,
                        Workflow.paused_by == "review",
                        Workflow.status == "paused",
                    )
                    .count()
                )
                # Phase 0 workflows have no Feature row to join through --
                # they're what CREATES Feature rows -- so a paused-for-
                # review decomposition (see _pause_phase0_for_review) is
                # otherwise invisible to the query above. Without this, a
                # different design's Phase 0 sitting paused for review
                # wouldn't block this design's next feature-execution-
                # group from starting, defeating the "gates the entire
                # pipeline" guarantee this function exists to provide.
                pending_phase0_reviews = (
                    db.query(Workflow)
                    .filter(
                        Workflow.project_id == project_id,
                        Workflow.definition_id.in_(PHASE0_DEFINITION_IDS),
                        Workflow.paused_by == "review",
                        Workflow.status == "paused",
                    )
                    .count()
                )
                pending_reviews = pending_feature_reviews + pending_phase0_reviews
                if pending_reviews == 0:
                    return
                logger.info(f"[REVIEW] Waiting for {pending_reviews} pending review(s) before starting new features")
        except Exception as e:
            logger.error(f"[REVIEW] Error checking pending reviews: {e}")
        time.sleep(poll_interval)


def _run_one_feature(
    sdk,
    design_entry: DesignEntry,
    feature: dict,
    designs_folder: Path,
    project_path: Path,
    logger: OrchestratorLogger,
    state: Optional[PipelineState] = None,
    max_iterations: int = 10,
    project_id: Optional[str] = None,
) -> FeatureRunStatus:
    """Run a single feature through the 12-phase pipeline.

    Args:
        sdk: HephaestusSDK instance
        design_entry: Design entry being processed
        feature: Feature dict from features.json
        designs_folder: Path to permanent storage
        project_path: Path to the project root
        logger: Orchestrator logger
        state: Pipeline state
        max_iterations: Max iterations for the pipeline
        project_id: AutopilotProject.id, threaded down to run_single_workflow
            for per-project stop-signal scoping.

    Returns:
        Feature status string (completed, failed, skipped)
    """
    feature_key = feature.get("id", "unknown")
    feature_name = feature.get("name", feature_key)

    logger.info(f"Starting feature pipeline: {feature_name} ({feature_key})")

    # Feature isn't in the module-level database import list (unlike
    # Workflow/get_db, already imported at top of this file). Imported
    # once here, at the top of the function, and used for every Feature
    # lookup below -- a second, later local re-import of it (or of
    # Workflow/get_db) anywhere else in this function would shadow the
    # name for the function's ENTIRE body, not just from that later point
    # on: Python treats a name as function-local if it's assigned
    # ANYWHERE in the function, including via a later import statement.
    # That exact mistake used to make the depends_on check below raise
    # "cannot access local variable 'get_db'"/'Feature' the moment any
    # feature actually had a dependency.
    from src.core.database import Feature

    # Check if all dependencies are completed before starting
    depends_on = feature.get("depends_on", [])
    if depends_on:
        with get_db() as db:
            for dep_key in depends_on:
                dep_feature = db.query(Feature).filter_by(
                    design_id=design_entry.db_id,
                    feature_key=dep_key,
                ).first()
                if not dep_feature:
                    logger.warning(f"[DEPENDENCY] Feature {feature_key} depends on {dep_key} which doesn't exist — skipping")
                    return FeatureRunStatus.SKIPPED
                if dep_feature.status not in ("completed", "active"):
                    logger.warning(f"[DEPENDENCY] Feature {feature_key} depends on {dep_key} which is {dep_feature.status} — skipping")
                    return FeatureRunStatus.SKIPPED

    # Set structured log context for this feature's lifetime
    from src.core.log_context import set_log_context
    set_log_context(workflow=feature_key, phase="feature_pipeline")

    # Find feature record in DB
    from src.core.cost_derivation import check_budget_before_new_work

    feature_id = None
    existing_workflow_id = None
    with get_db() as db:
        # Budget guard: refuse to launch features for over-budget projects
        # (inside same session to avoid race condition with concurrent cost writes)
        if project_id and not check_budget_before_new_work(db, project_id):
            logger.warning(f"[BUDGET] Cannot launch feature {feature_key} — project {project_id[:8]} over budget")
            return FeatureRunStatus.SKIPPED

        feat_record = (
            db.query(Feature)
            .filter_by(
                design_id=design_entry.db_id,
                feature_key=feature_key,
            )
            .first()
        )
        if feat_record:
            feature_id = feat_record.id

            # Resume support: a design that was Phase-0'd, then had this
            # feature's pipeline stopped mid-flight (service stop/pause),
            # lands back here on a later "play" with feat_record.status
            # still "active"/"failed" and workflow_id already pointing at
            # the workflow that was running. Without this check, a resumed
            # design's feature loop would always start a brand new workflow
            # from scratch for every feature, discarding whatever phases had
            # already completed.
            if feat_record.workflow_id:
                wf = db.query(Workflow).filter_by(id=feat_record.workflow_id).first()
                if wf and wf.status == "completed":
                    logger.info(f"Feature {feature_key} already completed (workflow {wf.id[:8]}) — skipping")
                    # feat_record.status may still be "active" from the run
                    # that actually did the work, if this function returned
                    # on that earlier call before reaching its own
                    # _update_feature_status(..., "completed", ...) call
                    # below (e.g. a backend restart re-entered this function
                    # for the same feature after the workflow had already
                    # finished) -- sync it here too, since this is also a
                    # legitimate "the feature is done" exit path.
                    if feat_record.status != "completed":
                        feat_record.status = "completed"
                        feat_record.completed_at = feat_record.completed_at or datetime.utcnow()
                        db.commit()
                    # Clean up worktree — branch and path are deterministic
                    # from design_id + feature_key, same as _run_one_feature's
                    # normal completion path.
                    _design_slug = (design_entry.db_id or "unknown")[:8]
                    _branch = f"feature/{_design_slug}/{feature_key}"
                    _wt = _create_integration_worktree(
                        project_path, feature_key, _branch, logger
                    )
                    if _wt:
                        _cleanup_worktree(_wt, _branch, project_path, logger)
                    return FeatureRunStatus.COMPLETED
                if wf:
                    existing_workflow_id = wf.id

            # Budget guard: block new workflow launches if project is over budget
            # Uses same DB session to avoid stale reads under concurrent cost recording
            if project_id:
                from src.core.cost_derivation import check_budget_before_new_work

                if not check_budget_before_new_work(db, project_id):
                    logger.info(
                        f"[BUDGET] Project over budget — blocking new workflow for feature {feature_key}"
                    )
                    _update_feature_status(
                        feature_id, design_entry.db_id, "paused", "Budget limit reached", logger
                    )
                    return FeatureRunStatus.BUDGET_BLOCKED

            # Update status to active
            feat_record.status = "active"
            feat_record.started_at = feat_record.started_at or datetime.utcnow()
            db.commit()

    if not feature_id:
        logger.error(f"Feature record not found for {feature_key}")
        return FeatureRunStatus.FAILED

    # Create feature record folder
    feature_record_path = designs_folder / "features" / feature_key
    feature_record_path.mkdir(parents=True, exist_ok=True)

    # Include design_id in the branch name to prevent collision when two designs
    # share a feature with the same key (e.g. both have an "auth" feature).
    design_slug = (design_entry.db_id or "unknown")[:8]
    branch = f"feature/{design_slug}/{feature_key}"
    worktree = _create_integration_worktree(project_path, feature_key, branch, logger)

    if worktree is None:
        logger.error(f"Failed to create worktree for feature {feature_key}")
        _update_feature_status(feature_id, design_entry.db_id, "failed", "Worktree creation failed", logger)
        return FeatureRunStatus.FAILED

    try:
        # Populate .hephaestus/ in worktree
        wt_heph = worktree / CONTEXT_DIR_NAME
        wt_heph.mkdir(parents=True, exist_ok=True)

        # Copy design document
        shutil.copy2(design_entry.path, wt_heph / "design.md")

        # Copy features.json
        features_json_path = designs_folder / "features.json"
        if features_json_path.exists():
            shutil.copy2(features_json_path, wt_heph / "features.json")

        # Copy scope.md for this feature
        scope_src = designs_folder / "features" / feature_key / "scope.md"
        scope_dest = wt_heph / "features" / feature_key / "scope.md"
        scope_dest.parent.mkdir(parents=True, exist_ok=True)
        if scope_src.exists():
            shutil.copy2(scope_src, scope_dest)

        # Launch autopilot workflow (12-phase)
        launch_params = {
            "design_document": str(design_entry.path),
            "project_path": str(project_path),
            "feature_id": feature_key,
            "feature_scope": str(wt_heph / "features" / feature_key / "scope.md"),
            "project_context": f"Building feature: {feature_name}. Scope: {wt_heph / 'features' / feature_key / 'scope.md'}",
        }

        description = f"Autopilot: {design_entry.name} - Feature: {feature_name}"

        # Set workflow type and link to feature
        # Note: We'll do this after workflow is created

        # run_single_workflow mutates state.current_workflow_id/_design_branch/
        # _design_worktree while it launches and polls the workflow. When
        # features run in parallel (run_feature_pipelines' ThreadPoolExecutor),
        # every thread is handed the SAME PipelineState object -- without a
        # thread-local copy here, run_single_workflow's own INTERNAL use of
        # these fields while polling would race across threads. The
        # status-display fields (designs_processed, current_design, ...) are
        # untouched by run_single_workflow and stay correctly shared via
        # `state`.
        thread_state = copy.copy(state) if state else None

        wf_status = run_single_workflow(
            sdk,
            "autopilot",
            str(worktree),
            description,
            logger,
            launch_params=launch_params,
            state=thread_state,
            # Deliberately NOT passing max_iterations here -- this
            # function's own `max_iterations` parameter carries the CLI's
            # --max-iterations value (a DIFFERENT, design-level retry
            # concept -- see MAX_DESIGN_RETRIES), not this workflow's
            # goto budget. run_single_workflow's own default (None) means
            # "no override", letting workflow.yaml's real max_total_gotos
            # (30, for the "autopilot" definition) apply as intended. See
            # the long comment on run_single_workflow's max_iterations
            # parameter for the incident this closes.
            design_id=design_entry.db_id,
            pause_existing=False,  # features run in parallel; don't clobber each other
            existing_workflow_id=existing_workflow_id,
            project_id=project_id,
        )

        # Link workflow to feature in DB. Deliberately NOT reading
        # thread_state.current_workflow_id here -- run_single_workflow
        # clears it back to None right before returning "completed" (see
        # its final success branch), so it's always empty by this point;
        # that made this a permanent no-op regardless of thread isolation
        # (see run_phase0's analogous phase0_workflow_id persistence for
        # the same reasoning). Resolve via the DB instead, matching this
        # design's just-created/resumed workflow by feature_key in
        # launch_params -- the same lookup _relink_features_to_workflows
        # already does for pipeline-restart recovery.
        if feature_id:
            _relink_features_to_workflows(design_entry.db_id, logger)

        # Determine final status
        if wf_status == FeatureRunStatus.COMPLETED:
            # Check if product validation passed
            # For now, mark as completed if workflow completed
            # Review mode: pause for human approval BEFORE marking completed
            if project_id and feature_id and _should_pause_for_review(project_id):
                _pause_feature_for_review(feature_id, logger)
                _wait_for_review_clearance(feature_id, logger, project_id=project_id)
            final_status = FeatureRunStatus.COMPLETED
        elif wf_status == FeatureRunStatus.PAUSED:
            # Not a failure -- run_single_workflow returns PAUSED for a
            # deliberately-paused workflow, fully resumable later via
            # existing_workflow_id (same resumability the worktree-cleanup
            # guard below already grants "paused"). Marking the FEATURE
            # "failed" here rolled the whole design's derived status to
            # "failed" too (derive_design_status treats any FAILED feature
            # as design-failed), permanently, even though nothing about
            # this feature had actually gone wrong -- it just hadn't had
            # its turn yet. Observed live: features from later sequential
            # execution groups sat "paused" with zero tasks, got marked
            # "failed" here, and the design could never be picked up as
            # "active" again even after an earlier group's feature that
            # WAS actively running went on to complete successfully.
            final_status = FeatureRunStatus.PAUSED
        elif not wf_status.is_terminal:
            # INTERRUPTED/TIMEOUT mean this walk stopped watching, not
            # that the feature reached a resolution -- see
            # FeatureRunStatus's docstring. Previously collapsed into
            # "failed" here, which silently defeated
            # run_feature_pipelines' own non-terminal halt-early check one
            # level up (it can never see a status this function never
            # returns) and wrote Feature.status="failed" for a feature
            # that may still be genuinely running or resumable via
            # existing_workflow_id. Preserve it distinctly; the
            # Feature.status write below is skipped entirely for this
            # case, leaving it at whatever it already is ("active", set
            # above) since nothing about this feature is actually known
            # to have gone wrong.
            final_status = wf_status
        else:
            final_status = FeatureRunStatus.FAILED

        # Update feature status. Skipped when final_status is
        # non-terminal (INTERRUPTED/TIMEOUT): Feature.status has no value
        # for either, and writing "failed" is exactly the bug the branch
        # above exists to avoid.
        if final_status.is_terminal:
            _update_feature_status(
                feature_id, design_entry.db_id, final_status.value, logger=logger
            )

        # Sweep artifacts to permanent record. Phase reports now live under
        # .hephaestus/ (git-excluded) -- some flat at the top level
        # (requirements.md, architecture.md), some one level down
        # in a phase subdirectory (qa_validation/qa.md,
        # adversarial_review/adversarial.md, etc., per each
        # gated phase's CRITICAL PATH RULE) -- so this must recurse, not
        # just iterate the top level like the old flat docs/ layout needed.
        # Excludes tmux/ (transcript logs), features/ (Phase 0 internal
        # state), and scratch/ (agent scratch space) -- none of those are
        # phase-report artifacts.
        docs_dir = worktree / ".hephaestus"
        _sweep_excluded_dirs = {"tmux", "features", "scratch"}
        if docs_dir.exists():
            for f in docs_dir.rglob("*"):
                if not f.is_file():
                    continue
                if f.relative_to(docs_dir).parts[0] in _sweep_excluded_dirs:
                    continue
                dest = feature_record_path / f.name
                if not dest.exists():
                    shutil.copy2(f, dest)

        if wf_status == FeatureRunStatus.COMPLETED:
            # Only clean up the worktree once the feature's pipeline has
            # genuinely, permanently finished. This used to run
            # unconditionally in a `finally:` block, so a "paused"/
            # "interrupted"/"timeout"/"failed" status -- every one of them
            # resumable via the existing_workflow_id check above, which
            # re-uses this exact deterministic worktree path -- deleted the
            # worktree anyway. Root cause of "shared worktree missing" in
            # create_agent_for_task on the next resume attempt (e.g. a
            # graceful backend restart mid-pipeline returns "interrupted"
            # here, then destroyed the very worktree resume needed).
            _cleanup_worktree(worktree, branch, project_path, logger)

        # Review mode: if the project has review_mode enabled, pause here and
        # wait for explicit human approval before returning to the caller.
        # Only pause for review if the feature completed successfully.
        # _wait_for_review_clearance polls every 30 s and respects the
        # pipeline's stop_event so Stop/restart work cleanly.
        if project_id and feature_id and final_status == FeatureRunStatus.COMPLETED and _should_pause_for_review(project_id):
            _pause_feature_for_review(feature_id, logger)
            _wait_for_review_clearance(feature_id, logger, project_id=project_id)

        return final_status

    except Exception as e:
        logger.error(f"Feature pipeline failed for {feature_key}: {e}")
        _update_feature_status(feature_id, design_entry.db_id, "failed", str(e), logger)
        # Do not clean up the worktree here either -- an exception mid-
        # pipeline is exactly the case resume needs the worktree to still
        # exist for.
        return FeatureRunStatus.FAILED


def run_feature_pipelines(
    sdk,
    design_entry: DesignEntry,
    features_json: dict,
    designs_folder: Path,
    project_path: Path,
    logger: OrchestratorLogger,
    state: Optional[PipelineState] = None,
    max_iterations: int = 10,
    project_id: Optional[str] = None,
) -> Dict[str, FeatureRunStatus]:
    """Run feature pipelines with parallel/sequential execution.

    Args:
        sdk: HephaestusSDK instance
        design_entry: Design entry being processed
        features_json: Parsed features.json content
        designs_folder: Path to permanent storage
        project_path: Path to the project root
        logger: Orchestrator logger
        state: Pipeline state
        max_iterations: Max iterations for the pipeline
        project_id: AutopilotProject.id, threaded down to each feature's
            run_single_workflow call for per-project stop-signal scoping.

    Returns:
        Dict mapping feature_key -> status
    """
    logger.info("=" * 70)
    logger.info("STAGE 2: FEATURE PIPELINES")
    logger.info("=" * 70)

    features = features_json.get("features", [])
    feature_results: Dict[str, FeatureRunStatus] = {}

    # Resolve execution order
    execution_groups = _resolve_execution_order(features, logger)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    # FeatureRunStatus members _run_one_feature/run_single_workflow can
    # return that do NOT mean the feature actually reached a resolved
    # state -- INTERRUPTED (an explicit stop/quit/KeyboardInterrupt) and
    # TIMEOUT (this poll loop's own wall-clock budget expired, reset
    # fresh on every resume -- see run_single_workflow's start_time) both
    # mean "we stopped watching," not "this feature is done." Unlike
    # FAILED/SKIPPED/HARD_ERROR (genuine, if bad, resolutions -- see the
    # comment below on why those don't block dependents), advancing to a
    # later dependency layer after one of these is exactly how a
    # still-in-progress dependency's dependents can start early: observed
    # live, a feature whose dependency was still genuinely running (its
    # own workflow status was "active", it simply hadn't finished within
    # this walk's 2-hour polling window) had its dependent feature
    # dispatched immediately after the dependency's run_single_workflow
    # call returned TIMEOUT. See FeatureRunStatus.is_terminal --
    # previously this checked a raw {"interrupted", "timeout"} string set
    # against a status _run_one_feature could never actually return (it
    # collapsed both into "failed" before returning), so this halt never
    # fired in production; _run_one_feature now preserves them distinctly.
    halted_early = False

    for group in execution_groups:
        # Every feature in the group is attempted -- a failed dependency no
        # longer auto-skips its dependents. Skipping was a one-shot,
        # permanent decision that nothing ever revisits (observed live: a
        # dependency that failed transiently, e.g. from an unrelated
        # workflow-timeout bug, later completed successfully, but its
        # dependents stayed permanently "skipped" since skip status is
        # never reconsidered). _resolve_execution_order's grouping still
        # runs dependents after their dependencies complete; it just no
        # longer discards them if a dependency didn't succeed.
        features_to_run = list(group)

        if not features_to_run:
            continue

        # Review mode: wait for any pending reviews before starting new features
        if project_id and _should_pause_for_review(project_id):
            _wait_for_pending_reviews(project_id, logger)

        # Run features in this group
        if len(features_to_run) == 1:
            # Single feature - run directly
            feat = features_to_run[0]
            feature_key = feat.get("id", "unknown")
            status = _run_one_feature(
                sdk,
                design_entry,
                feat,
                designs_folder,
                project_path,
                logger,
                state,
                max_iterations,
                project_id,
            )
            feature_results[feature_key] = status
            if not status.is_terminal:
                halted_early = True
        else:
            # Multiple parallel features - use ThreadPoolExecutor
            logger.info(f"Running {len(features_to_run)} features in parallel")

            with ThreadPoolExecutor(max_workers=MAX_PARALLEL_FEATURES) as executor:
                future_to_feature = {
                    executor.submit(
                        _run_one_feature,
                        sdk,
                        design_entry,
                        feat,
                        designs_folder,
                        project_path,
                        logger,
                        state,
                        max_iterations,
                        project_id,
                    ): feat
                    for feat in features_to_run
                }

                for future in as_completed(future_to_feature):
                    feat = future_to_feature[future]
                    feature_key = feat.get("id", "unknown")
                    try:
                        status = future.result()
                        feature_results[feature_key] = status
                        if not status.is_terminal:
                            halted_early = True
                    except Exception as e:
                        logger.error(f"Feature {feature_key} failed: {e}")
                        feature_results[feature_key] = FeatureRunStatus.FAILED

        # Stop before starting the next dependency layer -- a non-terminal
        # result means at least one feature in this layer may still be
        # genuinely in progress (or a stop was explicitly requested), so its
        # dependents in later layers must not be dispatched yet. The next
        # walk of this same design (background_phase_advancement_sweep's
        # resume, or the continuous pipeline's own re-pick) will re-resolve
        # execution_groups fresh and correctly re-encounter this layer
        # before ever reaching the ones after it.
        if halted_early:
            logger.info(
                "Halting feature pipeline walk early: a feature in this "
                "layer did not reach a resolved status (interrupted/timeout) "
                "-- not dispatching later dependency layers this walk."
            )
            break

    # Log summary
    logger.info("Feature pipeline results:")
    for feat_key, status in feature_results.items():
        logger.info(f"  {feat_key}: {status.value}")

    return feature_results


def run_design_aggregate(
    design_entry: DesignEntry,
    feature_results: Dict[str, FeatureRunStatus],
    designs_folder: Path,
    logger: OrchestratorLogger,
) -> Tuple[DesignStatus, FeatureReport]:
    """Generate aggregate design report and metrics.

    Args:
        design_entry: Design entry being processed
        feature_results: Mapping of feature_key -> status
        designs_folder: Path to permanent storage
        logger: Orchestrator logger

    Returns:
        Tuple of (DesignStatus, FeatureReport)
    """
    logger.info("=" * 70)
    logger.info("STAGE 3: DESIGN AGGREGATE")
    logger.info("=" * 70)

    # Determine overall status
    results = list(feature_results.values())
    all_completed = bool(results) and all(s == FeatureRunStatus.COMPLETED for s in results)
    any_failed = any(s == FeatureRunStatus.FAILED for s in results)
    any_completed = any(s == FeatureRunStatus.COMPLETED for s in results)
    all_skipped = bool(results) and all(s == FeatureRunStatus.SKIPPED for s in results)
    # A non-terminal result (INTERRUPTED/TIMEOUT) means at least one
    # feature never reached a resolution this walk -- run_feature_pipelines
    # halts dispatching further dependency layers when this happens, but
    # the layer that was actually running still landed here in
    # feature_results. Without this check, a mixed layer (one feature
    # already COMPLETED, another still genuinely in progress) fell through
    # to the "some skipped but >=1 completed -- partial success" branch
    # below and got marked DesignStatus.COMPLETED even though real work
    # was still outstanding -- same bucket as "not any_completed" already
    # gets today (not a new design-level state), just no longer silently
    # skipped past.
    any_non_terminal = any(not s.is_terminal for s in results)

    if all_completed:
        status = DesignStatus.COMPLETED
    elif any_failed or all_skipped or any_non_terminal or not any_completed or not results:
        # An all-skipped run (e.g. first feature failed, rest cascaded) is not a success.
        status = DesignStatus.FAILED
    else:
        # Some skipped but at least one completed — partial success.
        status = DesignStatus.COMPLETED

    # Calculate total time
    total_time = 0
    if design_entry.started_at and design_entry.completed_at:
        try:
            start = datetime.fromisoformat(design_entry.started_at)
            end = datetime.fromisoformat(design_entry.completed_at)
            total_time = int((end - start).total_seconds())
        except Exception:
            pass

    # Create FeatureReport
    report = FeatureReport(
        design_name=design_entry.name,
        project_path=str(design_entry.project_path or ""),
        feature_folder=str(designs_folder),
        design_document=str(design_entry.path),
        iterations=1,
        total_time_seconds=total_time,
        qa_passed=all_completed,
        product_validated=all_completed,
        stop_reason=status.value,
    )

    # Write design_metrics.json
    metrics = {
        "design_name": design_entry.name,
        "design_document": str(design_entry.path),
        "project_path": str(design_entry.project_path),
        "designs_folder": str(designs_folder),
        "total_time_seconds": total_time,
        "status": status.value,
        # FeatureRunStatus is a plain Enum, not json-serializable as-is.
        "features": {k: v.value for k, v in feature_results.items()},
        "completed_at": datetime.utcnow().isoformat(),
    }
    metrics_path = designs_folder / "design_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2))
    logger.info(f"Design metrics: {metrics_path}")

    # Generate design_report.html
    try:
        _generate_design_report_html(design_entry, feature_results, designs_folder, logger)
    except Exception as e:
        logger.warning(f"Failed to generate design report: {e}")

    # Update design status
    _update_design_status(
        design_entry.db_id,
        status.value,
        completed_at=datetime.utcnow(),
        logger=logger,
    )

    return status, report



def run_single_design(
    sdk,
    design_entry: DesignEntry,
    project_path: Path,
    logger: OrchestratorLogger,
    state: Optional[PipelineState] = None,
    max_iterations: int = 10,
    project_id: Optional[str] = None,
) -> Tuple[DesignStatus, FeatureReport]:
    """Three-stage coordinator: Phase 0 → per-feature pipelines → design aggregate."""
    project_path.mkdir(parents=True, exist_ok=True)
    design_entry.project_path = project_path
    # utcnow: paired with completed_at below to compute elapsed time.
    # Both use the same clock so the subtraction is self-consistent, but
    # local time jumps an hour at a DST boundary and a design run can span
    # one -- which would silently add or remove 3600s from total_time.
    design_entry.started_at = datetime.utcnow().isoformat()

    logger.info("=" * 70)
    logger.info(f"PROCESSING DESIGN: {design_entry.name}")
    logger.info(f"  Source: {design_entry.path}")
    logger.info(f"  Project: {project_path}")
    logger.info("=" * 70)

    # ── Stage 1: Phase 0 — Feature Architect ──
    features_json, designs_folder = run_phase0(sdk, design_entry, project_path, logger, state, project_id=project_id)
    if features_json is None:
        raise RuntimeError(f"Phase 0 failed to produce features.json for design '{design_entry.name}'. Check the feature_architect workflow and agent logs.")

    # ── Stage 2: Per-feature pipelines ──
    # Re-link features to their workflows if missing (handles pipeline restarts)
    _relink_features_to_workflows(design_entry.db_id, logger)

    feature_results = run_feature_pipelines(
        sdk,
        design_entry,
        features_json,
        designs_folder,
        project_path,
        logger,
        state,
        max_iterations,
        project_id=project_id,
    )

    # ── Stage 3: Design aggregate ──
    status, report = run_design_aggregate(design_entry, feature_results, designs_folder, logger)

    design_entry.completed_at = datetime.utcnow().isoformat()

    # Note: Phase 0 and feature worktrees are cleaned up by their own finally blocks
    # inside run_phase0() and _run_one_feature(). No additional cleanup needed here.

    return status, report


def _build_and_start_pipeline_sdk(args, project_path: Path, logger: OrchestratorLogger) -> Tuple[Any, str]:
    """Construct the SDK with every known workflow definition and start it.

    Returns the started SDK and the resolved CLI tool (needed by the caller
    to register the orchestrator agent). Exits the process if startup fails
    -- there is no pipeline to run without it.
    """
    sys.path.insert(0, str(HEPHAESTUS_DIR))
    from src.autopilot.phases import (
        AUTOPILOT_LAUNCH_TEMPLATE,
        AUTOPILOT_PHASES,
        AUTOPILOT_WORKFLOW_CONFIG,
    )
    from src.sdk import HephaestusSDK
    from src.sdk.models import WorkflowDefinition

    config = get_config()
    cli_tool = os.getenv("HEPHAESTUS_CLI_TOOL") or config.agents.default_cli_tool

    autopilot_def = WorkflowDefinition(
        id="autopilot",
        name="Autopilot Multi-Agent Pipeline",
        description="Continuous automated pipeline",
        phases=AUTOPILOT_PHASES,
        config=AUTOPILOT_WORKFLOW_CONFIG,
        launch_template=AUTOPILOT_LAUNCH_TEMPLATE,
    )

    # Load all workflow definitions from registry (including feature_architect)
    from src.workflow_registry import get_all_workflow_definitions

    extra_defs = [d for d in get_all_workflow_definitions() if d.id != autopilot_def.id]
    workflow_defs = [autopilot_def] + extra_defs
    if extra_defs:
        logger.info(f"Loaded extra workflow definitions: {[d.id for d in extra_defs]}")

    logger.info("Initializing SDK...")
    sdk = HephaestusSDK(
        workflow_definitions=workflow_defs,
        database_path=os.environ.get("DATABASE_PATH", str(HEPHAESTUS_DIR / "hephaestus.db")),
        qdrant_url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
        working_directory=str(project_path),
        mcp_port=int(os.environ.get("MCP_PORT", "8300")),
        monitoring_interval=config.monitoring.monitoring_interval_seconds,
        llm_provider=os.environ.get("LLM_PROVIDER", "openrouter"),
        llm_model=os.environ.get("LLM_MODEL", "xiaomi/mimo-v2.5"),
        default_cli_tool=cli_tool,
        main_repo_path=str(project_path),
        project_root=str(project_path),
        auto_commit=True,
        branch_prefix="agent-",
    )

    logger.info("Starting services...")
    try:
        # assume_backend_running: set when args came from AutopilotService's
        # in-process pipeline (see service.py's args.in_process), which is
        # itself part of the running backend process -- there is no scenario
        # where that path executes and the backend *isn't* already up.
        # Without this, sdk.start()'s pre-check is a single 2s-timeout
        # self-referential HTTP call to this same process's /health endpoint;
        # under load it can spuriously time out and conclude "not running",
        # spawning a second run_server.py that also binds port 8300 and
        # drives its own AutopilotService against the same DB (observed
        # live: two processes racing, one pausing a workflow the other had
        # just launched). Left False for the standalone
        # `python -m src.autopilot.orchestrator` CLI path (scripts/
        # autopilot.sh), where the backend genuinely may need spawning.
        sdk.start(
            enable_tui=False,
            timeout=config.autopilot.sdk_start_timeout_seconds,
            assume_backend_running=getattr(args, "in_process", False),
        )
    except Exception as e:
        logger.error(f"Failed to start: {e}")
        sys.exit(1)

    logger.info("Services started.")
    return sdk, cli_tool


def _persist_design_outcome(
    design, status, current_project_id: Optional[str], logger: OrchestratorLogger
) -> None:
    """Write a finished design's status back to the DB.

    Best-effort: a failure here must not stop the pipeline from moving on to
    the next design, since the authoritative record of what ran is the
    processed-hashes file the caller has already updated.
    """
    try:
        from src.core.database import AutopilotDesign, AutopilotProject
        from src.core.database import get_db as _get_db

        with _get_db() as _db:
            if current_project_id:
                _proj = _db.query(AutopilotProject).filter_by(id=current_project_id).first()
            else:
                _proj = _db.query(AutopilotProject).filter_by(is_active=True).first()
            if not _proj:
                return
            _des = _db.query(AutopilotDesign).filter_by(project_id=_proj.id, filename=design.path.name).first()
            if not _des:
                return
            _des.status = status.value if hasattr(status, "value") else str(status)
            _des.feature_folder = str(design.feature_folder) if design.feature_folder else None
            if status == DesignStatus.COMPLETED:
                _des.completed_at = datetime.utcnow()
                # Clear retry counter on success
                _delete_project_context(_db, f"autopilot_retry_{_des.id}")
            _db.commit()
    except Exception as _db_err:
        logger.warning(f"Failed to update DB design status: {_db_err}")


def _shutdown_pipeline(
    sdk,
    state: PipelineState,
    persistent_state,
    processed_hashes: set,
    project_path: Path,
    current_project_id: Optional[str],
    log_dir: Path,
    logger: OrchestratorLogger,
) -> None:
    """Final accounting, workflow pausing, and SDK shutdown."""
    state.total_elapsed = int(time.time() - state.start_time)
    state.queue_status = {"status": "stopped"}

    logger.info("")
    logger.info("=" * 70)
    logger.info("PIPELINE STOPPED")
    logger.info("=" * 70)
    logger.info(f"Total Time: {state.total_elapsed}s")
    logger.info(f"Designs Processed: {state.designs_processed}")
    logger.info(f"  Succeeded: {state.designs_succeeded}")
    logger.info(f"  Failed: {state.designs_failed}")
    logger.info(f"Logs: {log_dir}")
    logger.info("=" * 70)

    logger.save_state(state)
    persistent_state.save(state, processed_hashes)
    logger.event(
        "pipeline_stop",
        {
            "total_designs": state.designs_processed,
            "succeeded": state.designs_succeeded,
            "failed": state.designs_failed,
            "elapsed_seconds": state.total_elapsed,
        },
    )
    # terminate_agent_direct, not _update_orchestrator_status("terminated")
    # -- that raw status="terminated" write never set current_task_id=None
    # or terminated_at, the exact class of bug terminate_agent()'s own
    # docstring says every such write site must route through instead
    # (recurred independently 8 times in this codebase's history; this
    # was the 9th, for the orchestrator's own self-registered Agent row).
    _own_orchestrator_agent_id = _get_orchestrator_agent_id(current_project_id)
    if _own_orchestrator_agent_id:
        terminate_agent_direct(_own_orchestrator_agent_id)

    # Pause all active autopilot workflows belonging to THIS project.
    # Unscoped, this would forcibly pause an unrelated active workflow
    # in a different project just because this project's pipeline
    # stopped -- same class of cross-project collateral damage as the
    # stale current_workflow_id bug fixed alongside this.
    try:
        for wf in get_active_workflows(str(project_path), project_id=current_project_id):
            try:
                pause_workflow_direct(wf.get("id", ""))
                logger.info(f"Paused workflow {wf.get('id', '')[:8]}")
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Failed to pause workflows: {e}")

    if sdk is not None:
        sdk.shutdown(graceful=True, timeout=15)


def run_continuous_pipeline(args) -> None:
    log_dir = Path(AUTOPILOT_STATE_DIR) / datetime.now().strftime("run-%Y%m%d-%H%M%S")
    logger = OrchestratorLogger(log_dir)

    # Used everywhere this loop needs to tell "is this workflow/design/stop
    # request/pipeline-state ours" apart from a different project's (see
    # _workflow_belongs_to_project, pick_next_design, _should_stop,
    # PersistentPipelineState). AutopilotService.start() already resolved
    # this reliably (via _get_or_create_project_id) before this loop ever
    # began and passes it straight through args. Only the standalone CLI
    # path (`python -m src.autopilot.orchestrator`, which builds its own
    # argparse Namespace with no project_id) falls back to a DB lookup
    # further below, once project_path is available.
    current_project_id = getattr(args, "project_id", None)

    # Load persistent state from previous runs
    persistent_state = PersistentPipelineState(project_id=current_project_id)
    state, processed_hashes = persistent_state.load()

    # Check for incomplete work from previous run
    if persistent_state.has_incomplete_work():
        last_design = state.current_design
        logger.info(f"Resuming from previous run - last design: {last_design}")
        # Clear current design since we're starting fresh
        state.current_design = None
        state.current_feature_folder = None
        state.current_iteration = 0

    # Generate new run ID
    state.run_id = datetime.now().strftime("run-%Y%m%d-%H%M%S")
    state.start_time = time.time()

    logger.info("=" * 70)
    logger.info("AUTOPILOT CONTINUOUS PIPELINE")
    logger.info("=" * 70)
    logger.info(f"Design Queue: {args.design_queue}")
    logger.info(f"Project Root: {args.project_path}")
    logger.info(f"Control Model: Engine evaluation points (max_total_gotos={args.max_iterations})")
    logger.info(f"Poll Interval: {DESIGN_QUEUE_SCAN_INTERVAL}s")
    logger.info(f"Run ID: {state.run_id}")
    logger.info(f"Logs: {log_dir}")

    if processed_hashes:
        logger.info(f"Loaded {len(processed_hashes)} previously processed designs")

    logger.info("=" * 70)

    queue_dir = Path(args.design_queue)
    project_path = Path(args.project_path)
    project_path.mkdir(parents=True, exist_ok=True)
    queue_dir.mkdir(parents=True, exist_ok=True)

    # Standalone CLI path only (see current_project_id's resolution above):
    # no project_id in args, so fall back to a fresh DB lookup now that
    # project_path is available. A transient failure here leaving
    # current_project_id None for the run's duration is an acceptable
    # degradation for that path alone (it was the pre-existing behavior),
    # not a regression of AutopilotService's stop button -- that path
    # always has project_id from args.
    if not current_project_id:
        try:
            from src.core.database import AutopilotProject as _AutopilotProject

            with get_db() as _pdb:
                _proj = _pdb.query(_AutopilotProject).filter_by(base_dir=str(project_path.resolve())).first()
                if _proj:
                    current_project_id = _proj.id
        except Exception:
            pass

    processed_file = log_dir / "processed.json"

    sdk, cli_tool = _build_and_start_pipeline_sdk(args, project_path, logger)

    # Register orchestrator as an agent, keyed by project_id so a second
    # project's pipeline running concurrently can't overwrite this one's
    # registration (SOLID review 2.4).
    _orchestrator_agent_ids[current_project_id] = _register_orchestrator_agent(
        log_dir, cli_tool, logger
    )

    # NOTE: this used to unconditionally fail (or complete) every workflow
    # still "active" at startup, on the theory that "active" + backend-just-
    # restarted meant abandoned. That's no longer true: background_phase_
    # advancement_sweep, the auto-resume-on-boot path, and _run_one_feature's
    # existing_workflow_id resume branch are all specifically designed to
    # pick a genuinely active workflow back up across a restart -- an active
    # workflow with incomplete phases at boot is the NORMAL steady state,
    # not evidence of staleness. This block ran on every single restart and
    # force-failed whatever workflow was legitimately mid-flight before the
    # resume machinery ever got a chance to run (observed live: real,
    # actively-working agents killed and their workflow marked failed within
    # seconds of every backend restart, all day). A workflow that's
    # genuinely stuck (not just still in progress) is already caught more
    # carefully elsewhere -- attempt_recovery's 5-attempt escalation, which
    # verifies actual tmux liveness before giving up.

    logger.info("")
    logger.info(f"Watching design queue: {queue_dir}")
    logger.info("Drop .md or .txt files into the queue directory to add designs.")
    logger.info("Press Ctrl+C to stop.")
    logger.info("")

    last_queue_scan = 0
    # workflow_id -> consecutive count of scans where it showed zero agent/
    # task activity while blocking this gate. Reset whenever the workflow
    # drops out of the active set, or shows real activity (see the
    # escalation below).
    active_workflow_abandoned_streak: Dict[str, int] = {}

    try:
        while True:
            # Check if in-process service requested a stop
            if _should_stop(current_project_id):
                logger.info("Stop requested by AutopilotService")
                break

            now = time.time()

            if now - last_queue_scan >= DESIGN_QUEUE_SCAN_INTERVAL:
                last_queue_scan = now

                # Check if any workflow is still active - don't start a new design while one is running.
                # Scoped to this project: an active workflow in a DIFFERENT
                # project must never block this one. A workflow that stays
                # "active" with zero agent/task activity for
                # STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS consecutive scans
                # is escalated below (marked failed) instead of blocking
                # this gate forever -- e.g. a backend restart mid-flight can
                # lose the in-memory progress of a multi-feature pipeline
                # between one feature finishing and the next feature's task
                # being created, with nothing else positioned to notice or
                # resume it. A workflow with real activity is never
                # touched, no matter how long it legitimately runs.
                try:
                    active_workflows = get_active_workflows(str(project_path), project_id=current_project_id)
                    still_blocking = _escalate_stale_active_workflows(active_workflows, active_workflow_abandoned_streak, logger)
                    if still_blocking and not _has_resumable_active_design(current_project_id):
                        wf_ids = [i[:8] for i in still_blocking]
                        logger.info(f"Workflow still active ({', '.join(wf_ids)}) - waiting before picking next design")
                        state.queue_status = {
                            "status": "waiting",
                            "reason": "workflow_active",
                            "active_workflows": wf_ids,
                        }
                        logger.save_state(state)
                        persistent_state.save(state, processed_hashes)
                        time.sleep(POLL_INTERVAL)
                        continue
                    elif still_blocking:
                        wf_ids = [i[:8] for i in still_blocking]
                        logger.info(
                            f"Workflow(s) still active ({', '.join(wf_ids)}) but another design has "
                            "resumable ready features -- proceeding to pick_next_design instead of waiting"
                        )

                    # Also check previous workflow is fully complete (all phases done, branches merged).
                    # Same reasoning as the still_blocking bypass above: skip this
                    # entirely when another design already has resumable ready work
                    # -- state.current_workflow_id tracks whichever design THIS
                    # loop's own run_single_design call was last responsible for,
                    # which is a different thing from "is the project's queue
                    # allowed to make progress." Without this, a design left
                    # tracked here from before a restart (still legitimately
                    # in-progress, driven by its own agents independent of this
                    # loop) blocks pick_next_design from ever running again, the
                    # same way still_blocking did. Any genuine abandonment of
                    # THIS workflow is already caught by _escalate_stale_active_
                    # workflows above, which runs over the full project-wide
                    # active-workflow list regardless of state.current_workflow_id.
                    resumable_elsewhere = _has_resumable_active_design(current_project_id)
                    if state.current_workflow_id and resumable_elsewhere:
                        logger.info(
                            f"Previous workflow {state.current_workflow_id[:8]} not re-checked this cycle "
                            "-- another design has resumable ready features"
                        )
                    elif state.current_workflow_id:
                        # First check if workflow still exists in DB
                        try:
                            wf_check = get_workflow_status(state.current_workflow_id)
                            wf_check_status = wf_check.get("status", "")
                            if not wf_check_status:
                                # Workflow no longer exists in DB — clear stale state
                                logger.info(f"Previous workflow {state.current_workflow_id[:8]} no longer exists in DB, clearing stale state")
                                state.current_workflow_id = None
                                continue
                            # state.current_workflow_id is global, persisted
                            # pipeline state (PersistentPipelineState), NOT
                            # scoped per-project. Switching the active
                            # project in the UI and starting a new run
                            # against a different project_path used to leave
                            # this pointing at the PREVIOUS project's
                            # workflow -- the loop would then block the new
                            # project's entire queue behind an unrelated
                            # workflow it doesn't own (including a
                            # deliberately paused one), and after
                            # _recovery_attempts exhausted, force-mark that
                            # OTHER project's workflow "failed" purely as a
                            # side effect of switching projects. Observed
                            # live: switching from applitnator to Sotto
                            # force-failed applitnator's paused
                            # Authentication & Fraud Detection workflow.
                            # Uses _workflow_belongs_to_project: prefers the
                            # authoritative project_id FK, falls back to a
                            # resolved-path containment check (not a raw
                            # str.startswith() prefix match, which wrongly
                            # matched sibling directories sharing a name
                            # prefix -- e.g. "project-a" vs "project-ab" --
                            # silently reintroducing this exact bug for that
                            # narrower case). Treats "can't verify either
                            # signal" as NOT belonging (clears state rather
                            # than risk blocking/damaging a workflow we
                            # can't positively confirm is ours) -- consistent
                            # with get_active_workflows' pre-existing
                            # treatment of a missing working_directory.
                            if not _workflow_belongs_to_project(
                                wf_check.get("project_id"),
                                wf_check.get("working_directory"),
                                current_project_id,
                                str(project_path),
                            ):
                                logger.info(
                                    f"Previous workflow {state.current_workflow_id[:8]} belongs to a "
                                    f"different project (or project ownership could not be verified: "
                                    f"working_directory={wf_check.get('working_directory')!r}) "
                                    "— clearing stale state, not blocking or touching it"
                                )
                                state.current_workflow_id = None
                                continue
                        except Exception:
                            logger.info(f"Previous workflow {state.current_workflow_id[:8]} could not be checked, clearing stale state")
                            state.current_workflow_id = None
                            continue

                        is_complete, reason = is_design_fully_complete(state.current_workflow_id, logger)

                        # Periodic stale task cleanup (every cycle). Logged
                        # at warning, not debug (invisible at production log
                        # levels) -- if this starts failing every cycle,
                        # tasks stuck "assigned"/"in_progress" under
                        # terminated agents stop getting cleaned up with no
                        # visible sign the self-heal itself has stopped
                        # working.
                        try:
                            _clean_stale_assigned_tasks(state.current_workflow_id, logger)
                        except Exception as e:
                            logger.warning(f"Stale task cleanup error: {e}")

                        if not is_complete:
                            logger.info(f"Previous workflow not yet complete: {reason}")

                            # Track recovery attempts to prevent infinite
                            # loops -- see _update_resumed_workflow_recovery_
                            # attempts for why this must reset on real
                            # activity rather than ticking up regardless.
                            if not hasattr(state, "_recovery_attempts"):
                                state._recovery_attempts = 0
                            state._recovery_attempts = _update_resumed_workflow_recovery_attempts(state.current_workflow_id, state._recovery_attempts)

                            if state._recovery_attempts > 5:
                                logger.warning(f"Recovery failed after {state._recovery_attempts} attempts, escalating to impasse for workflow {state.current_workflow_id[:8]}")
                                # Mark workflow as failed — required phase was abandoned
                                try:
                                    # Aliased: a bare `get_db` import here makes
                                    # Python treat `get_db` as local for this
                                    # entire enclosing function (run_continuous_
                                    # pipeline), shadowing the module-level
                                    # import and raising UnboundLocalError at
                                    # every earlier `get_db()` call in the same
                                    # function (observed live: broke the stale-
                                    # workflow cleanup near the top of this
                                    # function, which then left a dead workflow
                                    # row permanently "active" and blocked
                                    # get_active_workflows() from ever letting a
                                    # new design start).
                                    from src.core.database import Workflow
                                    from src.core.database import get_db as _get_db2

                                    with _get_db2() as db:
                                        wf = db.query(Workflow).filter_by(id=state.current_workflow_id).first()
                                        if wf:
                                            wf.status = "failed"
                                            wf.status_reason = f"Abandoned: no agent/task activity for {state._recovery_attempts} consecutive resume attempts after a backend restart"
                                            db.commit()
                                            logger.warning(f"Workflow {state.current_workflow_id[:8]} marked as failed (abandoned phase)")
                                except Exception as e:
                                    logger.error(f"Failed to mark workflow as failed: {e}")
                                state.current_workflow_id = None
                                state._recovery_attempts = 0
                                continue

                            # Attempt recovery
                            success, recovery_msg = attempt_recovery(state.current_workflow_id, logger)
                            if success:
                                logger.info(f"Recovery actions: {recovery_msg}")

                            state.queue_status = {
                                "status": "waiting",
                                "reason": reason,
                                "recovery": recovery_msg if success else None,
                            }
                            logger.save_state(state)
                            persistent_state.save(state, processed_hashes)
                            _interruptible_sleep(POLL_INTERVAL, current_project_id)
                            continue
                        else:
                            logger.info(f"Previous workflow fully complete: {reason}")
                            state.current_workflow_id = None
                except Exception as e:
                    # This except wraps the ENTIRE protective-gating section
                    # above (still_blocking / _has_resumable_active_design /
                    # state.current_workflow_id verification) -- previously,
                    # any failure inside it (a transient DB error, not just
                    # one specific check) was logged as a mere warning and
                    # fell straight through to pick_next_design below with
                    # every protection bypassed. run_single_workflow's
                    # default pause_existing=True terminates every other
                    # active workflow's agents project-wide, so dispatching
                    # a new design here on an UNVERIFIED "nothing else is
                    # active" could kill a genuinely in-progress design's
                    # agents mid-work. Treat "couldn't verify" as "not safe
                    # to proceed yet" instead -- skip this scan cycle and
                    # let the gate re-run cleanly next time, matching the
                    # "wait and retry" pattern already used for the
                    # confirmed-active-workflow case above.
                    logger.warning(f"Warning: Could not check active workflows, skipping this scan cycle: {e}")
                    _interruptible_sleep(POLL_INTERVAL, current_project_id)
                    continue

                next_design = pick_next_design(queue_dir, processed_hashes, logger, project_id=current_project_id)

                if next_design is None:
                    logger.info(f"Queue empty. Scanning again in {DESIGN_QUEUE_SCAN_INTERVAL}s...")
                    state.queue_status = {
                        "status": "empty",
                        "processed": len(processed_hashes),
                    }
                    logger.save_state(state)
                    _update_orchestrator_status("idle", current_project_id)
                    persistent_state.save(state, processed_hashes)
                    _interruptible_sleep(DESIGN_QUEUE_SCAN_INTERVAL, current_project_id)
                    continue

                next_design.status = DesignStatus.IN_PROGRESS
                state.current_design = next_design.name
                state.current_feature_folder = str(next_design.feature_folder) if next_design.feature_folder else None
                state.queue_status = {
                    "status": "processing",
                    "current": next_design.name,
                    "processed": len(processed_hashes),
                }
                _update_orchestrator_status("working", current_project_id)
                # Checkpoint immediately, not just after run_single_design
                # returns (see save_state_only's docstring) -- a design's
                # run can take minutes to hours, and the status endpoint's
                # current_design reads this same persisted state.
                persistent_state.save(state, processed_hashes)

                try:
                    status, feature_report = run_single_design(
                        sdk,
                        next_design,
                        project_path,
                        logger,
                        state,
                        max_iterations=args.max_iterations,
                        project_id=current_project_id,
                    )
                    # Save state AFTER run_single_design so current_workflow_id is captured
                    logger.save_state(state)
                    persistent_state.save(state, processed_hashes)
                except Exception as _design_err:
                    logger.error(f"run_single_design raised unexpectedly for '{next_design.name}': {_design_err}")
                    status = DesignStatus.FAILED
                    feature_report = _empty_report(next_design)

                next_design.status = status
                processed_hashes.add(next_design.content_hash)
                processed_file.write_text(json.dumps(list(processed_hashes)))

                _persist_design_outcome(next_design, status, current_project_id, logger)

                state.designs_processed += 1
                if status == DesignStatus.COMPLETED:
                    state.designs_succeeded += 1
                else:
                    state.designs_failed += 1

                state.current_design = None
                state.current_feature_folder = None
                state.current_iteration = 0
                state.total_elapsed = int(time.time() - state.start_time)
                state.queue_status = {
                    "status": "idle",
                    "processed": len(processed_hashes),
                    "succeeded": state.designs_succeeded,
                    "failed": state.designs_failed,
                }
                _update_orchestrator_status("idle", current_project_id)
                logger.save_state(state)
                persistent_state.save(state, processed_hashes)

                logger.event(
                    "design_complete",
                    {
                        "design": next_design.name,
                        "status": status.value,
                        "iterations": feature_report.iterations,
                        "qa_passed": feature_report.qa_passed,
                        "product_validated": feature_report.product_validated,
                        "elapsed_seconds": feature_report.total_time_seconds,
                        "feature_folder": str(next_design.feature_folder),
                    },
                )

                logger.info("")
                logger.info(f"Design '{next_design.name}' complete. Status: {status.value}")
                logger.info(f"Total designs processed: {state.designs_processed}")
                logger.info(f"  Succeeded: {state.designs_succeeded}")
                logger.info(f"  Failed: {state.designs_failed}")
                logger.info("")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        logger.info("")
        logger.info("Pipeline interrupted by user")
    finally:
        _shutdown_pipeline(
            sdk,
            state,
            persistent_state,
            processed_hashes,
            project_path,
            current_project_id,
            log_dir,
            logger,
        )


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Autopilot Continuous Pipeline - Design Queue to Validated Software")
    parser.add_argument(
        "--design-queue",
        default=None,
        help="Directory to watch for design documents (default: <project-path>/.hephaestus/designs)",
    )
    parser.add_argument(
        "--project-path",
        required=True,
        help="Project directory for implementation code",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        help="Maximum review-fix-QA iterations per design",
    )
    parser.add_argument("--drop-db", action="store_true", help="Drop database before starting")

    args = parser.parse_args()

    # Check if another orchestrator is already running
    pid_dir = Path(AUTOPILOT_STATE_DIR)
    pid_file = pid_dir / "orchestrator.pid"
    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text().strip())
            # Check if process is alive
            os.kill(existing_pid, 0)
            # Process is alive - check if it's us
            if existing_pid != os.getpid():
                sys.exit(1)
        except (ProcessLookupError, ValueError):
            # Process not alive or invalid PID, clean up
            pid_file.unlink(missing_ok=True)

    # Default design queue to <project-path>/.hephaestus/designs
    if not args.design_queue:
        args.design_queue = str(Path(args.project_path) / DESIGN_CONTEXT_SUBDIR)

    if args.drop_db:
        db = HEPHAESTUS_DIR / "hephaestus.db"
        if db.exists():
            db.unlink()

    # Ensure DB tables and migrations are applied

    db_manager = DatabaseManager(str(HEPHAESTUS_DIR / "hephaestus.db"))
    db_manager.create_tables()

    # Write our PID
    pid_dir.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    try:
        run_continuous_pipeline(args)
    finally:
        # Clean up PID file
        pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
