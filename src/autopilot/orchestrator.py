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

import asyncio
import copy
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import git as _git
import requests

from src.autopilot.spec import GATED_PHASES, build_phase_output
from src.core.constants import (
    AUTOPILOT_STATE_DIR,
    CONTEXT_DIR_NAME,
    DESIGN_CONTEXT_SUBDIR,
    DIAGNOSTIC_TASK_PREFIX,
    GOTO_REASON_PREFIX,
)
from src.core.database import (
    Agent,
    DatabaseManager,
    Phase,
    PhaseExecution,
    ProjectContext,
    Task,
    Workflow,
    get_db,
)
from src.core.simple_config import get_config
from src.phases import PhaseManager

# Module-level logger for persistent state operations
logger = logging.getLogger(__name__)

HEPHAESTUS_DIR = Path(__file__).parent.parent.parent
API_BASE = os.environ.get("HEPHAESTUS_API_BASE", "http://127.0.0.1:8300")


def _get_workflow_timeout() -> int:
    """Get workflow timeout from config, with fallback to default."""
    try:
        from src.core.simple_config import get_config

        return get_config().workflow_timeout_seconds
    except Exception:
        return 7200  # 2 hours default


def _get_phase0_timeout() -> int:
    """Get Phase 0 timeout from config, with fallback to default."""
    try:
        from src.core.simple_config import get_config

        return get_config().phase0_timeout_seconds
    except Exception:
        return 3600  # 1 hour default


def _get_paused_workflow_retry_cooldown_seconds() -> int:
    """Get the exhausted-retry-pause cooldown from config, with fallback to default."""
    try:
        from src.core.simple_config import get_config

        return get_config().paused_workflow_retry_cooldown_seconds
    except Exception:
        return 300  # 5 min default


def _get_paused_workflow_max_retry_cycles() -> int:
    """Get the exhausted-retry-pause retry cycle cap from config, with fallback to default."""
    try:
        from src.core.simple_config import get_config

        return get_config().paused_workflow_max_retry_cycles
    except Exception:
        return 10  # default


POLL_INTERVAL = 15
STUCK_THRESHOLD = 3
DESIGN_QUEUE_SCAN_INTERVAL = 60
HEARTBEAT_INTERVAL = 300
# FIX: Extracted to config (hephaestus_config.yaml -> autopilot section)
# Fallback values preserved here for backward compatibility.
MAX_WORKFLOW_TIME = 7200  # 2 hours per workflow execution (deprecated: use config)
ACTIVE_AGENT_STATUSES = {
    "working",
    "idle",
}  # Excludes 'created' (not yet started), 'stuck', 'terminated'
PARENT_PEEK_INTERVAL = int(os.environ.get("HEPH_PEEK_INTERVAL", "60"))  # seconds between parent peeks

# Feature Model constants
# FIX: Extracted to config (hephaestus_config.yaml -> autopilot section)
MAX_PHASE0_TIME = 3600  # 1 hour timeout for Phase 0 (deprecated: use config)
MAX_PARALLEL_FEATURES = 4  # max concurrent feature pipelines
MAX_DESIGN_RETRIES = 3  # max times a failed design is auto-retried
# How many CONSECUTIVE design-queue scans (each DESIGN_QUEUE_SCAN_INTERVAL
# apart) a workflow can show zero agent/task activity while "active" before
# the "wait for active workflow" gate gives up on it as abandoned -- see
# _escalate_stale_active_workflows. Consecutive, not elapsed-time-since-
# first-seen: a single activity blip resets the streak, so this only fires
# on genuinely sustained abandonment, matching the same
# "self-healing an infinite wait" pattern as the state.current_workflow_id
# escalation nearby (5 consecutive not-yet-complete checks).
STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS = 10  # ~10 min at the default 60s scan interval

# How long a PhaseExecution's task_creation_claimed_at can be held before
# _case_in_progress_complete treats it as abandoned rather than "still being
# created elsewhere" -- see the staleness check there. A legitimate holder
# (first-task creation, or an arbitration task's whole lifetime) finishes in
# well under this; anything still holding it this long had its releaser
# crash, get killed, or (as observed live) simply predate the claim/release
# wiring being added at all, permanently hiding the phase from completion
# detection -- no matter how many times its task actually finished.
CLAIM_STALE_TIMEOUT_SECONDS = 480  # 8 minutes -- must stay shorter than
# STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS * DESIGN_QUEUE_SCAN_INTERVAL
# (10 * 60s = 600s), or a workflow whose only problem is a stuck claim gets
# killed by that other escalation before this one ever gets a chance to
# clear it and let the workflow self-heal instead.

# Module-level orchestrator agent ID (set during registration)
_orchestrator_agent_id: Optional[str] = None


def get_litellm_config() -> Dict[str, str]:
    """Read LiteLLM proxy config from environment variables."""
    return {
        "url": os.environ.get("LITELLM_PROXY_URL", ""),
        "api_key": os.environ.get("LITELLM_API_KEY", ""),
        "cost_api_key": os.environ.get("LITELLM_MASTER_KEY", ""),
        "cost_tracking": os.environ.get("LITELLM_COST_TRACKING", "false").lower() == "true",
    }


class StopReason(Enum):
    COMPLETED = "completed"
    HARD_ERROR = "hard_error"
    IMPASSE = "impasse"
    ARCHITECTURAL_ISSUE = "architectural_issue"
    MAX_ITERATIONS = "max_iterations"
    CREDIT_EXHAUSTED = "credit_exhausted"
    USER_INTERRUPT = "user_interrupt"
    USER_SKIP = "user_skip"
    QUEUE_EMPTY = "queue_empty"


class DesignStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class DesignEntry:
    path: Path
    name: str
    content_hash: str
    status: DesignStatus = DesignStatus.PENDING
    db_id: Optional[str] = None  # autopilot_designs.id — links Workflow back to Design (§9.7)
    project_path: Optional[Path] = None
    feature_folder: Optional[Path] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    file_path: Optional[str] = None  # absolute path to design file
    designs_folder: Optional[Path] = None  # path to permanent storage


@dataclass
class IterationResult:
    iteration: int
    status: str
    qa_passed: bool
    product_validated: bool
    issues_found: List[str]
    fixes_applied: List[str]
    elapsed_seconds: int
    stop_reason: Optional[StopReason] = None


@dataclass
class FeatureReport:
    design_name: str
    project_path: str
    feature_folder: str
    design_document: str
    iterations: int
    total_time_seconds: int
    qa_passed: bool
    product_validated: bool
    stop_reason: str
    requirements_summary: str = ""
    architecture_summary: str = ""
    doc_review_summary: str = ""
    security_summary: str = ""
    qa_summary: str = ""
    product_validation_summary: str = ""
    forensics_summary: str = ""
    files_created: List[str] = field(default_factory=list)
    issues_resolved: List[str] = field(default_factory=list)
    outstanding_issues: List[str] = field(default_factory=list)
    cost_total: float = 0.0
    cost_breakdown: Dict[str, float] = field(default_factory=dict)
    cost_currency: str = "USD"


@dataclass
class PipelineState:
    designs_processed: int = 0
    designs_succeeded: int = 0
    designs_failed: int = 0
    total_elapsed: int = 0
    current_design: Optional[str] = None
    current_workflow_id: Optional[str] = None
    current_feature_folder: Optional[str] = None
    current_iteration: int = 0
    queue_status: Dict[str, str] = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)
    run_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "designs_processed": self.designs_processed,
            "designs_succeeded": self.designs_succeeded,
            "designs_failed": self.designs_failed,
            "total_elapsed": self.total_elapsed,
            "current_design": self.current_design,
            "current_workflow_id": self.current_workflow_id,
            "current_feature_folder": self.current_feature_folder,
            "current_iteration": self.current_iteration,
            "queue_status": self.queue_status,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PipelineState":
        state = cls()
        state.designs_processed = data.get("designs_processed", 0)
        state.designs_succeeded = data.get("designs_succeeded", 0)
        state.designs_failed = data.get("designs_failed", 0)
        state.total_elapsed = data.get("total_elapsed", 0)
        state.current_design = data.get("current_design")
        state.current_workflow_id = data.get("current_workflow_id")
        state.current_feature_folder = data.get("current_feature_folder")
        state.current_iteration = data.get("current_iteration", 0)
        state.queue_status = data.get("queue_status", {})
        state.run_id = data.get("run_id")
        return state


def _get_project_context(db, key: str):
    """Read a ProjectContext value by key, or None if unset."""
    row = db.query(ProjectContext).filter_by(key=key).first()
    return row.value if row else None


def _set_project_context(db, key: str, value) -> None:
    """Upsert a ProjectContext value by key.

    Uses SQLite's INSERT ... ON CONFLICT DO UPDATE (an atomic upsert)
    instead of a read-then-write. A naive filter_by(key=key).first() /
    add-or-update sequence has a real TOCTOU window: two callers writing
    the same key for the first time can both see no existing row and both
    attempt to insert, raising IntegrityError on ProjectContext.key's
    unique constraint for whichever commits second.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    now = datetime.utcnow()
    stmt = sqlite_insert(ProjectContext).values(key=key, value=value, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[ProjectContext.key],
        set_={"value": value, "updated_at": now},
    )
    db.execute(stmt)


def _delete_project_context(db, key: str) -> None:
    """Remove a ProjectContext row by key, if present."""
    db.query(ProjectContext).filter_by(key=key).delete()


def _get_project_contexts_by_prefix(db, prefix: str) -> Dict[str, Any]:
    """Read every ProjectContext row whose key starts with `prefix`.

    Used for multi-project state that's namespaced per-project by key
    (e.g. _running_state_key(project_id)) rather than looked up by one
    known key -- see AutopilotService.enumerate_persisted_states.
    """
    rows = db.query(ProjectContext).filter(ProjectContext.key.like(f"{prefix}%")).all()
    return {row.key: row.value for row in rows}


def _resolve_project_id(project_path: str) -> Optional[str]:
    """Look up AutopilotProject.id for a project root path, read-only.

    Returns None if no AutopilotProject row exists yet for this path --
    callers that need one to exist should use _get_or_create_project_id
    instead.
    """
    from src.core.database import AutopilotProject

    try:
        with get_db() as db:
            proj = db.query(AutopilotProject).filter_by(base_dir=str(Path(project_path).resolve())).first()
            return proj.id if proj else None
    except Exception:
        return None


def _get_or_create_project_id(project_path: str) -> str:
    """Get-or-create + activate the AutopilotProject row for project_path,
    and resume any workflows the user had explicitly paused for it.

    Single source of truth for logic that used to live inline in
    AutopilotService.start() only -- extracted so callers that need
    project_id BEFORE starting a pipeline (e.g. POST /start's concurrency-
    cap check, AutopilotServiceRegistry.get_or_create) don't need a second,
    divergent copy of the same insert-if-missing/activate logic.
    """
    import uuid as _uuid

    from sqlalchemy.exc import IntegrityError

    from src.core.database import AutopilotProject

    project = Path(project_path).resolve()
    with get_db() as db:
        proj = db.query(AutopilotProject).filter_by(base_dir=str(project)).first()
        if not proj:
            proj = AutopilotProject(
                id=f"proj-{_uuid.uuid4().hex[:12]}",
                name=project.name,
                base_dir=str(project),
                is_active=False,
            )
            db.add(proj)
            try:
                db.flush()
            except IntegrityError:
                # Lost a race with a concurrent first-time call for the
                # same brand-new path (base_dir is unique) -- the other
                # caller's row already exists; use it instead of failing.
                db.rollback()
                proj = db.query(AutopilotProject).filter_by(base_dir=str(project)).first()
            else:
                logger.info(f"Auto-created project '{proj.name}' for {project} (none registered)")
            _ensure_git_excluded(
                project,
                {
                    ".worktrees/": "Hephaestus's per-feature worktrees for this project --",
                    ".hephaestus/": "Hephaestus's own orchestration/scratch state for this project --",
                },
                logger,
            )

        if not proj.is_active:
            # Cap at max_concurrent_projects instead of evicting whatever
            # else is active -- this function is the actual pipeline-launch
            # path (POST /start, AutopilotService.start()), called BEFORE
            # /start's own AutopilotServiceRegistry.try_reserve() cap check.
            # The old unconditional eviction here defeated that cap
            # entirely: starting project B would silently deactivate
            # project A's is_active flag (and with it, the phase-
            # advancement sweep's coverage of A's workflows) regardless of
            # whether the cap was actually exceeded. Lenient like
            # projects_api.py's create_project: leave inactive rather than
            # raise here -- try_reserve() immediately after (for the
            # /start path) is the authoritative gate and already returns a
            # proper 409; a caller that isn't /start (e.g. rerun_design)
            # shouldn't hard-fail just because the cap is full elsewhere.
            from src.core.simple_config import get_config

            active_count = db.query(AutopilotProject).filter_by(is_active=True).count()
            max_concurrent = get_config().max_concurrent_projects
            if active_count < max_concurrent:
                proj.is_active = True
                logger.info(f"Activated project '{proj.name}' for pipeline")
            else:
                logger.warning(
                    f"Not activating project '{proj.name}': max_concurrent_projects "
                    f"({max_concurrent}) already reached"
                )

        # Clear the deliberate-pause marker /autopilot/stop sets
        # (Workflow.paused_by="user") for this project's workflows --
        # otherwise every self-heal/retry path that correctly skips
        # user-paused workflows would also skip them here, leaving a
        # workflow permanently stuck even after the user explicitly hits
        # play again.
        #
        # Gated on proj.is_active actually being True (either already was,
        # or was just set above): background_phase_advancement_sweep
        # (server.py) scopes its work to is_active projects only. Flipping
        # a workflow back to "active" while is_active stayed False (cap
        # reached above) would leave it permanently invisible to that
        # sweep -- it looks like it's running but nothing ever advances
        # it. Leave it paused instead; the next successful activation
        # (this function running again with room under the cap) resumes it.
        if proj.is_active:
            resumed = db.query(Workflow).filter(Workflow.project_id == proj.id, Workflow.paused_by == "user").update({Workflow.status: "active", Workflow.paused_by: None})
            if resumed:
                logger.info(f"Resumed {resumed} user-paused workflow(s) for '{proj.name}'")

        db.commit()
        return proj.id


# ProjectContext keys for AutopilotService's "was a pipeline running, with
# what args" resume marker -- see src/autopilot/service.py's
# _persist_running_state/load_persisted_state/clear_persisted_state/
# enumerate_persisted_states. Namespaced per-project (multiple pipelines
# can be running concurrently); _RUNNING_STATE_KEY_LEGACY is the single
# pre-multi-project bare key, migrated in place by enumerate_persisted_states
# the first time the backend reads it after this change deploys.
_RUNNING_STATE_KEY_PREFIX = "autopilot_running_pipeline_"
_RUNNING_STATE_KEY_LEGACY = "autopilot_running_pipeline"


def _running_state_key(project_id: str) -> str:
    return f"{_RUNNING_STATE_KEY_PREFIX}{project_id}"


class PersistentPipelineState:
    """Manages pipeline state that survives restarts.

    Backed by ProjectContext (a generic key-value table that already existed
    but had no callers) instead of JSON files under AUTOPILOT_STATE_DIR.
    Files were a second, non-transactional source of truth: when the tasks/
    agents/workflows tables got wiped in one DB transaction, these files
    didn't move, and kept pointing at a dead workflow_id until manually
    deleted as a separate step. Storing this in the same database means a
    reset of workflow state naturally carries this along with it.
    """

    STATE_KEY_PREFIX = "autopilot_pipeline_state_"
    PROCESSED_KEY_PREFIX = "autopilot_processed_designs_"
    # Pre-multi-project bare keys, shared by every project under the old
    # single-global scheme. Migrated in place onto this project's namespaced
    # keys the first time a project_id-aware caller loads state and finds
    # nothing under its own key yet -- see _migrate_legacy_state_if_present.
    STATE_KEY_LEGACY = "autopilot_pipeline_state"
    PROCESSED_KEY_LEGACY = "autopilot_processed_designs"

    def __init__(self, project_id: Optional[str] = None):
        # Without project_id, two concurrent run_continuous_pipeline loops
        # (one per project, see AutopilotServiceRegistry) would share these
        # SAME bare keys and clobber each other's processed-design tracking
        # and current-design/workflow pointer. Falls back to the legacy
        # bare keys when project_id isn't given (the standalone CLI path,
        # which only ever runs one project at a time).
        self.project_id = project_id
        if project_id:
            self.STATE_KEY = f"{self.STATE_KEY_PREFIX}{project_id}"
            self.PROCESSED_KEY = f"{self.PROCESSED_KEY_PREFIX}{project_id}"
        else:
            self.STATE_KEY = self.STATE_KEY_LEGACY
            self.PROCESSED_KEY = self.PROCESSED_KEY_LEGACY

    def _migrate_legacy_state_if_present(self) -> None:
        """One-time migration of the pre-multi-project bare keys onto this
        project's namespaced keys. Best-effort: assumes the legacy state
        belongs to whichever project_id-aware caller asks first, which
        holds at the moment this migration ships (only one project's
        pipeline could have been running under the old single-global
        scheme). Idempotent -- a no-op once the namespaced key exists.
        """
        try:
            with get_db() as db:
                if _get_project_context(db, self.STATE_KEY) is not None:
                    return
                legacy_state = _get_project_context(db, self.STATE_KEY_LEGACY)
                legacy_processed = _get_project_context(db, self.PROCESSED_KEY_LEGACY)
                if legacy_state is None and legacy_processed is None:
                    return
                logger.info(f"[MIGRATE] Namespacing legacy pipeline state to project {self.project_id}")
                if legacy_state is not None:
                    _set_project_context(db, self.STATE_KEY, legacy_state)
                    _delete_project_context(db, self.STATE_KEY_LEGACY)
                if legacy_processed is not None:
                    _set_project_context(db, self.PROCESSED_KEY, legacy_processed)
                    _delete_project_context(db, self.PROCESSED_KEY_LEGACY)
        except Exception as e:
            logger.warning(f"Failed to migrate legacy pipeline state: {e}")

    def save(self, state: PipelineState, processed_hashes: Set[str]):
        """Save pipeline state and processed designs to the DB.

        Write order: processed_designs first, in its OWN committed
        transaction, then state in a second one — two separate get_db()
        blocks, not one. This matters: a single shared transaction would be
        atomic (both writes land or neither does), which sounds safer but
        actually reintroduces the failure mode this ordering exists to
        avoid. If the process dies before a single shared commit, NEITHER
        write lands, so the design isn't marked processed and gets
        reprocessed (double-processed) on restart. With two separate
        transactions, a crash between them leaves processed_designs
        durably committed (design safely skipped next time) while state
        merely undercounts by 1 -- worse bookkeeping, not worse work.
        """
        try:
            with get_db() as db:
                _set_project_context(db, self.PROCESSED_KEY, list(processed_hashes))
        except Exception as e:
            logger.warning(f"Failed to save processed designs: {e}")

        self.save_state_only(state)

    def save_state_only(self, state: PipelineState) -> None:
        """Persist just the state half of save(), without touching
        processed_hashes.

        Used for an early, mid-run checkpoint right after
        state.current_workflow_id becomes known (see run_single_workflow),
        rather than only at the end of run_single_design. Without this, the
        status endpoint's current_design/current_workflow_id -- read from
        this same DB-persisted state, see get_autopilot_status -- stayed
        stale for a design's *entire* run (which can take minutes to
        hours): AutopilotService's own live current_design field is never
        updated (this runs in a separate thread with no reference back to
        it), so the status endpoint's fallback to this persisted state was
        the only path, and it wasn't written early enough. Observed live: a
        real agent was actively working (active_agents: 1) right after a
        design was launched, but the dashboard's play button, current
        design, and workflow id all still showed the previous, already-
        completed run -- indistinguishable from clicking play and nothing
        happening. Calling save(...) here instead (the full version) would
        also be safe but wastefully re-writes the unchanged processed_hashes
        set on every mid-run checkpoint.
        """
        state_data = state.to_dict()
        state_data["saved_at"] = datetime.now().isoformat()
        try:
            with get_db() as db:
                _set_project_context(db, self.STATE_KEY, state_data)
        except Exception as e:
            logger.warning(f"Failed to save pipeline state: {e}")

    def load(self) -> Tuple[PipelineState, Set[str]]:
        """Load pipeline state and processed designs from the DB."""
        if self.project_id:
            self._migrate_legacy_state_if_present()

        state = PipelineState()
        processed_hashes: Set[str] = set()

        try:
            with get_db() as db:
                state_data = _get_project_context(db, self.STATE_KEY)
            if state_data:
                state = PipelineState.from_dict(state_data)
                logger.info(f"Loaded pipeline state: {state.designs_processed} designs processed")
        except Exception as e:
            logger.warning(f"Failed to load pipeline state: {e}")

        try:
            with get_db() as db:
                processed_list = _get_project_context(db, self.PROCESSED_KEY)
            if processed_list:
                processed_hashes = set(processed_list)
                logger.info(f"Loaded {len(processed_hashes)} processed designs")
        except Exception as e:
            logger.warning(f"Failed to load processed designs: {e}")

        return state, processed_hashes

    def clear(self):
        """Clear persisted state (for fresh start)."""
        with get_db() as db:
            _delete_project_context(db, self.STATE_KEY)
            _delete_project_context(db, self.PROCESSED_KEY)

    def has_incomplete_work(self) -> bool:
        """Check if there's incomplete work from a previous run."""
        try:
            with get_db() as db:
                state_data = _get_project_context(db, self.STATE_KEY)
            if not state_data:
                return False

            current_design = state_data.get("current_design")
            # `.get("queue_status", {})` only applies its default when the
            # key is ABSENT -- a stored value of `"queue_status": null`
            # (explicit None) returns None here, not {}, and would raise
            # AttributeError on the next .get() call below without the
            # `or {}` guard.
            queue_status = state_data.get("queue_status") or {}
            return current_design is not None or queue_status.get("status") == "processing"
        except Exception as e:
            logger.warning(f"Failed to read state for incomplete work check: {e}")
            return False

    def get_last_run_id(self) -> Optional[str]:
        """Get the run ID from the last persisted state."""
        try:
            with get_db() as db:
                state_data = _get_project_context(db, self.STATE_KEY)
            return state_data.get("run_id") if state_data else None
        except Exception as e:
            logger.warning(f"Failed to read state for last run ID: {e}")
            return None

    def remove_processed_hash(self, design_hash: str) -> None:
        """Remove a single hash from the processed-designs set, touching
        ONLY that key -- not the pipeline state.

        Callers that only need to un-mark one design (e.g. re-adding a
        deleted design so it gets reprocessed) must not go through
        load()+save(): save() rewrites STATE_KEY too, and a load()...save()
        round trip captures a snapshot of pipeline state that a
        concurrently-running pipeline (run_continuous_pipeline saves up to
        6 times per loop iteration) could have already moved past by the
        time this save() lands, silently reverting its progress.
        """
        try:
            with get_db() as db:
                processed_list = _get_project_context(db, self.PROCESSED_KEY) or []
                if design_hash in processed_list:
                    processed_list = [h for h in processed_list if h != design_hash]
                    _set_project_context(db, self.PROCESSED_KEY, processed_list)
        except Exception as e:
            logger.warning(f"Failed to remove processed hash: {e}")


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


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def api_get(endpoint: str, timeout: int = 5) -> Optional[dict]:
    """Legacy HTTP GET - prefer direct DB access functions below."""
    try:
        r = requests.get(f"{API_BASE}{endpoint}", timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug(f"[api_get] {endpoint} failed: {e}")
    return None


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


def terminate_agent_direct(agent_id: str) -> bool:
    """Terminate agent directly in database (H-2 fix)."""
    try:
        with get_db() as session:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            if agent:
                agent.status = "terminated"
                agent.current_task_id = None  # Clear stale reference
                agent.terminated_at = datetime.utcnow()
                return True
        return False
    except Exception as e:
        logger.debug(f"[terminate_agent_direct] Failed: {e}")
        return False


def pause_workflow_direct(workflow_id: str) -> bool:
    """Pause workflow directly in database (H-2 fix)."""
    try:
        with get_db() as session:
            wf = session.query(Workflow).filter_by(id=workflow_id).first()
            if wf:
                wf.status = "paused"
                return True
        return False
    except Exception as e:
        logger.debug(f"[pause_workflow_direct] Failed: {e}")
        return False


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
            Workflow.status.in_(["active", "running"]),
        )
        .all()
    )

    paused_count = 0
    workflow_ids = []
    for wf in active_workflows:
        wf.status = "paused"
        wf.paused_by = paused_by
        wf.paused_at = datetime.utcnow()
        if paused_by == "budget":
            wf.status_reason = "Budget limit reached"
        elif paused_by == "user":
            wf.status_reason = None
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
            agent.status = "terminated"
            agent.terminated_at = datetime.utcnow()
            agent.current_task_id = None
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


def create_agent_for_task_direct(
    task_id: str,
    workflow_id: str,
    phase_id: Optional[str] = None,
    agent_type: str = "phase",
    enriched_data_override: Optional[dict] = None,
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
    import asyncio

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

            agent = asyncio.run(
                server_state.agent_manager.create_agent_for_task(
                    task=task,
                    enriched_data=enriched_data,
                    memories=[],
                    project_context="",
                    agent_type=agent_type,
                    use_existing_worktree=True,
                )
            )
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
        logger.debug(f"[create_agent_for_task_direct] Failed: {e}")
        return None


def _update_orchestrator_status(status: str) -> None:
    """Update the orchestrator agent's status in the database.

    Args:
        status: New status ("working", "idle", or "terminated")
    """
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


def _workflow_belongs_to_project(
    wf_project_id: Optional[str],
    wf_working_directory: Optional[str],
    current_project_id: Optional[str],
    current_project_path: str,
) -> bool:
    """Whether a workflow belongs to the given project.

    Prefers the authoritative Workflow.project_id FK when both sides have
    one. Falls back to a resolved-path containment check via
    working_directory using Path.is_relative_to() -- NOT a raw
    str.startswith() prefix match, which wrongly matches sibling
    directories that share a name prefix (e.g. "/code/project-a" is a
    string-prefix of "/code/project-ab/.worktrees/wt_1", so a plain
    startswith() would treat project-ab's workflow as belonging to
    project-a). Returns False (does not belong) when neither signal can
    positively confirm membership -- the safe default for every caller of
    this helper: don't block on, force-fail, force-pause, or terminate
    agents for a workflow we can't positively confirm is ours.
    """
    if wf_project_id and current_project_id:
        return wf_project_id == current_project_id
    if not wf_working_directory:
        return False
    try:
        return Path(wf_working_directory).resolve().is_relative_to(Path(current_project_path).resolve())
    except (OSError, ValueError):
        return False


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
    logger: OrchestratorLogger,
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


def is_design_fully_complete(workflow_id: str, logger: OrchestratorLogger) -> Tuple[bool, str]:
    """Check if a design is fully complete:
    1. Workflow DB status is completed (or no active agents/tasks remain)
    2. No active agents
    3. All agent branches merged to main

    Returns:
        (is_complete, reason) tuple
    """
    # Check workflow status — if the server already marked it completed, trust that.
    wf = get_workflow_status(workflow_id)
    wf_status = wf.get("status", "")
    if wf_status == "completed":
        return True, "Workflow status: completed"
    if wf_status not in ("active", "running", "paused"):
        return False, f"Workflow status: {wf_status}"

    # Check task statuses
    pending = get_tasks(status="pending", workflow_id=workflow_id)
    queued = get_tasks(status="queued", workflow_id=workflow_id)
    in_progress = get_tasks(status="in_progress", workflow_id=workflow_id)
    assigned = get_tasks(status="assigned", workflow_id=workflow_id)
    failed = get_tasks(status="failed", workflow_id=workflow_id)
    done = get_tasks(status="done", workflow_id=workflow_id)

    # Pending/active tasks indicate real work remaining.
    # Ignore DIAGNOSTIC tasks (created by the monitor itself when stuck) — they
    # should not block completion detection.
    real_pending = [t for t in (pending + queued + in_progress + assigned) if not (t.get("raw_description") or "").startswith(DIAGNOSTIC_TASK_PREFIX)]
    if real_pending:
        task_ids = [t.get("id", "")[:8] for t in real_pending[:3]]
        return False, f"{len(real_pending)} task(s) still active: {', '.join(task_ids)}"

    # Failed tasks: only block if the same phase has NO subsequent done task
    # (i.e., a retry succeeded → the failure is resolved).
    done_phase_ids = {t.get("phase_id") for t in done if t.get("phase_id")}
    unresolved_failures = [t for t in failed if t.get("phase_id") not in done_phase_ids and not (t.get("raw_description") or "").startswith(DIAGNOSTIC_TASK_PREFIX)]
    if unresolved_failures:
        task_ids = [t.get("id", "")[:8] for t in unresolved_failures[:3]]
        return (
            False,
            f"{len(unresolved_failures)} unresolved failed task(s): {', '.join(task_ids)}",
        )

    # Check for active agents
    agents = get_agents(workflow_id=workflow_id)
    active_agents = [a for a in agents if a.get("status") in ("working", "starting", "idle")]
    if active_agents:
        agent_ids = [a.get("id", "")[:8] for a in active_agents[:3]]
        return (
            False,
            f"{len(active_agents)} agent(s) still active: {', '.join(agent_ids)}",
        )

    # Check for unmerged agent branches
    try:
        # Get project path from workflow's working directory
        wf_data = get_workflow_status(workflow_id)
        project_path = wf_data.get("working_directory") or os.getenv("PROJECT_PATH")
        if not project_path:
            # Fallback: try to get from DB
            try:
                from src.core.database import Workflow, get_db

                with get_db() as _db:
                    _wf = _db.query(Workflow).filter_by(id=workflow_id).first()
                    if _wf and _wf.working_directory and Path(_wf.working_directory).exists():
                        project_path = _wf.working_directory
            except Exception:
                pass
        if not project_path:
            return False, "Cannot determine project path for branch check"
        result = subprocess.run(
            ["git", "branch", "--list", "agent-*"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_path,
        )
        if result.returncode == 0:
            branches = [b.strip().lstrip("* ") for b in result.stdout.strip().split("\n") if b.strip()]
            if branches:
                return False, f"{len(branches)} unmerged agent branch(es)"
    except Exception:
        pass

    # Check done task count vs actual phase count (not hardcoded 10).
    try:
        from src.core.database import DatabaseManager as _DbM
        from src.core.database import Phase

        _db = _DbM()
        _s = _db.get_session()
        try:
            phase_count = _s.query(Phase).count()
        finally:
            _s.close()
    except Exception:
        phase_count = 11  # fallback for the autopilot pipeline
    if len(done) < phase_count:
        return False, f"Only {len(done)}/{phase_count} phases done"

    return True, "All phases done, branches merged"


def _retry_failed_tasks(workflow_id: str, logger: OrchestratorLogger) -> List[str]:
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
        is_orphan = "Orphaned" in (task.get("failure_reason") or "")
        if retry_count >= 2 and not is_orphan:
            logger.info(
                f"  Task {task_id[:8]} failed {retry_count} times - skipping retry"
            )
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


def attempt_recovery(workflow_id: str, logger: OrchestratorLogger) -> Tuple[bool, str]:
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


def prompt_human(reason: str, logger: OrchestratorLogger, timeout: int = 600) -> str:
    """Prompt for human input. Auto-continues after timeout seconds."""
    import sys
    import uuid

    logger.warning(f"DECISION POINT: {reason}")

    input_dir = Path(AUTOPILOT_STATE_DIR)
    input_dir.mkdir(parents=True, exist_ok=True)

    request_id = str(uuid.uuid4())[:8]
    request_file = input_dir / f"input_request_{request_id}.json"
    response_file = input_dir / f"input_response_{request_id}.json"

    # Atomic write via temp+rename
    payload = json.dumps(
        {
            "id": request_id,
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
            "options": ["c", "s", "q"],
            "labels": {"c": "Continue", "s": "Skip design", "q": "Quit pipeline"},
            "timeout_seconds": timeout,
        },
        indent=2,
    )
    tmp = request_file.with_suffix(".tmp")
    tmp.write_text(payload)
    os.rename(tmp, request_file)

    logger.event("human_input_required", {"reason": reason, "request_id": request_id})

    start = time.time()
    while time.time() - start < timeout:
        # Check if request file was dismissed (deleted by API)
        if not request_file.exists():
            logger.warning("Input request was dismissed (auto-continuing)")
            response_file.unlink(missing_ok=True)
            return "c"  # Auto-continue when dismissed

        # Check file response
        if response_file.exists():
            try:
                data = json.loads(response_file.read_text())
                choice = data.get("choice", "").strip().lower()
                message = data.get("message", "")

                if choice == "m" and message:
                    # Log the message and continue waiting for actual decision
                    logger.info(f"Human message: {message}")
                    logger.event(
                        "human_input",
                        {
                            "choice": "m",
                            "message": message,
                            "reason": reason,
                            "source": "web",
                            "request_id": request_id,
                        },
                    )
                    response_file.unlink(missing_ok=True)  # Delete response, keep waiting
                    continue

                if choice in ("c", "s", "q"):
                    logger.event(
                        "human_input",
                        {
                            "choice": choice,
                            "reason": reason,
                            "source": "web",
                            "request_id": request_id,
                        },
                    )
                    request_file.unlink(missing_ok=True)
                    response_file.unlink(missing_ok=True)
                    return choice
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Error reading response file: {e}")

        # Check terminal input (non-blocking on Unix only)
        try:
            if sys.platform != "win32" and sys.stdin.isatty():
                import select as select_mod

                rlist, _, _ = select_mod.select([sys.stdin], [], [], 1.0)
                if rlist:
                    choice = sys.stdin.readline().strip().lower()
                    if choice in ("c", "s", "q"):
                        logger.event(
                            "human_input",
                            {
                                "choice": choice,
                                "reason": reason,
                                "source": "terminal",
                                "request_id": request_id,
                            },
                        )
                        request_file.unlink(missing_ok=True)
                        response_file.unlink(missing_ok=True)
                        return choice
            else:
                time.sleep(2)
        except (OSError, ValueError):
            time.sleep(2)

    # Timeout - auto-continue
    logger.warning(f"Human input timed out after {timeout}s, auto-continuing")
    logger.event("human_input", {"choice": "timeout", "reason": reason, "request_id": request_id})
    request_file.unlink(missing_ok=True)
    return "c"


def scan_design_queue(queue_dir: Path, processed_hashes: Set[str]) -> List[DesignEntry]:
    designs = []
    if not queue_dir.exists():
        return designs

    for ext in ("*.md", "*.txt"):
        for filepath in sorted(queue_dir.glob(ext)):
            if filepath.is_dir():
                continue
            content_hash = file_hash(filepath)
            if content_hash in processed_hashes:
                # Self-heal: if the design is marked processed but its
                # features are all pending (e.g. server crashed between
                # marking processed and creating features), re-queue it.
                try:
                    from src.core.database import (
                        AutopilotDesign as _AD,
                    )
                    from src.core.database import (
                        Feature as _Feat,
                    )
                    from src.core.database import (
                        get_db as _gdb,
                    )

                    with _gdb() as _db:
                        _des = _db.query(_AD).filter_by(content_hash=content_hash).first()
                        if _des:
                            _feats = _db.query(_Feat).filter_by(design_id=_des.id).all()
                            if not _feats or all(f.status == "pending" for f in _feats):
                                logger.warning(f"[SELF-HEAL] Design {_des.name} is in processed_hashes but has no features or all pending — re-queuing")
                                processed_hashes.discard(content_hash)
                            else:
                                continue
                        else:
                            continue
                except Exception:
                    continue
            name = filepath.stem.replace("_", " ").replace("-", " ").title()
            designs.append(
                DesignEntry(
                    path=filepath,
                    name=name,
                    content_hash=content_hash,
                )
            )

    # Check for manual reorder file — stored in .hephaestus/ (not in docs/design/)
    order_file = queue_dir.parent.parent / CONTEXT_DIR_NAME / ".queue_order.json"
    if order_file.exists():
        try:
            saved_order = json.loads(order_file.read_text())
            # Create lookup by filename
            by_filename = {d.path.name: d for d in designs}
            # Order by saved order, then append any new files not in saved order
            ordered = []
            for fname in saved_order:
                if fname in by_filename:
                    ordered.append(by_filename.pop(fname))
            # Add remaining files (not in saved order) sorted by name
            ordered.extend(sorted(by_filename.values(), key=lambda d: d.path.name.lower()))
            return ordered
        except (json.JSONDecodeError, KeyError):
            pass  # Fall back to default sort

    designs.sort(key=lambda d: d.path.name.lower())
    return designs


def _has_resumable_active_design(project_id: Optional[str]) -> bool:
    """True if this project has an ACTIVE design with incomplete (not
    completed/skipped) features already on file.

    Used by run_continuous_pipeline's "workflow still active" gate to decide
    whether an unrelated design's still-running workflow should actually
    block picking up new work. It should only block a FRESH design's Phase 0
    -- run_single_workflow's default pause_existing=True terminates every
    other active workflow's agents project-wide (see its docstring), which
    is destructive if another design's agents are still genuinely working.
    Resuming an already-active design never reaches that path: run_phase0
    skips straight past Phase 0 when Feature rows already exist (Tier 1,
    see its docstring), and the feature dispatch that follows always passes
    pause_existing=False. So a design in this state is always safe to
    resume regardless of what else is active in the project.
    """
    try:
        from src.core.database import AutopilotDesign, Feature, get_db

        if not project_id:
            return False
        with get_db() as db:
            active_designs = db.query(AutopilotDesign).filter_by(project_id=project_id, status="active").all()
            for candidate in active_designs:
                incomplete = (
                    db.query(Feature)
                    .filter(Feature.design_id == candidate.id, Feature.status.notin_(["completed", "skipped"]))
                    .count()
                )
                if incomplete > 0:
                    return True
            return False
    except Exception:
        return False


def pick_next_design(
    queue_dir: Path,
    processed_hashes: Set[str],
    logger: OrchestratorLogger,
    project_id: Optional[str] = None,
) -> Optional[DesignEntry]:
    """Pick the next design to process.

    Reads from DB (autopilot_designs) if available, falls back to file scan.
    Uses file_path column if available, falls back to filename-based path.

    project_id: which project's queue to pick from. Without this, two
    concurrent run_continuous_pipeline loops (one per project, see
    AutopilotServiceRegistry) would both resolve "the project" via the
    single global AutopilotProject.is_active flag -- whichever project
    most recently started/restarted -- and silently steal each other's
    designs the moment a second project starts. Falls back to is_active
    only when project_id isn't supplied (the standalone CLI path, which
    has no per-project registry and only ever runs one project at a time).
    """
    # Try DB-based queue first
    try:
        from src.core.database import (
            AutopilotDesign,
            AutopilotProject,
            Feature,
            Workflow,
            get_db,
        )

        with get_db() as db:
            # Find the target project: by id when given (concurrent,
            # per-project loop), else the single active project (standalone
            # CLI path).
            if project_id:
                project = db.query(AutopilotProject).filter_by(id=project_id).first()
            else:
                project = db.query(AutopilotProject).filter_by(is_active=True).first()
            if not project:
                logger.info("pick_next_design: no active project found")
                return None

            # Budget guard: skip project entirely if over budget
            from src.core.cost_derivation import check_budget_before_new_work

            if not check_budget_before_new_work(db, project.id):
                logger.info(
                    f"pick_next_design: project '{project.name}' ({project.id[:8]}) "
                    f"over budget (${project.cost_total_usd:.2f} >= ${project.cost_limit_usd:.2f}) — skipping"
                )
                return None

            logger.info(
                f"pick_next_design: searching project '{project.name}' ({project.id[:8]})"
            )

            # Resume support: prioritize a design that already finished
            # Phase 0 (status moved to "active") but still has incomplete
            # features over starting a brand new "pending" design. A design
            # in this state was checked FIRST here, before finishing this
            # loop looked at "pending" designs at all -- but the "pending"
            # query below always ran unconditionally FIRST, so any pending
            # design (however low-priority) always won, silently starting
            # a whole new design's Phase 0 while an active design sat with
            # unblocked, ready-to-run features it would never be given a
            # turn to finish. Observed live: a feature whose only blocking
            # dependency had just completed stayed unstarted indefinitely
            # because a second, unrelated design was next in queue-order.
            #
            # Snapshot the pending list BEFORE the active-design loop below
            # runs -- that loop can itself reset a design back to "pending"
            # (candidate.status = "pending", to retry after a failed
            # workflow), and a design reset like that must wait for a
            # FRESH pick_next_design call to be eligible, not be picked
            # right back up by the pending-fallback query at the bottom of
            # this same call as if it had been queued all along.
            pending_designs = db.query(AutopilotDesign).filter_by(project_id=project.id, status="pending").order_by(AutopilotDesign.ordinal, AutopilotDesign.filename).all()

            design = None
            active_designs = db.query(AutopilotDesign).filter_by(project_id=project.id, status="active").order_by(AutopilotDesign.ordinal, AutopilotDesign.filename).all()
            if active_designs:
                logger.info(f"pick_next_design: found {len(active_designs)} active design(s), checking for incomplete work before considering pending designs")
                for candidate in active_designs:
                    incomplete = (
                        db.query(Feature)
                        .filter(
                            Feature.design_id == candidate.id,
                            Feature.status.notin_(["completed", "skipped"]),
                        )
                        .count()
                    )

                    # Check if any associated workflow has failed — only consider
                    # workflows linked to incomplete features. A failed workflow
                    # that's orphaned (no feature links to it) or only linked to
                    # completed features should not block the design.
                    failed_wf = (
                        db.query(Workflow)
                        .join(Feature, Feature.workflow_id == Workflow.id)
                        .filter(
                            Workflow.design_id == candidate.id,
                            Workflow.status == "failed",
                            Feature.status.notin_(["completed", "skipped"]),
                        )
                        .first()
                    )
                    # Fallback: orphaned failed workflow (no feature links to it at all)
                    if not failed_wf:
                        orphaned_failed = (
                            db.query(Workflow)
                            .outerjoin(Feature, Feature.workflow_id == Workflow.id)
                            .filter(
                                Workflow.design_id == candidate.id,
                                Workflow.status == "failed",
                                Feature.id.is_(None),
                            )
                            .first()
                        )
                        if orphaned_failed:
                            logger.info(f"  Ignoring orphaned failed workflow {orphaned_failed.id[:8]} (no features link to it)")
                            # Clear the design_id so it stops showing up
                            orphaned_failed.design_id = None
                            db.commit()

                    logger.info(f"  Active design '{candidate.name}' ({candidate.id[:8]}): incomplete={incomplete}, failed_wf={failed_wf.id[:8] if failed_wf else 'None'}, status={candidate.status}")

                    if incomplete > 0:
                        if failed_wf:
                            # Failed workflow with incomplete features — retry
                            retry_key = f"autopilot_retry_{candidate.id}"
                            retry_count = _get_project_context(db, retry_key) or 0
                            if retry_count >= MAX_DESIGN_RETRIES:
                                logger.info(
                                    f"Design {candidate.name} has failed workflow {failed_wf.id[:8]} and exceeded {MAX_DESIGN_RETRIES} retries ({retry_count}/{MAX_DESIGN_RETRIES}) — marking failed"
                                )
                                candidate.status = "failed"
                                # Surfaced by /autopilot/status's last_error so
                                # the UI can show a real popup instead of this
                                # silently sitting in a log file no one reads
                                # -- the pipeline itself keeps polling "queue
                                # empty" every 60s afterward, looking healthy,
                                # while having permanently given up on the
                                # only design in the queue.
                                candidate.error = f"Gave up after {MAX_DESIGN_RETRIES} retries: workflow {failed_wf.id[:8]} kept failing"
                                db.commit()
                                continue
                            logger.info(f"Design {candidate.name} has failed workflow {failed_wf.id[:8]} — resetting to pending for retry ({retry_count + 1}/{MAX_DESIGN_RETRIES})")
                            _set_project_context(db, retry_key, retry_count + 1)
                            candidate.status = "pending"
                            candidate.error = None  # clear any stale exhausted-retry message
                            db.commit()
                            continue
                        design = candidate
                        logger.info(f"Resuming active design {design.name} ({incomplete} feature(s) not yet complete)")
                        break
                    else:
                        # All features completed/skipped — but check
                        # if any workflow failed (e.g. diagnostic task).
                        # If so, retry instead of marking done.
                        if failed_wf:
                            retry_key = f"autopilot_retry_{candidate.id}"
                            retry_count = _get_project_context(db, retry_key) or 0
                            if retry_count >= MAX_DESIGN_RETRIES:
                                logger.info(
                                    f"Design {candidate.name} has all features done "
                                    f"but failed workflow {failed_wf.id[:8]} and "
                                    f"exceeded {MAX_DESIGN_RETRIES} retries "
                                    f"({retry_count}/{MAX_DESIGN_RETRIES}) — marking done"
                                )
                                candidate.status = "completed"
                                db.commit()
                                continue
                            logger.info(f"Design {candidate.name} has all features done but failed workflow {failed_wf.id[:8]} — retrying ({retry_count + 1}/{MAX_DESIGN_RETRIES})")
                            _set_project_context(db, retry_key, retry_count + 1)
                            candidate.status = "pending"
                            db.commit()
                            continue
                        # All features done, no failed workflows — mark done.
                        candidate.status = "completed"
                        db.commit()
                        logger.info(f"Design {candidate.name} has all features completed/skipped — marking done")

            if design is None:
                # No active design has resumable work -- safe to start the
                # next design that was already pending before this call
                # (the snapshot taken above, not a fresh query -- see its
                # comment for why a design the loop above just reset to
                # "pending" must not be picked up here).
                design = pending_designs[0] if pending_designs else None
                if design:
                    logger.info(f"pick_next_design: found pending design '{design.name}' ({design.id[:8]})")
                else:
                    logger.info("pick_next_design: no designs to process")

            if design:
                # Mark as processing
                design.status = "processing"
                db.commit()

                # Construct DesignEntry from DB record
                # Try file_path first, fall back to filename-based path
                design_path = None
                if design.file_path:
                    # Use file_path if available (absolute path)
                    design_path = Path(design.file_path)
                    if not design_path.exists():
                        logger.warning(f"file_path does not exist: {design_path}")
                        design_path = None

                if design_path is None:
                    # Fall back to filename-based path
                    design_path = Path(project.base_dir) / DESIGN_CONTEXT_SUBDIR / design.filename

                if design_path.exists():
                    entry = DesignEntry(
                        path=design_path,
                        name=design.name,
                        content_hash=design.content_hash or file_hash(design_path),
                        db_id=design.id,
                        file_path=str(design_path),
                    )
                    logger.info(f"Selected from DB: {design.name} (ordinal={design.ordinal})")
                    return entry
                else:
                    logger.warning(f"Design file not found: {design_path}")
    except Exception as e:
        logger.warning(f"DB queue read failed, falling back to file scan: {e}")

    # Fallback: file-based queue
    designs = scan_design_queue(queue_dir, processed_hashes)

    if not designs:
        return None

    logger.info(f"Found {len(designs)} pending design(s) in queue")
    for d in designs:
        logger.info(f"  - {d.name} ({d.path.name})")

    next_design = designs[0]
    logger.info(f"Selected: {next_design.name}")

    # Try to look up DB ID for file-scanned design, creating the row if it
    # doesn't exist yet (e.g. the project itself was just auto-created —
    # see AutopilotService.start — so no AutopilotDesign row was ever
    # created for this file under the new project_id). Leaving db_id as
    # None here isn't a safe no-op: _create_feature_records requires a
    # non-null design_id (NOT NULL constraint), so a design processed via
    # this fallback with no DB row would crash later with an
    # IntegrityError right after Phase 0 completes — observed live during
    # smoke testing.
    try:
        import uuid as _uuid

        from src.core.database import AutopilotDesign, AutopilotProject
        from src.core.database import get_db as _get_db

        with _get_db() as _db:
            if project_id:
                project = _db.query(AutopilotProject).filter_by(id=project_id).first()
            else:
                project = _db.query(AutopilotProject).filter_by(is_active=True).first()
            if project:
                db_design = _db.query(AutopilotDesign).filter_by(project_id=project.id, filename=next_design.path.name).first()
                if not db_design:
                    db_design = AutopilotDesign(
                        id=f"des-{_uuid.uuid4().hex[:12]}",
                        project_id=project.id,
                        filename=next_design.path.name,
                        name=next_design.name,
                        content_hash=next_design.content_hash,
                        file_path=str(next_design.path),
                        status="pending",
                    )
                    _db.add(db_design)
                    _db.flush()
                    logger.info(f"Auto-created AutopilotDesign row for {next_design.path.name} (none existed for project {project.id})")
                next_design.db_id = db_design.id
    except Exception as e:
        logger.warning(f"Could not link/create DB design row: {e}")

    return next_design


def _assess_run_health(
    project_path: Path,
    _exec_id: str,
    orchestrator_log_path: Path,
    logger: "OrchestratorLogger",
) -> dict:
    """Assess run health by checking orchestrator log and tmux logs for problems."""
    health: dict = {
        "clean": True,
        "goto_count": 0,
        "error_count": 0,
        "goto_events": [],
        "tmux_errors": [],
        "warnings": [],
    }

    # Count GOTOs from orchestrator log — informational only, not an error signal
    if orchestrator_log_path and orchestrator_log_path.exists():
        try:
            lines = orchestrator_log_path.read_text(errors="replace").splitlines()
            goto_lines = [line for line in lines if "[GOTO]" in line]
            decision_lines = [line for line in lines if "DECISION POINT" in line]
            health["goto_count"] = len(goto_lines)
            health["goto_events"] = goto_lines[-10:]
            health["decision_points"] = [line.strip() for line in decision_lines]
            # GOTOs are normal iteration — do NOT set clean=False for them
        except Exception as e:
            health["warnings"].append(f"Could not read orchestrator log: {e}")

    # Grep tmux logs for error patterns
    error_patterns = [
        "ERROR",
        "Traceback",
        "FAILED",
        "ModuleNotFoundError",
        "ImportError",
        "AssertionError",
        "pytest.*FAILED",
        "exit code 1",
    ]
    tmux_dir = project_path / CONTEXT_DIR_NAME / "tmux"
    total_errors = 0
    if tmux_dir.is_dir():
        for log_file in sorted(tmux_dir.glob("*.log")):
            try:
                text = log_file.read_text(errors="replace")
                hits = [ln.strip() for ln in text.splitlines() if any(p in ln for p in error_patterns)]
                if hits:
                    total_errors += len(hits)
                    health["tmux_errors"].append(
                        {
                            "file": log_file.name,
                            "count": len(hits),
                            "samples": hits[:3],
                        }
                    )
            except Exception:
                pass
    health["error_count"] = total_errors
    if total_errors > 0:
        health["clean"] = False

    if health["clean"]:
        logger.info("Run health: CLEAN (no GOTOs, no tmux errors)")
    else:
        logger.info(f"Run health: PROBLEMS DETECTED — gotos={health['goto_count']} tmux_errors={health['error_count']}")

    return health


def create_feature_folder(project_path: Path, design_name: str, logger: OrchestratorLogger) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = design_name.lower().replace(" ", "_")[:40]
    # Features go in .hephaestus/features/ to keep project root clean
    feature_folder = project_path / CONTEXT_DIR_NAME / "features" / f"{timestamp}_{safe_name}"
    feature_folder.mkdir(parents=True, exist_ok=True)
    (feature_folder / "docs").mkdir(exist_ok=True)

    # Note: .hephaestus/ is excluded from git via .git/info/exclude
    # (managed by WorktreeManager). We do NOT modify the user's .gitignore.

    logger.info(f"Feature folder: {feature_folder}")
    return feature_folder


def copy_design_document(design_entry: DesignEntry, feature_folder: Path) -> Path:
    # Store in .hephaestus/ context, not docs/, so the design doc doesn't
    # appear as a pipeline artifact in the UI's docs listing.
    dest = feature_folder / CONTEXT_DIR_NAME / design_entry.path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(design_entry.path, dest)
    return dest


# ── Feature Model Helper Functions ─────────────────────────────────


def _create_integration_worktree(
    project_path: Path,
    design_id: str,
    branch: str,
    logger: OrchestratorLogger,
) -> Optional[Path]:
    """Create an integration worktree for a feature pipeline.

    Args:
        project_path: Path to the project root
        design_id: Design ID for branch naming
        branch: Branch name to create
        logger: Orchestrator logger

    Returns:
        Path to the worktree, or None on failure
    """
    try:
        from src.core.database import DatabaseManager as DbManager
        from src.core.simple_config import get_config
        from src.core.worktree_manager import WorktreeManager

        cfg = get_config()
        db = DbManager(str(cfg.database_path))
        try:
            wt_mgr = WorktreeManager(db_manager=db)
            wt_mgr.reload(project_path)

            # Create branch from main if it doesn't exist
            try:
                wt_mgr.main_repo.git.branch(branch)
            except _git.exc.GitCommandError:
                pass  # Branch exists

            # Create worktree
            safe_branch = branch.replace("/", "-")
            wt_path = wt_mgr.worktree_base / f"wt_{safe_branch}"
            # A directory can exist here without being a valid git worktree --
            # e.g. a prior run got killed mid-`git worktree add`, or a stale
            # cleanup left a stub behind (observed live: only a leftover
            # .hephaestus/.placeholder, no .git). Reusing it silently as-is
            # then breaks everything downstream: agent creation later
            # discovers it has no .git, falls back to an isolated per-agent
            # worktree, and nulls the workflow's working_directory -- so
            # output validation can never find what the agent wrote. Treat
            # "exists but not a real worktree" the same as "doesn't exist".
            if wt_path.exists() and not (wt_path / ".git").exists():
                logger.warning(f"Found stale non-worktree directory at {wt_path} (no .git) -- removing before recreating")
                import shutil as _shutil

                _shutil.rmtree(wt_path, ignore_errors=True)
            if not wt_path.exists():
                wt_mgr.main_repo.git.worktree("add", str(wt_path), branch)

            logger.info(f"Created integration worktree: {wt_path} (branch: {branch})")
            return wt_path
        finally:
            session = getattr(db, "_session", None) or getattr(db, "session", None)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Failed to create integration worktree: {e}")
        return None


def _cleanup_worktree(
    worktree: Path,
    branch: str,
    project_path: Path,
    logger: OrchestratorLogger,
) -> None:
    """Clean up a worktree after feature pipeline completes.

    Args:
        worktree: Path to the worktree
        branch: Branch name
        project_path: Path to the project root
        logger: Orchestrator logger
    """
    try:
        from src.core.database import DatabaseManager as DbManager
        from src.core.simple_config import get_config
        from src.core.worktree_manager import WorktreeManager

        cfg = get_config()
        db = DbManager(str(cfg.database_path))
        try:
            wt_mgr = WorktreeManager(db_manager=db)
            wt_mgr.reload(project_path)

            # Archive tmux transcripts before the worktree (and everything in
            # it) is deleted -- .hephaestus/ is git-excluded, so it doesn't
            # survive the merge like docs/*.md reports do. Copy to the same
            # project-root .hephaestus/tmux/ location _assess_run_health
            # already reads from, so these transcripts remain available for
            # forensics/audit after the fact, same as the merged report
            # artifacts.
            try:
                src_tmux = worktree / CONTEXT_DIR_NAME / "tmux"
                if src_tmux.is_dir():
                    dest_tmux = project_path / CONTEXT_DIR_NAME / "tmux"
                    dest_tmux.mkdir(parents=True, exist_ok=True)
                    for log_file in src_tmux.glob("*"):
                        shutil.copy2(log_file, dest_tmux / log_file.name)
                    logger.info(f"Archived tmux transcripts to {dest_tmux}")
            except Exception as e:
                logger.warning(f"Failed to archive tmux transcripts: {e}")

            # Remove worktree
            if worktree.exists():
                try:
                    wt_mgr.main_repo.git.worktree("remove", str(worktree), "--force")
                    logger.info(f"Removed worktree: {worktree}")
                except Exception as e:
                    logger.warning(f"Failed to remove worktree: {e}")

                # Clear stale working_directory from any workflows pointing to
                # this worktree -- but never touch a workflow that's still
                # "active" or "paused" (resumable -- see the same exclusion in
                # worktree_manager.py's cleanup_all_stale_branches). This
                # worktree path is deterministic (derived only from design_id,
                # reused across every retry), so an old, already-finished
                # attempt's cleanup can otherwise null out a *different*,
                # currently-active-or-paused workflow that has since
                # legitimately reused the same path (e.g. after an abrupt
                # orchestrator kill left an earlier attempt's cleanup
                # deferred). Once working_directory is wrongly nulled, agent
                # creation can't find the shared worktree (falls back to an
                # isolated per-agent one) and output validation can't check
                # any candidate path at all -- silently breaking a workflow
                # that's still genuinely in progress or waiting to be resumed.
                try:
                    from src.core.database import Workflow

                    _s = db.get_session()
                    try:
                        wfs = (
                            _s.query(Workflow)
                            .filter(
                                Workflow.working_directory == str(worktree),
                                Workflow.status.notin_(["active", "paused"]),
                            )
                            .all()
                        )
                        for wf in wfs:
                            wf.working_directory = None
                            logger.info(f"Cleared stale working_directory from workflow {wf.id[:8]}")
                        if wfs:
                            _s.commit()
                    finally:
                        _s.close()
                except Exception as e:
                    logger.warning(f"Failed to clear workflow working_directory: {e}")
        finally:
            session = getattr(db, "_session", None) or getattr(db, "session", None)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Failed to cleanup worktree: {e}")


def sweep_completed_workflow_worktrees(logger: OrchestratorLogger) -> int:
    """Remove worktrees left behind by workflows that reached 'completed'
    status but never got their normal _cleanup_worktree() call to run --
    e.g. the backend restarted between run_single_workflow returning
    "completed" in _run_one_feature and that same call stack reaching its
    _cleanup_worktree() a few lines later. Nothing else ever revisits a
    workflow once it's "completed", so a worktree orphaned this way sits
    forever until something (previously: only a manual /cleanup-branches
    call, or a rerun of that exact design) happens to sweep it.

    Deliberately narrower than WorktreeManager.cleanup_all_stale_branches():
    only touches a worktree whose OWN workflow record unambiguously says
    "done", one at a time via the same removal _cleanup_worktree already
    uses for the normal completion path -- not a heuristic dirty/branch
    sweep that can pull old, unrelated branches back into main (observed
    live: doing that once already reintroduced files under .hephaestus/,
    which must stay git-excluded, into main's history).

    Returns the number of worktrees removed.
    """
    from src.core.database import DatabaseManager as DbManager
    from src.core.database import Workflow
    from src.core.simple_config import get_config

    cfg = get_config()
    db = DbManager(str(cfg.database_path))
    removed = 0
    try:
        with db.session_scope() as session:
            targets = [
                (wf.id, wf.working_directory, wf.launch_params)
                for wf in session.query(Workflow).filter(
                    Workflow.status == "completed",
                    Workflow.working_directory.isnot(None),
                )
                if wf.working_directory and ".worktrees/" in wf.working_directory
            ]

        for wf_id, working_directory, launch_params in targets:
            worktree = Path(working_directory)
            if not (worktree / ".git").exists():
                continue  # already gone -- nothing to remove

            lp = launch_params if isinstance(launch_params, dict) else {}
            project_path_str = lp.get("project_path")
            if not project_path_str:
                logger.warning(
                    f"[SWEEP] Workflow {wf_id[:8]} has an orphaned worktree "
                    f"{worktree} but no launch_params.project_path to scope "
                    "cleanup to -- skipping rather than guessing"
                )
                continue

            try:
                branch = _git.Repo(worktree).active_branch.name
            except Exception:
                branch = ""

            logger.info(
                f"[SWEEP] Cleaning up orphaned worktree for completed "
                f"workflow {wf_id[:8]}: {worktree}"
            )
            _cleanup_worktree(worktree, branch, Path(project_path_str), logger)
            removed += 1
    except Exception as e:
        logger.warning(f"[SWEEP] Failed to sweep completed-workflow worktrees: {e}")
    finally:
        session = getattr(db, "_session", None) or getattr(db, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
    return removed


def _create_designs_folder(
    project_path: Path,
    design_entry: DesignEntry,
    logger: OrchestratorLogger,
) -> Path:
    """Create permanent storage folder for design artifacts.

    Args:
        project_path: Path to the project root
        design_entry: Design entry being processed
        logger: Orchestrator logger

    Returns:
        Path to the designs folder
    """
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = design_entry.name.lower().replace(" ", "_")[:40]
    designs_folder = project_path / CONTEXT_DIR_NAME / "designs" / f"{timestamp}_{safe_name}_{design_entry.db_id or 'unknown'}"
    designs_folder.mkdir(parents=True, exist_ok=True)
    (designs_folder / "features").mkdir(exist_ok=True)

    logger.info(f"Created designs folder: {designs_folder}")
    return designs_folder


def _create_feature_records(
    design_id: Optional[str],
    features_json: dict,
    designs_folder: Path,
    logger: OrchestratorLogger,
) -> List[dict]:
    """Create Feature DB records from features.json.

    Args:
        design_id: Design ID
        features_json: Parsed features.json content
        designs_folder: Path to designs folder
        logger: Orchestrator logger

    Returns:
        List of feature records created
    """
    import uuid

    from src.core.database import Feature, get_db

    feature_records = []

    with get_db() as db:
        for feat in features_json.get("features", []):
            feature_id = f"feat-{uuid.uuid4().hex[:8]}"
            feature_key = feat.get("id", "")

            # Create feature record path
            feature_record_path = designs_folder / "features" / feature_key
            feature_record_path.mkdir(parents=True, exist_ok=True)

            # Only store scope_doc_path after the file has been copied by run_phase0.
            # At this point the copy has already happened (run_phase0 copies scope files
            # before calling _create_feature_records), so check for existence.
            scope_doc_path = feature_record_path / "scope.md"
            scope_doc_path_str = str(scope_doc_path) if scope_doc_path.exists() else None

            feature = Feature(
                id=feature_id,
                design_id=design_id,
                feature_key=feature_key,
                name=feat.get("name", feature_key),
                scope=feat.get("scope", ""),
                files=feat.get("files", []),
                depends_on=feat.get("depends_on", []),
                execution=feat.get("execution", "parallel"),
                status="pending",
                scope_doc_path=scope_doc_path_str,
                feature_record_path=str(feature_record_path),
            )
            db.add(feature)

            feature_records.append(
                {
                    "id": feature_id,
                    "feature_key": feature_key,
                    "name": feat.get("name", feature_key),
                    "scope": feat.get("scope", ""),
                    "files": feat.get("files", []),
                    "depends_on": feat.get("depends_on", []),
                    "execution": feat.get("execution", "parallel"),
                    "scope_doc_path": scope_doc_path_str,
                    "feature_record_path": str(feature_record_path),
                }
            )

            logger.info(f"Created feature record: {feature_key} ({feature_id})")

        db.commit()

    return feature_records


def _update_feature_status(
    feature_id: str,
    design_id: Optional[str],
    status: str,
    error: Optional[str] = None,
    logger: OrchestratorLogger = None,
) -> None:
    """Update a feature's status in the database.

    This is the single write path for Feature.status from the orchestrator.

    Args:
        feature_id: Feature ID
        design_id: Design ID
        status: New status (pending, active, completed, failed, skipped)
        error: Error message if status is failed
        logger: Optional logger
    """
    from datetime import datetime

    from src.core.database import Feature, get_db

    with get_db() as db:
        feature = db.query(Feature).filter_by(id=feature_id, design_id=design_id).first()
        if feature:
            feature.status = status
            if status == "active":
                feature.started_at = datetime.utcnow()
            elif status in ("completed", "failed", "skipped"):
                feature.completed_at = datetime.utcnow()
            if error:
                feature.error = error
            db.commit()
            if logger:
                logger.info(f"Updated feature {feature.feature_key} status to {status}")


def _sync_stale_feature_statuses(logger: OrchestratorLogger) -> int:
    """Self-heal: flip Feature.status to "completed" for any feature whose
    linked Workflow has already reached "completed", if the Feature row
    itself hasn't caught up.

    _update_feature_status is the normal write path for this, but it only
    ever runs as a side effect of _run_one_feature actually being called
    again for that specific feature -- which only happens on a fresh,
    full re-walk of the whole design (run_single_design ->
    run_feature_pipelines). A backend restart's resume path continues
    whatever workflow was actually in-flight directly (run_single_workflow's
    own poll loop), not a full design re-walk -- so a feature whose
    workflow finished (e.g. via a goto/retry cycle that happened to
    complete on its own after the restart) days ago can have its
    Feature.status stuck "active" indefinitely, with nothing left to ever
    call _run_one_feature for it again. Observed live: a feature's
    workflow status showed "completed" (git_commit_push had run) while its
    Feature row still showed "active" in the UI, unresolved across
    multiple backend restarts.

    Runs from the same generic, restart-safe background sweep that already
    drives _advance_phases for every workflow (see
    background_phase_advancement_sweep in server.py) -- Feature-table-wide,
    not scoped to a single workflow, since the whole point is to catch
    features no workflow-scoped loop is going to revisit.

    Returns the number of features repaired.
    """
    from src.core.database import Feature, Workflow, get_db

    repaired = 0

    # Re-link any feature whose workflow link was never written (see
    # _relink_features_to_workflows). That function normally runs as a side
    # effect of _run_one_feature/run_single_design re-walking the design --
    # but a design whose pipeline has already fully finished has nothing
    # left to trigger a re-walk, so a feature whose workflow completed
    # without the link ever being written stays workflow_id=None forever.
    # The stale-status join below can't see it either (it requires a
    # linked Workflow row), so without this pass the feature is invisible
    # to every self-heal path and its status sticks indefinitely. Observed
    # live: a feature's workflow reached "completed" but Feature.workflow_id
    # was never set, leaving Feature.status stuck "active" across restarts.
    with get_db() as db:
        orphaned_design_ids = {
            design_id
            for (design_id,) in db.query(Feature.design_id)
            .filter(Feature.workflow_id.is_(None))
            .distinct()
            .all()
        }
    for design_id in orphaned_design_ids:
        _relink_features_to_workflows(design_id, logger)

    with get_db() as db:
        # Feature has a linked workflow that completed. Deliberately NOT
        # also inferring completion for workflow_id-less features from
        # sibling status ("has a completed sibling, no active sibling" was
        # tried here and reverted -- a feature that simply hasn't been
        # dispatched yet is indistinguishable from an orphaned-but-actually-
        # done one by that signal alone: both have workflow_id=None and a
        # non-terminal status. Observed live: the instant a design's last
        # in-flight feature flipped to "completed", every one of its
        # genuinely not-yet-started siblings got marked "completed" too on
        # the very next sweep tick, since none of them were "active" at
        # that moment either -- silently dropping their real work from the
        # pipeline). The actual "workflow completed but link never got
        # written" scenario is handled at the source instead: see
        # _relink_features_to_workflows, called from _run_one_feature
        # after every feature run and from run_single_design before every
        # design reprocessing.
        stale = (
            db.query(Feature)
            .join(Workflow, Feature.workflow_id == Workflow.id)
            .filter(
                Feature.status.notin_(["completed", "failed", "skipped"]),
                Workflow.status == "completed",
            )
            .all()
        )
        for feature in stale:
            logger.info(f"[FEATURE-SYNC] {feature.feature_key}: workflow already completed but Feature.status was {feature.status!r} -- syncing to completed")
            feature.status = "completed"
            feature.completed_at = feature.completed_at or datetime.utcnow()
            repaired += 1

        if repaired:
            db.commit()
    return repaired


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


def _recover_abandoned_workflows_missing_worktree(logger: OrchestratorLogger) -> int:
    """Self-heal for a workflow that _escalate_stale_active_workflows marked
    "failed" as a false positive (its own message already hedges: "likely
    lost mid-flight across a backend restart") AND whose shared worktree is
    now gone (Workflow.working_directory is None -- e.g. from the exact
    worktree-deletion incident _remove_worktree's require_clean guard now
    prevents going forward, but which can still be true for a workflow
    already damaged before that fix landed).

    A workflow in this state has no automated path back to progress:
    _advance_phases's every case requires status in ("active", "paused"),
    so a "failed" workflow is invisible to all of them, forever, until a
    human clicks Resume in the UI. And simply flipping status back to
    "active" without also fixing working_directory would silently make
    things worse, not better: create_agent_for_task's shared-worktree
    resolution only hard-fails when working_directory is a *present-but-
    missing* path (by design, per its own comment -- no safe fallback for
    that case, since a disconnected fork would be unmergeable); when
    working_directory is None outright, that check is skipped entirely and
    agent creation silently falls through to forking a brand-new, isolated
    worktree with none of the prior phases' real commits -- the next agent
    would review/build against the wrong code entirely.

    Recovers correctly instead: rebuild the shared worktree from the
    feature's own branch (feature/<design_id[:8]>/<feature_key>, same name
    _run_one_feature always uses) via _create_integration_worktree -- the
    branch itself was never touched by any of this, so it still carries
    every phase's real commits. Reconnecting Workflow.working_directory to
    a fresh checkout of that branch, then resuming, lets the normal retry
    machinery (_maybe_retry_failed_tasks) safely take it from there.

    Capped via the stuck task's own retry_count (reusing the same
    MAX_RETRY_COUNT convention _maybe_retry_failed_tasks already enforces)
    so a workflow whose branch/worktree recreation keeps failing for a
    real reason eventually stops retrying and stays failed for a human,
    instead of looping forever.
    """
    from src.core.database import AutopilotDesign, AutopilotProject, Feature, Task

    max_recovery_attempts = 2
    recovered = 0
    with get_db() as db:
        candidates = (
            db.query(Workflow)
            .filter(
                Workflow.status == "failed",
                Workflow.working_directory.is_(None),
                Workflow.status_reason.like("Abandoned: no agent/task activity%"),
            )
            .all()
        )
        for wf in candidates:
            feature = db.query(Feature).filter_by(workflow_id=wf.id).first()
            if not feature or not feature.design_id:
                continue
            design = db.query(AutopilotDesign).filter_by(id=feature.design_id).first()
            if not design or not design.project_id:
                continue
            project = db.query(AutopilotProject).filter_by(id=design.project_id).first()
            if not project or not project.base_dir:
                continue

            # Scoped to the CURRENTLY in_progress phase only -- a workflow
            # that's been through several goto cycles can carry old,
            # already-superseded "failed" tasks from phases that long since
            # completed on a later attempt (e.g. an early "development"
            # attempt that failed and hit its own retry cap, before a much
            # later retry succeeded and the pipeline moved on for real).
            # Those are harmless history, not evidence recovery is unsafe --
            # checking retry_count across every failed task ever recorded
            # for this workflow, instead of just the phase actually stuck
            # right now, refused to recover a workflow whose real blocker
            # (security_review, retry_count=0) had never been retried at
            # all, purely because an unrelated, ancient development-phase
            # task happened to already be at the cap.
            in_progress_phase_ids = {
                pid for (pid,) in db.query(PhaseExecution.phase_id).join(Phase, PhaseExecution.phase_id == Phase.id).filter(Phase.workflow_id == wf.id, PhaseExecution.status == "in_progress").all()
            }
            stuck_tasks = (
                db.query(Task)
                .filter(
                    Task.workflow_id == wf.id,
                    Task.status == "failed",
                    Task.phase_id.in_(in_progress_phase_ids),
                )
                .all()
                if in_progress_phase_ids
                else []
            )
            if not stuck_tasks:
                continue
            if any((t.retry_count or 0) >= max_recovery_attempts for t in stuck_tasks):
                continue

            branch = f"feature/{feature.design_id[:8]}/{feature.feature_key}"
            wt_path = _create_integration_worktree(Path(project.base_dir), feature.design_id, branch, logger)
            if not wt_path:
                logger.warning(f"[WORKFLOW-RECOVERY] Could not rebuild worktree for workflow {wf.id[:8]} (branch {branch}) -- leaving failed")
                continue

            logger.warning(
                f"[WORKFLOW-RECOVERY] Rebuilt worktree for workflow {wf.id[:8]} "
                f"from branch {branch} at {wt_path} -- resuming; the stuck "
                'task(s) are left exactly as they are (still "failed", own '
                "retry_count untouched) so _maybe_retry_failed_tasks' own "
                "already-tested retry-and-dispatch path picks them up on "
                "the very next active-workflow sweep pass, instead of this "
                "function reimplementing that dispatch itself."
            )
            wf.working_directory = str(wt_path)
            wf.status = "active"
            wf.status_reason = None
            recovered += 1
        if recovered:
            db.commit()
    return recovered


def _recover_abandoned_workflows_with_completed_phase(logger: OrchestratorLogger) -> int:
    """Self-heal for a workflow _escalate_stale_active_workflows marked
    "failed" (same abandonment message as
    _recover_abandoned_workflows_missing_worktree), but whose worktree is
    still intact and whose current in-progress phase's task(s) already
    finished ("done", none pending/assigned/in_progress) -- i.e. the phase's
    real work completed, but nothing then evaluated it or created the next
    phase's task. _escalate_stale_active_workflows's own docstring names the
    likely cause: a backend restart landing in the narrow window between a
    task's "done" commit and the synchronous spec-gate evaluation
    (fire_spec_gate_if_ready) that normally follows it in the same request.

    Distinct from _recover_abandoned_workflows_missing_worktree, which
    handles a FAILED task with a lost worktree (retry machinery re-dispatches
    it). This case has no failed task to retry -- the work already
    succeeded -- so recovery is just: make the workflow visible to
    _advance_phases again (status back to "active", clear status_reason) and
    let its own existing "phase complete -> fire transition" path
    (_case_in_progress_complete) re-evaluate the already-done work on the
    very next sweep, instead of this function re-implementing that
    evaluation itself. If the phase's declared output is genuinely missing
    (e.g. the agent's JSON never made it into the worktree), that path's
    normal result_missing handling sends it to development with the
    available report text as context, same as any other run -- this
    function only unblocks the workflow, it doesn't grade the work.
    """
    from src.core.database import Task

    recovered = 0
    with get_db() as db:
        candidates = (
            db.query(Workflow)
            .filter(
                Workflow.status == "failed",
                Workflow.working_directory.isnot(None),
                Workflow.status_reason.like("Abandoned: no agent/task activity%"),
            )
            .all()
        )
        for wf in candidates:
            in_progress_phase_ids = {
                pid
                for (pid,) in db.query(PhaseExecution.phase_id)
                .join(Phase, PhaseExecution.phase_id == Phase.id)
                .filter(Phase.workflow_id == wf.id, PhaseExecution.status == "in_progress")
                .all()
            }
            if not in_progress_phase_ids:
                continue  # nothing in_progress -- not this function's case

            unfinished = (
                db.query(Task)
                .filter(
                    Task.phase_id.in_(in_progress_phase_ids),
                    Task.status.in_(["pending", "assigned", "in_progress"]),
                )
                .count()
            )
            if unfinished > 0:
                continue  # something genuinely still active -- leave it alone

            has_done = (
                db.query(Task)
                .filter(Task.phase_id.in_(in_progress_phase_ids), Task.status == "done")
                .count()
            )
            if not has_done:
                continue  # nothing completed yet either -- not evaluable

            logger.warning(
                f"[WORKFLOW-RECOVERY] Workflow {wf.id[:8]} was marked failed "
                "(abandoned) but its worktree is intact and its current "
                "phase's task(s) already finished -- resuming so the next "
                "sweep can evaluate and advance it"
            )
            wf.status = "active"
            wf.status_reason = None
            recovered += 1
        if recovered:
            db.commit()
    return recovered


def _retry_exhausted_paused_workflows(logger: OrchestratorLogger) -> int:
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

    max_cycles = _get_paused_workflow_max_retry_cycles()
    cutoff = datetime.utcnow() - timedelta(seconds=_get_paused_workflow_retry_cooldown_seconds())
    recovered = 0
    with get_db() as db:
        candidates = (
            db.query(Workflow)
            .filter(
                Workflow.status == "paused",
                Workflow.paused_by == "system",
                or_(Workflow.paused_at.is_(None), Workflow.paused_at < cutoff),
            )
            .all()
        )
        for wf in candidates:
            if wf.paused_retry_count >= max_cycles:
                logger.warning(f"[WORKFLOW-RECOVERY] Workflow {wf.id[:8]} exhausted {max_cycles} auto-retry cycles -- giving up permanently, needs a manual resume")
                wf.paused_by = "system-exhausted"
                wf.status_reason = f"{wf.status_reason or ''} (auto-retry gave up after {max_cycles} attempts -- manual resume required)"
                recovered += 1  # counts as "handled", not "retried"
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


def _update_design_status(
    design_id: Optional[str],
    status: str,
    logger: OrchestratorLogger = None,
    **kwargs,
) -> None:
    """Update a design's status in the database.

    Args:
        design_id: Design ID
        status: New status
        logger: Optional logger
        **kwargs: Additional fields to update
    """
    from src.core.database import AutopilotDesign, get_db

    with get_db() as db:
        design = db.query(AutopilotDesign).filter_by(id=design_id).first()
        if design:
            design.status = status
            for key, value in kwargs.items():
                if hasattr(design, key):
                    setattr(design, key, value)
                elif logger:
                    logger.warning(f"_update_design_status: unknown field {key!r} for AutopilotDesign")
            db.commit()
            if logger:
                logger.info(f"Updated design {design_id} status to {status}")


def _set_workflow_type(workflow_id: str, workflow_type: str) -> None:
    """Set the workflow type (design or feature).

    Args:
        workflow_id: Workflow ID
        workflow_type: Type of workflow
    """
    from src.core.database import Workflow, get_db

    with get_db() as db:
        workflow = db.query(Workflow).filter_by(id=workflow_id).first()
        if workflow:
            workflow.workflow_type = workflow_type
            db.commit()


def _get_phase0_completion(design_id: Optional[str]) -> Optional[dict]:
    """Check whether Phase 0's workflow already completed for this design.

    Uses the same status-based idempotency concept PhaseManager.mark_phase_complete
    uses for every other phase (PhaseExecution.status == "completed"), anchored one
    level up at Workflow-existence since Phase 0 is necessarily its own Workflow row
    (it decomposes a design into N features, each of which then gets its own
    separate Workflow — Phase 0 can't be "phase order=0" of any of those, since it
    runs before they exist) rather than a Phase row inside a shared one.

    Checks the LAST phase (by order) rather than the first, since Phase 0 now
    runs two phases (Feature Architect, feature_review) — checking just the
    first Phase's PhaseExecution would report "completed" as soon as Feature
    Architect finished, before feature_review ever ran. Also requires the
    Workflow's own status to be "completed": a couple of generic,
    Phase-0-unaware code paths (WorkflowTerminationHandler.terminate_workflow,
    the admin POST /api/workflow-executions/{id}/complete endpoint) can mark
    a PhaseExecution "completed" as part of a forced/generic teardown without
    the workflow itself having reached a real "completed" state — requiring
    both catches a forced-complete that never actually ran feature_review.

    Returns designs_folder (NOT the workflow's own working_directory/worktree,
    which _cleanup_worktree removes once the workflow finishes) — run_phase0
    already persists AutopilotDesign.designs_folder before calling
    _create_feature_records specifically so this recovery path has somewhere
    durable to read features.json back from if that call never completed.

    Returns:
        {"workflow_id": ..., "designs_folder": ...} if completed, else None.
    """
    if not design_id:
        return None
    from src.core.database import (
        AutopilotDesign,
        Phase,
        PhaseExecution,
        Workflow,
        get_db,
    )

    with get_db() as db:
        design = db.query(AutopilotDesign).filter_by(id=design_id).first()
        if not design or not design.phase0_workflow_id or not design.designs_folder:
            return None
        wf = db.query(Workflow).filter_by(id=design.phase0_workflow_id).first()
        if not wf or wf.status != "completed":
            return None
        last_phase = db.query(Phase).filter_by(workflow_id=wf.id).order_by(Phase.order.desc()).first()
        execution = db.query(PhaseExecution).filter_by(phase_id=last_phase.id).first() if last_phase else None
        if execution and execution.status == "completed":
            return {"workflow_id": wf.id, "designs_folder": design.designs_folder}
        return None


def _relink_features_to_workflows(design_id: str, logger: OrchestratorLogger) -> None:
    """Re-link features to their workflows if workflow_id is missing.

    Handles pipeline restarts where features exist but their workflow link
    was lost -- and, since run_single_workflow clears
    state.current_workflow_id back to None right before returning
    "completed" (see its final success branch), this is also _run_one_
    feature's ONLY working way to link a just-finished feature's workflow
    (see the call site there). Matches features to workflows by feature_key
    in launch_params.
    """
    import json as _json

    from src.core.database import Feature, Workflow, get_db

    with get_db() as db:
        unlinked = db.query(Feature).filter_by(design_id=design_id, workflow_id=None).all()
        if not unlinked:
            return

        # Scoped to this design_id -- without it, two different designs
        # that happen to share a feature_key (e.g. both have an "auth"
        # feature) could link a feature to the WRONG design's workflow.
        # Matters more now that this runs after every single feature
        # completes, not just once per design reprocessing.
        workflows = db.query(Workflow).filter(
            Workflow.definition_id == "autopilot", Workflow.design_id == design_id
        ).order_by(Workflow.created_at.desc()).all()

        for feat in unlinked:
            for wf in workflows:
                try:
                    params = wf.launch_params if isinstance(wf.launch_params, dict) else _json.loads(wf.launch_params or "{}")
                except Exception:
                    continue
                if params.get("feature_id") == feat.feature_key:
                    feat.workflow_id = wf.id
                    logger.info(f"Re-linked workflow {wf.id[:8]} to feature {feat.id} ({feat.name})")
                    break

        db.commit()


def _clean_stale_assigned_tasks(workflow_id: str, logger: OrchestratorLogger) -> None:
    """Clean tasks that are 'assigned' or 'in_progress' to terminated agents,
    and pending/assigned tasks that belong to already-completed workflows.

    Called periodically from the polling loop to prevent tasks from hanging
    forever when agents crash or are killed.
    """
    from src.core.database import Agent, Task, Workflow, get_db

    with get_db() as db:
        # 1. Tasks assigned to terminated agents
        stale_tasks = (
            db.query(Task)
            .filter(
                Task.workflow_id == workflow_id,
                Task.status.in_(["assigned", "in_progress"]),
                Task.assigned_agent_id.isnot(None),
            )
            .all()
        )
        for task in stale_tasks:
            agent = db.query(Agent).filter_by(id=task.assigned_agent_id).first()
            if agent and agent.status == "terminated":
                logger.info(f"[STALE-TASK] Task {task.id[:8]} assigned to terminated agent {task.assigned_agent_id[:8]} — marking failed")
                task.status = "failed"
                # Don't clobber a real reason: update_task_status already
                # records why a "done" claim was rejected (e.g. a missing
                # output artifact) on this same field before the agent's
                # session ends. Only fall back to the generic message when
                # nothing more specific is already there -- otherwise the
                # retry below loses exactly the feedback it needs to fix.
                if not task.failure_reason:
                    task.failure_reason = f"Agent {task.assigned_agent_id[:8]} terminated unexpectedly"
                db.commit()

        # 2. Pending/assigned tasks in already-completed workflows
        workflow = db.query(Workflow).filter_by(id=workflow_id).first()
        if workflow and workflow.status == "completed":
            orphaned = (
                db.query(Task)
                .filter(
                    Task.workflow_id == workflow_id,
                    Task.status.in_(["pending", "assigned"]),
                )
                .all()
            )
            for task in orphaned:
                logger.info(f"[ORPHAN-TASK] Task {task.id[:8]} ({task.phase_id}) in completed workflow — marking failed")
                task.status = "failed"
                task.failure_reason = "Orphaned: workflow already completed"
                task.assigned_agent_id = None
            if orphaned:
                db.commit()


def _validate_features_json(features_json: dict) -> None:
    """Validate features.json structure.

    Args:
        features_json: Parsed features.json content

    Raises:
        ValueError: If validation fails
    """
    if not isinstance(features_json, dict):
        raise ValueError("features.json must be a JSON object")

    if "design_name" not in features_json:
        raise ValueError("features.json missing 'design_name' field")

    if "features" not in features_json:
        raise ValueError("features.json missing 'features' array")

    features = features_json["features"]
    if not isinstance(features, list):
        raise ValueError("'features' must be an array")

    # The prompt targets "around 5" as a rough guide, but the actual count
    # should follow the design's own structure -- a complex design
    # legitimately needing 6-10 features shouldn't have its entire Phase 0
    # output discarded over a headcount. Observed live: a well-formed
    # 6-feature decomposition for a genuinely multi-concern backend design
    # got rejected outright by a strict 1-5 cap, throwing away real analysis
    # work and forcing a full re-run. Keep only a generous sanity ceiling to
    # catch actual garbage (e.g. one "feature" per file).
    if len(features) < 1:
        raise ValueError("features array must have at least 1 entry, got 0")
    if len(features) > 50:
        raise ValueError(f"features array has {len(features)} entries -- that's not a feature decomposition, it looks like one feature per file")

    # Check for required fields and unique IDs
    ids = set()
    all_files = []

    for i, feat in enumerate(features):
        if "id" not in feat:
            raise ValueError(f"Feature {i} missing 'id' field")
        if "name" not in feat:
            raise ValueError(f"Feature {i} missing 'name' field")
        if "scope" not in feat:
            raise ValueError(f"Feature {i} missing 'scope' field")

        feat_id = feat["id"]
        if feat_id in ids:
            raise ValueError(f"Duplicate feature id: {feat_id}")
        ids.add(feat_id)

        # Check execution field
        execution = feat.get("execution", "parallel")
        if execution not in ("parallel", "sequential"):
            raise ValueError(f"Feature {feat_id} has invalid execution: {execution}")

        # Check depends_on references
        depends_on = feat.get("depends_on", [])
        if not isinstance(depends_on, list):
            raise ValueError(f"Feature {feat_id} depends_on must be an array")

        # Check files for overlaps
        files = feat.get("files", [])
        if not isinstance(files, list):
            raise ValueError(f"Feature {feat_id} files must be an array")

        for f in files:
            # Normalize: strip trailing slashes for comparison
            f_norm = f.rstrip("/")
            for existing in all_files:
                existing_norm = existing.rstrip("/")
                # Check for overlap: exact match or directory containment
                # (e.g. src/ contains src/utils/file.py, but .env does NOT contain .env.example)
                if f_norm == existing_norm or f_norm.startswith(existing_norm + "/") or existing_norm.startswith(f_norm + "/"):
                    raise ValueError(f"File overlap between features: {f} and {existing}")
            all_files.append(f)

    # Validate depends_on references
    for feat in features:
        depends_on = feat.get("depends_on", [])
        for dep in depends_on:
            if dep not in ids:
                raise ValueError(f"Feature {feat['id']} depends on unknown feature: {dep}")

    # Check for cycles
    def has_cycle(graph: dict) -> bool:
        """Check for cycles in dependency graph using DFS."""
        visited = set()
        rec_stack = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)

            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True

            rec_stack.remove(node)
            return False

        for node in graph:
            if node not in visited:
                if dfs(node):
                    return True
        return False

    # Build dependency graph
    dep_graph = {feat["id"]: feat.get("depends_on", []) for feat in features}
    if has_cycle(dep_graph):
        raise ValueError("Dependency cycle detected in features")


def _resolve_execution_order(
    features: List[dict],
    logger: OrchestratorLogger,
) -> List[List[dict]]:
    """Resolve execution order using Kahn's algorithm with parallel/sequential handling.

    Args:
        features: List of feature dicts from features.json
        logger: Orchestrator logger

    Returns:
        List of execution groups, each group is a list of features
    """
    from collections import defaultdict, deque

    # Build dependency graph
    in_degree = {f["id"]: 0 for f in features}
    adjacency = defaultdict(list)

    for feat in features:
        feat_id = feat["id"]
        depends_on = feat.get("depends_on", [])
        in_degree[feat_id] = len(depends_on)
        for dep in depends_on:
            adjacency[dep].append(feat_id)

    # Kahn's algorithm
    queue = deque([f["id"] for f in features if in_degree[f["id"]] == 0])
    execution_groups = []
    processed = set()

    # Build lookup for quick access
    feat_map = {f["id"]: f for f in features}
    # Architect's original features.json order, for ordering siblings within
    # a dependency layer below -- Kahn's algorithm only guarantees a feature
    # never runs before its dependencies (layer boundaries), it says nothing
    # about the relative order of independent features at the same depth.
    original_index = {f["id"]: i for i, f in enumerate(features)}

    while queue:
        # Collect current layer
        current_layer = []
        while queue:
            feat_id = queue.popleft()
            if feat_id not in processed:
                current_layer.append(feat_id)
                processed.add(feat_id)

        if not current_layer:
            break

        # Group this layer's features in the architect's original list
        # order -- consecutive "parallel" features batch into one group,
        # a "sequential" feature gets its own group in place, rather than
        # unconditionally running every parallel feature in the layer
        # before any sequential one regardless of where it sat in the
        # design's own ordering (observed live: a sequential feature that
        # several other features depend on got pushed to run after two
        # unrelated parallel features listed after it, purely because of
        # the parallel/sequential split, not any real dependency).
        current_layer.sort(key=lambda fid: original_index[fid])
        parallel_batch = []
        for feat_id in current_layer:
            feat = feat_map[feat_id]
            if feat.get("execution", "parallel") == "parallel":
                parallel_batch.append(feat)
                continue
            if parallel_batch:
                execution_groups.append(parallel_batch)
                parallel_batch = []
            execution_groups.append([feat])
        if parallel_batch:
            execution_groups.append(parallel_batch)

        # Reduce in-degrees of dependents
        for feat_id in current_layer:
            for neighbor in adjacency[feat_id]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0 and neighbor not in processed:
                    queue.append(neighbor)

    # Check for cycles (unprocessed features)
    unprocessed = [f["id"] for f in features if f["id"] not in processed]
    if unprocessed:
        logger.warning(f"Cycle detected in dependencies among {unprocessed}; appending cyclic features sequentially after already-resolved groups")
        # Preserve groups already resolved by Kahn's algorithm; append the cyclic
        # remainder one-by-one so we don't silently drop any processed features.
        for fid in feat_map:
            if fid not in processed:
                execution_groups.append([feat_map[fid]])

    # Log execution plan
    logger.info("Execution plan:")
    for i, group in enumerate(execution_groups):
        feat_names = [f["name"] for f in group]
        if len(group) > 1:
            logger.info(f"  Group {i + 1}: PARALLEL - {', '.join(feat_names)}")
        else:
            logger.info(f"  Group {i + 1}: SEQUENTIAL - {feat_names[0]}")

    return execution_groups


# ── Stray-file sweep ────────────────────────────────────────────────
# Agents may accidentally write report files to the project root instead
# of the feature docs dir.  Only move files whose names match known
# ephemeral report patterns — never touch source files, design docs,
# scripts, or anything else that belongs to the repo.
#
# NOTE: the call site is currently disabled (SWEEP_ENABLED = False).
# Enable once the allowlist has been validated in production.

SWEEP_ENABLED = False

# Only files matching these exact names (case-insensitive) are eligible
# to be swept.  Everything else in the project root is left alone.
_SWEEP_REPORT_NAMES = {
    "review_findings.md",
    "review_report.md",
    "security_report.md",
    "test_failures.md",
    "doc_review_report.md",
    "adversarial_review.md",
    "adversarial_review_report.md",
    "forensics_report.md",
    "architecture.md",
    "run_health.json",
    "pipeline_metrics.json",
    "qa_report.md",
    "product_validation.md",
    "scope_review_result.md",
    "arbitration_result.json",
}
_STRAY_DIRS: set = set()  # no directories swept until re-validated


def _sweep_stray_files(
    project_path: Path,
    feature_folder: Path,
    docs_dir: Path,
    logger: OrchestratorLogger,
) -> None:
    """Move known ephemeral report files from project root into feature .hephaestus/.

    Only files whose lowercased name appears in _SWEEP_REPORT_NAMES are
    eligible — source files, design docs, scripts, and anything else in
    the project tree are never touched.
    """
    if not SWEEP_ENABLED:
        return

    docs_dir.mkdir(parents=True, exist_ok=True)

    # ── known report files written to ./.hephaestus/ by agents ────────
    proj_hephaestus = project_path / _REPORT_SUBDIR
    if proj_hephaestus.is_dir() and proj_hephaestus.resolve() != docs_dir.resolve():
        for f in proj_hephaestus.iterdir():
            if f.is_file() and f.name.lower() in _SWEEP_REPORT_NAMES:
                dest = docs_dir / f.name
                if not dest.exists():
                    shutil.copy2(str(f), str(dest))
                    logger.info(f"Copied report: .hephaestus/{f.name} -> features/.../docs/")

    # ── known report files accidentally written to project root ─────
    for f in project_path.iterdir():
        if not f.is_file():
            continue
        if f.name.lower() not in _SWEEP_REPORT_NAMES:
            continue
        dest = docs_dir / f.name
        if not dest.exists():
            shutil.move(str(f), str(dest))
            logger.info(f"Moved root file: {f.name} -> features/.../docs/")

    # ── known report files in feature_folder root (above docs/) ─────
    for f in feature_folder.iterdir():
        if not f.is_file():
            continue
        if f.name.lower() not in _SWEEP_REPORT_NAMES:
            continue
        dest = docs_dir / f.name
        if not dest.exists():
            shutil.move(str(f), str(dest))
            logger.info(f"Moved feature file: {f.name} -> features/.../docs/")


_REPORT_SUBDIR = ".hephaestus"


def _report_path(project_path: Path, filename: str) -> Path:
    """Locate a report an agent wrote.

    Under worktree isolation agents write reports to ./.hephaestus/ (relative
    to their worktree), which is git-excluded. Fall back to the project root.
    Does NOT iterate worktrees (too slow for per-turn calls).
    """
    in_hephaestus = project_path / _REPORT_SUBDIR / filename
    if in_hephaestus.exists():
        return in_hephaestus
    return project_path / filename


def collect_report_summaries(project_path: Path) -> Dict[str, str]:
    summaries = {}
    report_files = {
        "requirements": "requirements_analysis.md",
        "architecture": "architecture.md",
        "review": "review_report.md",
        "doc_review": "doc_review_report.md",
        "security": "security_report.md",
        "qa": "qa_report.md",
        "product_validation": "product_validation.md",
        "forensics": "forensics_report.md",
    }

    for key, filename in report_files.items():
        # First check .hephaestus/ (where agents write), then project root
        filepath = project_path / ".hephaestus" / filename
        if not filepath.exists():
            filepath = project_path / filename
        if filepath.exists():
            try:
                content = filepath.read_text()
                lines = content.strip().split("\n")
                summary_lines = []
                for line in lines[:80]:
                    if line.strip():
                        summary_lines.append(line.strip())
                summaries[key] = "\n".join(summary_lines)
            except Exception:
                summaries[key] = f"[Could not read {filename}]"
        else:
            summaries[key] = f"[{filename} not found]"

    return summaries


def collect_files_created(project_path: Path, feature_folder: Path = None) -> List[str]:
    files = []
    dirs_to_scan = [project_path]
    if feature_folder:
        dirs_to_scan.append(feature_folder)

    for scan_dir in dirs_to_scan:
        for pattern in [
            "**/*.py",
            "**/*.ts",
            "**/*.tsx",
            "**/*.js",
            "**/*.jsx",
            "**/*.html",
            "**/*.css",
            "**/*.md",
        ]:
            for f in sorted(scan_dir.glob(pattern)):
                if ".venv" in str(f) or "node_modules" in str(f) or "__pycache__" in str(f):
                    continue
                rel = f.relative_to(scan_dir)
                files.append(str(rel))
    return sorted(set(files))


def generate_html_feature_report(
    report: FeatureReport,
    summaries: Dict[str, str],
    feature_folder: Path,
    logger: OrchestratorLogger,
) -> Path:
    """Generate an HTML feature report using a Jinja2 template."""
    from jinja2 import Environment, FileSystemLoader

    # Set up Jinja2 with the templates directory
    templates_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=True)
    template = env.get_template("feature_report.html")

    # Prepare template context
    hours = report.total_time_seconds // 3600
    minutes = (report.total_time_seconds % 3600) // 60
    time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

    context = {
        "design_name": report.design_name,
        "design_document": report.design_document,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "product_validated": report.product_validated,
        "qa_passed": report.qa_passed,
        "status_text": "VALIDATED" if report.product_validated else "NEEDS REVIEW",
        "qa_text": "PASSED" if report.qa_passed else "FAILED",
        "iterations": report.iterations,
        "time_str": time_str,
        "files_count": len(report.files_created),
        "cost_total": report.cost_total,
        "cost_breakdown": report.cost_breakdown,
        "summaries": summaries,
        "issues_resolved": report.issues_resolved,
        "outstanding_issues": report.outstanding_issues,
        "files_created": report.files_created,
    }

    html = template.render(**context)
    html_path = feature_folder / "feature_report.html"
    html_path.write_text(html)
    logger.info(f"HTML feature report: {html_path}")
    import subprocess
    import sys

    if sys.platform == "darwin":
        subprocess.Popen(["open", str(html_path)])
    return html_path


def generate_product_validation_report(
    project_path: Path,
    design_entry: DesignEntry,
    qa_passed: bool,
    logger: OrchestratorLogger,
) -> Tuple[bool, str]:
    """Read existing product validation results.

    NOTE: The primary validation now happens through the spec gate (spec.py)
    and the engine's evaluation points. This function is a fallback that reads
    existing reports for display/summary purposes only.
    """
    validation_path = _report_path(project_path, "product_validation.md")

    if validation_path.exists():
        from src.autopilot.okf_markdown import read_okf

        parsed = read_okf(validation_path)
        if parsed:
            frontmatter, _ = parsed
            verdict = str(frontmatter.get("verdict", "")).upper()
            meets_spec = verdict == "PASS" and qa_passed
            logger.info(f"Using structured product_validation.md frontmatter: verdict={verdict}")
            return meets_spec, validation_path.read_text()

        # Fallback: no parseable frontmatter -- fall back to a raw text scan
        # of the whole file (pre-OKF reports, or a malformed write).
        try:
            existing = validation_path.read_text()
            meets_spec = qa_passed and ("PASS" in existing or "pass" in existing.lower())
            logger.info("Using existing product validation from Phase 8 (no frontmatter)")
            return meets_spec, existing
        except Exception:
            pass

    # No validation report exists - this shouldn't happen if the workflow completed
    logger.warning("No product validation report found")
    return False, "No product validation report generated"


def _update_orchestrator_max_gotos(max_gotos: int, logger: OrchestratorLogger) -> None:
    """Update the autopilot workflow definition's max_total_gotos in the DB.

    This makes --max-iterations control the engine's iteration budget.
    """
    try:
        from src.core.database import WorkflowDefinition, get_db

        with get_db() as db:
            defn = db.query(WorkflowDefinition).filter_by(id="autopilot").first()
            if defn and defn.orchestrator_config:
                config = dict(defn.orchestrator_config)
                old_val = config.get("max_total_gotos", 10)
                if old_val != max_gotos:
                    config["max_total_gotos"] = max_gotos
                    defn.orchestrator_config = config
                    db.commit()
                    logger.info(f"Updated max_total_gotos: {old_val} -> {max_gotos}")
    except Exception as e:
        logger.warning(f"Failed to update max_total_gotos: {e}")


def _advance_phases(workflow_id: str, logger: OrchestratorLogger) -> bool:
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
            if not wf or wf.status not in ("active", "paused"):
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


def _try_auto_resume_paused_workflow(db, workflow_id: str, wf, logger: OrchestratorLogger) -> None:
    """Auto-resume paused workflow if it has a done task in the stalled phase.

    Skips workflows deliberately paused by anyone/anything (wf.paused_by is
    not None — "user", "budget", "system", etc.). Without this check, a
    deliberate pause could get silently reverted within one sweep tick
    (~20s) whenever the paused workflow's in-progress phase happens to have
    a done task sitting in it -- a state pausing itself commonly produces
    (the running task finishes right after being told to stop). Observed
    live: a user's pause click appeared to do nothing for a long time,
    because this function kept flipping the workflow back to "active"
    every cycle until whatever made the phase look stalled resolved on its
    own.
    """
    if wf.paused_by is not None:
        return  # Respect any deliberate pause ("user", "budget", etc.)
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
                db.commit()
                break


def _release_stale_task_creation_claims(db, workflow_id: str, logger: OrchestratorLogger) -> None:
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


def _release_pending_phases_with_done_tasks(db, workflow_id: str, logger: OrchestratorLogger) -> None:
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
    """
    execution = db.query(PhaseExecution).filter_by(phase_id=phase_id).populate_existing().first()
    if not execution:
        return
    if execution.status in ("pending", "completed"):
        execution.status = "in_progress"
        execution.started_at = datetime.utcnow()  # matches _create_phase_task's own convention
    execution.task_creation_claimed_at = None
    db.commit()


def _case_start_first_phase(db, workflow_id: str, pending: list, in_progress: list, completed: list, logger: OrchestratorLogger) -> Optional[bool]:
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
            )
    return None


def _case_in_progress_no_tasks(db, workflow_id: str, in_progress: list, logger: OrchestratorLogger) -> Optional[bool]:
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
            )
    return None


def _case_completed_with_successor(db, workflow_id: str, completed: list, pending: list, in_progress: list, logger: OrchestratorLogger) -> Optional[bool]:
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
            # Check if successor already has tasks (transition already fired)
            existing_tasks = db.query(Task).filter_by(phase_id=successor["phase"].id).count()
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
            )
    return None


def _case_in_progress_complete(db, workflow_id: str, in_progress: list, logger: OrchestratorLogger) -> Optional[bool]:
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
                return _create_phase_task(workflow_id, phase.id, phase.name, "continue", logger)

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


def _maybe_retry_failed_tasks(db, phase, logger: OrchestratorLogger, cycle_start: Optional[datetime] = None) -> Optional[bool]:
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
        max_retry_count = 2
        failed_tasks = (
            db.query(Task)
            .filter(Task.phase_id == phase.id, Task.status == "failed", *cycle_filter)
            .all()
        )
        # Orphaned tasks (never dispatched) are scheduling issues, not agent
        # failures -- they should always be retryable.
        retryable_tasks = [
            t for t in failed_tasks
            if (t.retry_count or 0) < max_retry_count
            or "Orphaned" in (t.failure_reason or "")
        ]
        if not retryable_tasks:
            reasons = sorted({t.failure_reason for t in failed_tasks if t.failure_reason})
            reason_text = "; ".join(reasons) if reasons else "no reason recorded"
            logger.warning(
                f"[PHASE-ADVANCE] Phase {phase.name} has {len(failed_tasks)} failed "
                f"task(s), all past the retry cap ({max_retry_count}) -- pausing "
                f"the workflow instead of retrying forever: {reason_text}"
            )
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
                base = task.enriched_description or task.raw_description or ""
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
            agent_data = create_agent_for_task_direct(task_id, phase.workflow_id, phase.id)
            with get_db() as retry_db:
                retry_task = retry_db.query(Task).filter_by(id=task_id).first()
                if not retry_task:
                    continue
                if not agent_data:
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


def _fire_phase_transition(workflow_id: str, phase_id: str, phase_name: str, logger: OrchestratorLogger) -> bool:
    """Fire the phase transition: mark complete, evaluate, create next task/agent.

    Returns True if something was done.
    """
    try:
        # Build phase output for gated phases
        phase_output = {}
        if phase_name in GATED_PHASES:
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

        pm = PhaseManager(DatabaseManager())
        pm.workflow_id = workflow_id
        result = pm.mark_phase_complete(
            phase_id,
            "Phase completed",
            phase_output=phase_output,
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
            # adversarial_review_report.md found" while the adversarial
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
   ./.hephaestus/ (e.g. qa_report.md, adversarial_review_report.md,
   security_report.md -- whichever exist for this workflow; each starts
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


def _trigger_arbitration(
    workflow_id: str,
    phase_id: str,
    phase_name: str,
    reason: str,
    logger: OrchestratorLogger,
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
        prior_arbitrations = (
            db.query(Task)
            .filter(
                Task.phase_id == phase_id,
                Task.created_by_agent_id == ARBITRATION_CREATED_BY,
            )
            .count()
        )
        if prior_arbitrations >= max_arbitrations_per_phase:
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

        pm = PhaseManager(DatabaseManager())
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


def _maybe_resolve_arbitration(workflow_id: str, logger: OrchestratorLogger) -> None:
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


def _resolve_arbitration_outcome(
    workflow_id: str,
    phase_id: str,
    phase_name: str,
    decision: str,
    target_phase: Optional[str],
    reason: str,
    logger: OrchestratorLogger,
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

    pm = PhaseManager(DatabaseManager())
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


def _ensure_git_excluded(repo_path: Path, patterns: Dict[str, str], logger: Any) -> None:
    """logger: OrchestratorLogger or the plain module-level logging.Logger --
    called from both. Only uses .warning(), which both support.

    Add `patterns` (path -> one-line comment explaining it) to this
    repo's local, untracked .git/info/exclude, idempotently.

    Not the project's own tracked .gitignore: these are all Hephaestus
    tooling artifacts (worktrees, orchestration state, the ash scanner's
    working directory) -- not something the project itself produces, so
    they have no business in a file the project's real contributors
    maintain. info/exclude is the correct, local-only, per-checkout place
    for exactly this category of thing.

    `repo_path` may be a worktree, not just a repo root -- worktrees don't
    have their own info/exclude, it lives in the shared ("common") git
    dir, so `git rev-parse --git-common-dir` (resolves correctly from a
    worktree, unlike a hardcoded ".git/") is required rather than assuming
    `repo_path / ".git" / "info" / "exclude"`.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(repo_path),
            capture_output=True,
            timeout=10,
            text=True,
        )
        if result.returncode != 0:
            return
        common_dir = Path(result.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = repo_path / common_dir
        exclude_path = common_dir / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text() if exclude_path.exists() else ""
        existing_lines = {line.strip() for line in existing.splitlines()}
        to_add = {p: c for p, c in patterns.items() if p not in existing_lines}
        if not to_add:
            return
        with exclude_path.open("a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            for pattern, comment in to_add.items():
                f.write(f"# {comment} Added automatically by Hephaestus.\n{pattern}\n")
    except Exception as e:
        logger.warning(f"Could not update git exclude at {repo_path}: {e}")


def _run_ash_scan(worktree: Path, logger: OrchestratorLogger) -> None:
    """Run the AWS Automated Security Helper against a feature's worktree.

    security_review.yaml marks this scan MANDATORY, but relying on the agent
    to remember to run it is unreliable — observed live during smoke testing:
    an agent skipped both the mandatory feature-classification step and this
    scan entirely, with no note of it being skipped (which the prompt also
    explicitly asked for on failure). Running it here, unconditionally,
    before the agent starts, removes the compliance gap the same way
    Enhancement 1 (run_independent_test_verification in spec.py) stopped
    trusting agent-reported QA metrics — the orchestrator now guarantees the
    scan happened at all, regardless of what the agent does with the results.
    """
    results_path = worktree / CONTEXT_DIR_NAME / "ash_results.txt"
    _ensure_git_excluded(
        worktree,
        {".ash/": ("AWS Automated Security Helper's own scan working directory (security_review's mandatory ash scan) --")},
        logger,
    )
    try:
        heph_repo = Path(__file__).resolve().parents[2]
        ash_script = heph_repo / "scripts" / "ash"
        if not ash_script.exists():
            logger.warning(f"[ASH] scripts/ash not found at {ash_script}, skipping scan")
            return

        results_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [str(ash_script), "--source-dir", "."],
            cwd=str(worktree),
            capture_output=True,
            timeout=300,
            text=True,
        )
        output = (result.stdout or "") + (result.stderr or "")
        results_path.write_text(output or "(no output)")
        logger.info(f"[ASH] Automated security scan complete (exit code {result.returncode}), results written to {results_path}")
    except subprocess.TimeoutExpired:
        logger.warning("[ASH] Automated security scan timed out after 300s")
        results_path.write_text("SCAN TIMED OUT after 300s")
    except Exception as e:
        logger.warning(f"[ASH] Automated security scan failed: {e}")
        try:
            results_path.write_text(f"SCAN FAILED TO RUN: {e}")
        except Exception:
            pass
    finally:
        # The ash CLI leaves its own raw working directory (.ash/ --
        # per-scanner output, converted files, and an aggregated SARIF
        # results JSON) behind in the worktree root -- observed live at
        # 76MB, with the aggregated JSON alone at 19MB. Two real problems
        # if it's left there: commit_and_link_ticket's `git add -A` after
        # every task completion would commit all of it into the feature
        # branch, and a security_review agent digging past the small
        # summary above into that raw JSON (a natural thing to do when
        # looking for more detail) has been observed crashing its own CLI
        # session trying to parse it inline, over and over on every
        # relaunch. The summary above already has everything the agent
        # needs -- delete the rest regardless of scan outcome.
        try:
            shutil.rmtree(worktree / ".ash", ignore_errors=True)
        except Exception:
            pass


def _cap_out_review_phase(
    db,
    workflow_id: str,
    phase,
    run_count: int,
    max_runs: int,
    logger: OrchestratorLogger,
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
    return _fire_phase_transition(workflow_id, phase.id, phase.name, logger)


def _create_phase_task(
    workflow_id: str,
    phase_id: str,
    phase_name: str,
    action: str,
    logger: OrchestratorLogger,
    feedback: Optional[str] = None,
    source_phase_name: Optional[str] = None,
) -> bool:
    """Create a task and agent for a phase via API.

    source_phase_name: the phase whose evaluation decided `action` -- e.g.
    for a goto, the phase whose gate found something wrong and sent the
    pipeline back here. Recorded as this new task's own action_target_phase
    (same field _tag_completing_task sets on the DECIDING phase's task, just
    the complementary direction: "where I came from" here vs. "where I sent
    things" there). Irrelevant for action="continue" (normal advancement).
    """
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


def _create_corrective_task(
    workflow_id: str,
    phase_id: str,
    phase_name: str,
    feedback: str,
    logger: OrchestratorLogger,
) -> Optional[str]:
    """Create a task asking the agent to fix a specific, known validation
    failure in its already-written output, instead of the phase's whole
    output getting discarded and the entire (expensive) run redone from
    scratch. Reopens the phase/workflow if the engine already marked them
    complete -- a normal 'done' claim doesn't know a downstream hard-floor
    check will later reject it.

    Returns the new task's id, or None if agent creation failed.
    """
    import uuid

    from src.core.database import Phase, PhaseExecution, Task, Workflow, get_db

    task_id = str(uuid.uuid4())
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
            # Same reopen-point fix as _create_phase_task -- this phase may
            # have been marked "completed" with its task_creation_claimed_at
            # already consumed by that prior cycle's evaluation. Without
            # resetting it here, the new corrective task's eventual
            # completion would find the claim already held and
            # _case_in_progress_complete would skip evaluating the
            # transition forever.
            execution.task_creation_claimed_at = None

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
    logger: OrchestratorLogger,
    project_id: Optional[str] = None,
) -> str:
    """Poll a task until it reaches a terminal status or times out.

    Returns "done", "failed", "timeout", or "interrupted".
    """
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
    logger: OrchestratorLogger,
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


def _resume_stuck_workflow_tasks(workflow_id: str, logger: OrchestratorLogger) -> int:
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


def run_single_workflow(
    sdk,
    workflow_id: str,
    project_path: str,
    description: str,
    logger: OrchestratorLogger,
    launch_params: Dict[str, Any] = None,
    state: PipelineState = None,
    max_iterations: int = 10,
    design_id: Optional[str] = None,
    timeout_seconds: int = None,
    pause_existing: bool = True,
    existing_workflow_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> str:
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
    # Update the workflow definition's orchestrator_config with the requested max_iterations.
    # This makes --max-iterations control the engine's max_total_gotos.
    _update_orchestrator_max_gotos(max_iterations, logger)

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
        db = DbManager(str(cfg.database_path))
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
        return "failed"

    stuck_count = 0
    credit_stuck_count = 0
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

    try:
        while True:
            time.sleep(POLL_INTERVAL)

            # Check if in-process service requested a stop
            if _should_stop(project_id):
                logger.info("Stop requested during workflow execution")
                return "interrupted"

            # Timeout check
            elapsed = int(time.time() - start_time)
            if elapsed > timeout_seconds:
                logger.error(f"Workflow timed out after {timeout_seconds}s")
                return "timeout"

            wf_status = get_workflow_status(exec_id)
            # Get agents for this workflow only
            agents = get_agents(workflow_id=exec_id)
            active_agents = [a for a in agents if a.get("status") in ACTIVE_AGENT_STATUSES]
            # Filter tasks by workflow_id to avoid counting tasks from other workflows
            pending = get_tasks(status="pending", workflow_id=exec_id)
            in_progress = get_tasks(status="in_progress", workflow_id=exec_id)
            done = get_tasks(status="done", workflow_id=exec_id)
            failed = get_tasks(status="failed", workflow_id=exec_id)
            # Also check for non-terminal statuses that mean work is still happening
            assigned = get_tasks(status="assigned", workflow_id=exec_id)
            queued = get_tasks(status="queued", workflow_id=exec_id)
            under_review = get_tasks(status="under_review", workflow_id=exec_id)
            validation = get_tasks(status="validation_in_progress", workflow_id=exec_id)
            needs_work = get_tasks(status="needs_work", workflow_id=exec_id)
            blocked = get_tasks(status="blocked", workflow_id=exec_id)
            non_terminal = assigned + queued + under_review + validation + needs_work + blocked

            _log_phase_transitions(exec_id)

            # Log agent spawns and terminations
            current_agent_states = {a["id"]: (a.get("status", ""), a.get("agent_type", "")) for a in agents}
            for aid, (status, atype) in current_agent_states.items():
                prev_status, _ = _last_agent_states.get(aid, (None, None))
                if prev_status is None and status in ACTIVE_AGENT_STATUSES:
                    logger.info(f"  [AGENT SPAWN] {aid[:8]} ({atype}) status={status}")
                elif prev_status in ACTIVE_AGENT_STATUSES and status == "terminated":
                    logger.info(f"  [AGENT DONE]  {aid[:8]} ({atype}) terminated")
                elif prev_status is not None and prev_status != status:
                    logger.info(f"  [AGENT]       {aid[:8]} ({atype}): {prev_status} → {status}")
            _last_agent_states = current_agent_states

            logger.info(f"[{workflow_id}] [{elapsed}s] Agents: {len(active_agents)} active | Tasks: {len(pending)} pending, {len(in_progress)} active, {len(done)} done, {len(failed)} failed")

            # Phase progression — the single source of truth for advancing phases.
            # This replaces the monitor's phase progression logic.
            _advance_phases(exec_id, logger)

            # Refresh task counts after potential phase advancement
            pending = get_tasks(status="pending", workflow_id=exec_id)
            in_progress = get_tasks(status="in_progress", workflow_id=exec_id)

            # Agent scheduling is handled by the server's background_queue_processor.
            # Stuck-agent detection is handled by Guardian/Conductor.
            # The orchestrator only monitors and logs.

            # Parent peeks at children's output periodically for observability
            if elapsed > 0 and elapsed % PARENT_PEEK_INTERVAL < POLL_INTERVAL:
                for agent in active_agents:
                    aid = agent.get("id", "")
                    output = peek_agent_output(aid, lines=15)
                    if output:
                        # Show last meaningful lines (skip blank)
                        lines = [ln.strip() for ln in output.strip().split("\n") if ln.strip()][-8:]
                        if lines:
                            preview = " | ".join(lines[-3:])  # last 3 lines
                            logger.info(f"  [{aid[:8]}] {preview}")

            wf_state = wf_status.get("status", "")
            if wf_state in ("completed", "failed", "paused"):
                logger.info(f"Workflow {wf_state}: {exec_id}")
                return wf_state

            # Check if workflow should be considered complete:
            # No active agents AND no pending/in-progress/non-terminal tasks
            if not active_agents and not pending and not in_progress and not non_terminal:
                # All agents done, no more work to do
                if done:
                    # Verify all phases are completed before declaring workflow done.
                    # This prevents premature completion when the monitor hasn't yet
                    # created the next phase's task.
                    try:
                        from src.core.database import (
                            DatabaseManager,
                            PhaseExecution,
                        )

                        _db = DatabaseManager()
                        _session = _db.get_session()
                        try:
                            pending_phases = (
                                _session.query(PhaseExecution)
                                .filter(
                                    PhaseExecution.workflow_execution_id == exec_id,
                                    PhaseExecution.status.in_(["pending", "in_progress"]),
                                )
                                .count()
                            )
                            if pending_phases > 0:
                                logger.info(f"{len(done)} tasks done but {pending_phases} phases still pending/in_progress — waiting")
                                # Don't declare complete yet; monitor will create next task
                                time.sleep(POLL_INTERVAL)
                                continue
                        finally:
                            _session.close()
                    except Exception as e:
                        logger.warning(f"Could not check phase status: {e}")

                    logger.info(f"Workflow complete: {len(done)} tasks done, no agents active, all phases done")

                    # Final merge: merge the shared design branch into main
                    try:
                        design_branch = getattr(state, "_design_branch", None)
                        if design_branch:
                            from src.core.database import DatabaseManager as DbManager
                            from src.core.simple_config import get_config
                            from src.core.worktree_manager import WorktreeManager

                            cfg = get_config()
                            db = DbManager(str(cfg.database_path))
                            wt_mgr = WorktreeManager(db_manager=db)
                            wt_mgr.reload(Path(project_path))

                            # Ensure main is clean
                            wt_mgr.main_repo.heads[wt_mgr.config.base_branch].checkout()
                            try:
                                wt_mgr.main_repo.git.merge("--abort")
                            except Exception:
                                pass
                            wt_mgr.main_repo.git.reset("--hard", "HEAD")
                            wt_mgr.main_repo.git.clean("-fd")

                            # Merge the design branch
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
                                    # Create PR instead
                                    logger.info(f"Conflict detected — branch {design_branch} preserved for manual merge/PR")
                                else:
                                    raise

                            # Worktree is intentionally kept — UI references artifacts there
                        else:
                            logger.info("No design branch tracked — skipping final merge")
                    except Exception as e:
                        logger.warning(f"Final merge failed: {e}")

                    if state:
                        state.current_workflow_id = None
                    return "completed"
                elif elapsed > 300 and not done:
                    # No tasks AND no done tasks after 5 minutes — something is wrong
                    logger.error(f"No tasks exist after {elapsed}s — workflow appears broken")
                    return "hard_error"

            out_of_credits, credit_reason = check_api_credits()
            if out_of_credits:
                credit_stuck_count += 1
                stuck_count = 0  # reset impasse counter during credit issues
                if credit_stuck_count >= 1:
                    choice = prompt_human(credit_reason, logger)
                    if choice == "q":
                        return "interrupted"
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

            hard_error, error_reason = detect_hard_error(agents, failed, workflow_id=exec_id)
            if hard_error:
                logger.error(f"Hard error detected: {error_reason}")
                return "hard_error"

            impasse, impasse_reason = detect_impasse(agents, pending, in_progress, elapsed)
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
                    choice = prompt_human(impasse_reason, logger)
                    if choice == "q":
                        return "interrupted"
                    elif choice == "s":
                        stuck_count = 0
                        # Skip this design - terminate all active agents for this workflow
                        for a in agents:
                            if a.get("status") in ACTIVE_AGENT_STATUSES:
                                try:
                                    terminate_agent_direct(a["id"])
                                    logger.info(f"Terminated agent {a['id'][:8]} (skip)")
                                except Exception:
                                    pass
                        return "skipped"
                    else:
                        # "c" (continue) or timeout — reset stuck count and keep watching
                        stuck_count = 0
            else:
                stuck_count = 0

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return "interrupted"
    finally:
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

    # Update design status to decomposing
    _update_design_status(design_entry.db_id, "decomposing", logger=logger)

    # Create permanent designs folder
    designs_folder = _create_designs_folder(project_path, design_entry, logger)
    design_entry.designs_folder = designs_folder

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

        if wf_status != "completed":
            logger.error(f"Phase 0 workflow failed with status: {wf_status}")
            _update_design_status(
                design_entry.db_id,
                "failed",
                error=f"Phase 0 failed: {wf_status}",
                logger=logger,
            )
            return None, None

        # Persist phase0_workflow_id IMMEDIATELY after workflow completes,
        # before any post-processing that could fail or crash. This ensures
        # _get_phase0_completion can find the completed workflow for recovery
        # even if the server crashes before _create_feature_records runs.
        from src.core.database import AutopilotDesign as _ADModel, Workflow as _WfModel


        with _get_db() as _db:
            _phase0_wf = _db.query(_WfModel).filter_by(design_id=design_entry.db_id, definition_id="feature_architect").order_by(_WfModel.created_at.desc()).first()
            if _phase0_wf:
                _db.query(_ADModel).filter_by(id=design_entry.db_id).update({_ADModel.phase0_workflow_id: _phase0_wf.id})
                _db.flush()
                logger.info(f"Persisted phase0_workflow_id={_phase0_wf.id[:8]} for {design_entry.name}")

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
        review_src = features_json_path.parent / "feature_review_report.md"
        if review_src.exists():
            shutil.copy2(review_src, designs_folder / review_src.name)

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

        # Create Feature DB records
        feature_records = _create_feature_records(design_entry.db_id, features_json, designs_folder, logger)

        logger.info(f"Phase 0 complete: {len(feature_records)} features created")
        return features_json, designs_folder

    finally:
        # Cleanup worktree
        _cleanup_worktree(worktree, branch, project_path, logger)


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
) -> str:
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

    # Set structured log context for this feature's lifetime
    from src.core.log_context import set_log_context
    set_log_context(workflow=feature_key, phase="feature_pipeline")

    # Find feature record in DB
    from src.core.database import Feature, Workflow, get_db
    from src.core.cost_derivation import check_budget_before_new_work

    feature_id = None
    existing_workflow_id = None
    with get_db() as db:
        # Budget guard: refuse to launch features for over-budget projects
        # (inside same session to avoid race condition with concurrent cost writes)
        if project_id and not check_budget_before_new_work(db, project_id):
            logger.warning(f"[BUDGET] Cannot launch feature {feature_key} — project {project_id[:8]} over budget")
            return "skipped"

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
                    return "completed"
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
                    return "budget_blocked"

            # Update status to active
            feat_record.status = "active"
            feat_record.started_at = feat_record.started_at or datetime.utcnow()
            db.commit()

    if not feature_id:
        logger.error(f"Feature record not found for {feature_key}")
        return "failed"

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
        return "failed"

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
            max_iterations=max_iterations,
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
        if wf_status == "completed":
            # Check if product validation passed
            # For now, mark as completed if workflow completed
            final_status = "completed"
        elif wf_status == "paused":
            # Not a failure -- run_single_workflow returns "paused" for a
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
            final_status = "paused"
        elif wf_status == "interrupted":
            final_status = "failed"
        else:
            final_status = "failed"

        # Update feature status
        _update_feature_status(feature_id, design_entry.db_id, final_status, logger=logger)

        # Sweep artifacts to permanent record. Phase reports now live under
        # .hephaestus/ (git-excluded) -- some flat at the top level
        # (requirements_analysis.md, architecture.md), some one level down
        # in a phase subdirectory (qa_validation/qa_report.md,
        # adversarial_review/adversarial_review_report.md, etc., per each
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

        if wf_status == "completed":
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

        return final_status

    except Exception as e:
        logger.error(f"Feature pipeline failed for {feature_key}: {e}")
        _update_feature_status(feature_id, design_entry.db_id, "failed", str(e), logger)
        # Do not clean up the worktree here either -- an exception mid-
        # pipeline is exactly the case resume needs the worktree to still
        # exist for.
        return "failed"


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
) -> Dict[str, str]:
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
    feature_results: Dict[str, str] = {}

    # Resolve execution order
    execution_groups = _resolve_execution_order(features, logger)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Statuses _run_one_feature/run_single_workflow can return that do NOT
    # mean the feature actually reached a resolved state -- "interrupted"
    # (an explicit stop/quit/KeyboardInterrupt) and "timeout" (this poll
    # loop's own wall-clock budget expired, reset fresh on every resume --
    # see run_single_workflow's start_time) both mean "we stopped watching,"
    # not "this feature is done." Unlike "failed"/"skipped"/"hard_error"
    # (genuine, if bad, resolutions -- see the comment below on why those
    # don't block dependents), advancing to a later dependency layer after
    # one of these is exactly how a still-in-progress dependency's
    # dependents can start early: observed live, a feature whose dependency
    # was still genuinely running (its own workflow status was "active", it
    # simply hadn't finished within this walk's 2-hour polling window) had
    # its dependent feature dispatched immediately after the dependency's
    # run_single_workflow call returned "timeout".
    NON_TERMINAL_STATUSES = {"interrupted", "timeout"}
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
            if status in NON_TERMINAL_STATUSES:
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
                        if status in NON_TERMINAL_STATUSES:
                            halted_early = True
                    except Exception as e:
                        logger.error(f"Feature {feature_key} failed: {e}")
                        feature_results[feature_key] = "failed"

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
        logger.info(f"  {feat_key}: {status}")

    return feature_results


def run_design_aggregate(
    design_entry: DesignEntry,
    feature_results: Dict[str, str],
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
    all_completed = bool(results) and all(s == "completed" for s in results)
    any_failed = any(s == "failed" for s in results)
    any_completed = any(s == "completed" for s in results)
    all_skipped = bool(results) and all(s == "skipped" for s in results)

    if all_completed:
        status = DesignStatus.COMPLETED
    elif any_failed or all_skipped or not any_completed or not results:
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
        "features": feature_results,
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


def _generate_design_report_html(
    design_entry: DesignEntry,
    feature_results: Dict[str, str],
    designs_folder: Path,
    logger: OrchestratorLogger,
) -> None:
    """Generate HTML design report using Jinja2 template.

    Args:
        design_entry: Design entry
        feature_results: Feature results mapping
        designs_folder: Path to designs folder
        logger: Orchestrator logger
    """
    from jinja2 import Environment, FileSystemLoader

    templates_dir = Path(__file__).parent / "templates"
    if not templates_dir.exists():
        logger.warning(f"Templates directory not found: {templates_dir}")
        return

    env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=True)

    try:
        template = env.get_template("design_report.html")
    except Exception as e:
        logger.warning(f"Design report template not found: {e}")
        return

    # Load feature records from DB
    from src.core.database import Feature, get_db

    feature_records = []
    with get_db() as db:
        for feat_key in feature_results:
            feat = (
                db.query(Feature)
                .filter_by(
                    design_id=design_entry.db_id,
                    feature_key=feat_key,
                )
                .first()
            )
            if feat:
                feature_records.append(
                    {
                        "name": feat.name,
                        "status": feat.status,
                        "started_at": feat.started_at.isoformat() if feat.started_at else None,
                        "completed_at": feat.completed_at.isoformat() if feat.completed_at else None,
                    }
                )

    context = {
        "design_name": design_entry.name,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "features": feature_records,
        "total_features": len(feature_records),
        "completed_features": sum(1 for f in feature_records if f["status"] == "completed"),
        "failed_features": sum(1 for f in feature_records if f["status"] == "failed"),
        "skipped_features": sum(1 for f in feature_records if f["status"] == "skipped"),
    }

    html = template.render(**context)
    html_path = designs_folder / "design_report.html"
    html_path.write_text(html)
    logger.info(f"Design report: {html_path}")


def _empty_report(design_entry: DesignEntry) -> FeatureReport:
    """Create an empty FeatureReport for failed designs."""
    return FeatureReport(
        design_name=design_entry.name,
        project_path="",
        feature_folder="",
        design_document=str(design_entry.path),
        iterations=0,
        total_time_seconds=0,
        qa_passed=False,
        product_validated=False,
        stop_reason="failed",
    )


def _archive_and_cleanup(
    design_entry: DesignEntry,
    designs_folder: Path,
    logger: OrchestratorLogger,
) -> None:
    """Copy phase artifacts to the permanent designs folder, then remove the worktree.

    Copies .hephaestus/*.md, *.json, *.html from the shared worktree into
    designs_folder/.hephaestus/ so artifacts survive worktree removal.
    Then removes the linked worktree via `git worktree remove`.
    """
    import shutil
    import subprocess

    project_path = Path(design_entry.project_path) if design_entry.project_path else None
    if not project_path or not project_path.exists():
        return

    # Worktrees live at <project_root>/.worktrees/wt_<name>. The project_path
    # passed to run_single_design IS the worktree root.
    worktree = project_path
    repo_root = worktree.parent.parent  # .worktrees/ -> project root

    # Copy docs. Recurse, not iterate the top level -- some phase reports
    # sit flat at .hephaestus/<file> (requirements_analysis.md,
    # architecture.md), others one level down in a phase subdirectory
    # (qa_validation/qa_report.md, per each gated phase's CRITICAL PATH
    # RULE). Excludes tmux/ (transcript logs), features/ (Phase 0 internal
    # state), and scratch/ (agent scratch space) -- not phase-report
    # artifacts.
    worktree_docs = worktree / ".hephaestus"
    dest_docs = designs_folder / ".hephaestus"
    _archive_excluded_dirs = {"tmux", "features", "scratch"}
    if worktree_docs.exists():
        dest_docs.mkdir(parents=True, exist_ok=True)
        for f in worktree_docs.rglob("*"):
            if not f.is_file():
                continue
            if f.relative_to(worktree_docs).parts[0] in _archive_excluded_dirs:
                continue
            dest = dest_docs / f.name
            if not dest.exists():
                shutil.copy2(f, dest)
        logger.info(f"Artifacts archived to {dest_docs}")

    # Remove the linked worktree
    if ".worktrees" in str(worktree) and worktree.exists():
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info(f"Worktree removed: {worktree}")
        else:
            logger.warning(f"git worktree remove failed: {result.stderr.strip()}")
            subprocess.run(["git", "worktree", "prune"], cwd=str(repo_root))


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
    design_entry.started_at = datetime.now().isoformat()

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

    design_entry.completed_at = datetime.now().isoformat()

    # Note: Phase 0 and feature worktrees are cleaned up by their own finally blocks
    # inside run_phase0() and _run_one_feature(). No additional cleanup needed here.

    return status, report


# project_id -> the AutopilotService's own asyncio.Event, registered by
# AutopilotService._run_pipeline_sync. Was a single bare module global
# (_service_stop_event) -- a second project starting overwrote the first
# project's reference silently, so whichever project's stop() fired last
# won control of BOTH pipelines' stop signal (project A's "stop" could be
# swallowed, or could incorrectly stop project B). Keyed by project_id,
# not workflow_id: AutopilotService is 1:1 with a project, not a workflow
# (run_continuous_pipeline, one of this function's three call sites, spans
# many workflows over its life and has no single workflow_id to key by).
_stop_events: Dict[str, "asyncio.Event"] = {}


def _should_stop(project_id: Optional[str]) -> bool:
    """Check if the pipeline should stop for this project.

    Returns True if the in-process AutopilotService for this project has
    requested a stop (via _stop_events, keyed by project_id). A caller
    that couldn't resolve its own project_id gets False rather than
    guessing at some other project's stop signal.
    """
    if not project_id:
        return False
    event = _stop_events.get(project_id)
    if event is not None:
        try:
            # Non-blocking check
            return event.is_set()
        except Exception:
            pass
    return False


def _interruptible_sleep(seconds: int, project_id: Optional[str]) -> None:
    """Sleep up to `seconds`, but return early if _should_stop(project_id)
    flips during it. A plain time.sleep(seconds) here means a stop request
    (including AutopilotService.pause_for_restart(), see
    docs/SAFE_RESTART_DESIGN.md §3.3) is invisible to the loop for however
    long the sleep already had left -- up to DESIGN_QUEUE_SCAN_INTERVAL
    (60s) at the two call sites that use this. Checking every second
    instead makes that latency ~1s regardless of where in the sleep the
    stop request lands.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _should_stop(project_id):
            return
        time.sleep(max(0, min(1, deadline - time.time())))


def _register_orchestrator_agent(log_dir: Path, cli_tool: str, logger: OrchestratorLogger) -> Optional[str]:
    """Register (or re-register, after a restart) the orchestrator's own
    Agent row, whose id becomes Task.created_by_agent_id for every task the
    orchestrator itself creates (_create_phase_task, _create_corrective_task).

    Returns the new agent's id, or None if registration failed -- in which
    case those task-creation call sites fall back to created_by_agent_id=
    None (the column is nullable).
    """
    try:
        import uuid

        from src.core.database import Agent, DatabaseManager

        db_manager = DatabaseManager()
        session = db_manager.get_session()
        try:
            new_agent_id = f"orchestrator-{uuid.uuid4().hex[:8]}"
            orchestrator_agent = session.query(Agent).filter_by(id=new_agent_id).first()
            if orchestrator_agent:
                orchestrator_agent.status = "working"
                orchestrator_agent.last_activity = datetime.utcnow()
            else:
                # Check if tmux_session_name is already taken
                existing = session.query(Agent).filter_by(tmux_session_name="orchestrator").first()
                if existing:
                    existing.status = "terminated"
                    existing.current_task_id = None  # Clear stale reference
                    existing.terminated_at = datetime.utcnow()
                    # tmux_session_name has a UNIQUE constraint -- marking
                    # the old row "terminated" alone doesn't free the value
                    # "orchestrator" up, so the commit below still collides
                    # with it. Without this, registration silently failed
                    # on every restart after the first (logged as just a
                    # warning), leaving the caller's _orchestrator_agent_id
                    # pointing at an Agent row that was never actually
                    # persisted -- so any task creation using it as
                    # created_by_agent_id (_create_phase_task) hit a
                    # FOREIGN KEY failure the moment FK enforcement was
                    # turned on. Uses the FULL id, not a slice: every
                    # orchestrator agent id shares the literal prefix
                    # "orchestrator-", so id[:8] is always "orchestr" for
                    # every one of them -- not unique at all, and the very
                    # first fix attempt using it collided with itself
                    # across restarts the same way the original bug did.
                    existing.tmux_session_name = f"orchestrator-terminated-{existing.id}"
                orchestrator_agent = Agent(
                    id=new_agent_id,
                    system_prompt=f"LOG_DIR:{log_dir}",
                    status="working",
                    cli_type=cli_tool,
                    agent_type="orchestrator",
                    tmux_session_name="orchestrator",
                )
                session.add(orchestrator_agent)
            session.commit()
            logger.info(f"Registered orchestrator agent: {orchestrator_agent.id[:8]}")
            return new_agent_id
        except Exception as e:
            logger.warning(f"Warning: Could not register orchestrator agent: {e}")
            return None
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"Warning: Could not register orchestrator agent: {e}")
        return None


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

    sys.path.insert(0, str(HEPHAESTUS_DIR))
    from src.autopilot.phases import (
        AUTOPILOT_LAUNCH_TEMPLATE,
        AUTOPILOT_PHASES,
        AUTOPILOT_WORKFLOW_CONFIG,
    )
    from src.sdk import HephaestusSDK
    from src.sdk.models import WorkflowDefinition

    config = get_config()
    cli_tool = os.getenv("HEPHAESTUS_CLI_TOOL") or config.default_cli_tool

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

    all_defs = get_all_workflow_definitions()
    # Add any definitions not already in our list
    known_ids = {autopilot_def.id}
    extra_defs = [d for d in all_defs if d.id not in known_ids]
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
        monitoring_interval=60,
        llm_provider=os.environ.get("LLM_PROVIDER", "openrouter"),
        llm_model=os.environ.get("LLM_MODEL", "xiaomi/mimo-v2.5"),
        default_cli_tool=cli_tool,
        main_repo_path=str(project_path),
        project_root=str(project_path),
        auto_commit=True,
        conflict_resolution="newest_file_wins",
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
            timeout=60,
            assume_backend_running=getattr(args, "in_process", False),
        )
    except Exception as e:
        logger.error(f"Failed to start: {e}")
        sys.exit(1)

    logger.info("Services started.")

    # Register orchestrator as an agent
    global _orchestrator_agent_id
    _orchestrator_agent_id = _register_orchestrator_agent(log_dir, cli_tool, logger)

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

                        # Periodic stale task cleanup (every cycle)
                        try:
                            _clean_stale_assigned_tasks(state.current_workflow_id, logger)
                        except Exception as e:
                            logger.debug(f"Stale task cleanup error: {e}")

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
                    logger.warning(f"Warning: Could not check active workflows: {e}")

                next_design = pick_next_design(queue_dir, processed_hashes, logger, project_id=current_project_id)

                if next_design is None:
                    logger.info(f"Queue empty. Scanning again in {DESIGN_QUEUE_SCAN_INTERVAL}s...")
                    state.queue_status = {
                        "status": "empty",
                        "processed": len(processed_hashes),
                    }
                    logger.save_state(state)
                    _update_orchestrator_status("idle")
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
                _update_orchestrator_status("working")
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

                # Update DB design status
                try:
                    from src.core.database import AutopilotDesign, AutopilotProject
                    from src.core.database import get_db as _get_db

                    with _get_db() as _db:
                        if current_project_id:
                            _proj = _db.query(AutopilotProject).filter_by(id=current_project_id).first()
                        else:
                            _proj = _db.query(AutopilotProject).filter_by(is_active=True).first()
                        if _proj:
                            _des = _db.query(AutopilotDesign).filter_by(project_id=_proj.id, filename=next_design.path.name).first()
                            if _des:
                                _des.status = status.value if hasattr(status, "value") else str(status)
                                _des.feature_folder = str(next_design.feature_folder) if next_design.feature_folder else None
                                if status == DesignStatus.COMPLETED:
                                    _des.completed_at = datetime.utcnow()
                                    # Clear retry counter on success
                                    _delete_project_context(
                                        _db,
                                        f"autopilot_retry_{_des.id}",
                                    )
                                _db.commit()
                except Exception as _db_err:
                    logger.warning(f"Failed to update DB design status: {_db_err}")

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
                _update_orchestrator_status("idle")
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
        _update_orchestrator_status("terminated")

        # Pause all active autopilot workflows belonging to THIS project.
        # Unscoped, this would forcibly pause an unrelated active workflow
        # in a different project just because this project's pipeline
        # stopped -- same class of cross-project collateral damage as the
        # stale current_workflow_id bug fixed alongside this.
        try:
            active_workflows = get_active_workflows(str(project_path), project_id=current_project_id)
            for wf in active_workflows:
                wf_id = wf.get("id", "")
                try:
                    pause_workflow_direct(wf_id)
                    logger.info(f"Paused workflow {wf_id[:8]}")
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Failed to pause workflows: {e}")

        if sdk is not None:
            sdk.shutdown(graceful=True, timeout=15)


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
