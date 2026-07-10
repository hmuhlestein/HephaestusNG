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
from datetime import datetime
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
    DESIGN_SUBDIR,
    DIAGNOSTIC_TASK_PREFIX,
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
PARENT_PEEK_INTERVAL = int(
    os.environ.get("HEPH_PEEK_INTERVAL", "60")
)  # seconds between parent peeks

# Feature Model constants
# FIX: Extracted to config (hephaestus_config.yaml -> autopilot section)
MAX_PHASE0_TIME = 3600  # 1 hour timeout for Phase 0 (deprecated: use config)
MAX_PARALLEL_FEATURES = 4  # max concurrent feature pipelines
MAX_DESIGN_RETRIES = 3  # max times a failed design is auto-retried

# Module-level orchestrator agent ID (set during registration)
_orchestrator_agent_id: Optional[str] = None


def get_litellm_config() -> Dict[str, str]:
    """Read LiteLLM proxy config from environment variables."""
    return {
        "url": os.environ.get("LITELLM_PROXY_URL", ""),
        "api_key": os.environ.get("LITELLM_API_KEY", ""),
        "cost_api_key": os.environ.get("LITELLM_MASTER_KEY", ""),
        "cost_tracking": os.environ.get("LITELLM_COST_TRACKING", "false").lower()
        == "true",
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
    db_id: Optional[str] = (
        None  # autopilot_designs.id — links Workflow back to Design (§9.7)
    )
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


# ProjectContext key for AutopilotService's "was a pipeline running, with
# what args" resume marker -- see src/autopilot/service.py's
# _persist_running_state/load_persisted_state/clear_persisted_state.
_RUNNING_STATE_KEY = "autopilot_running_pipeline"


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

    STATE_KEY = "autopilot_pipeline_state"
    PROCESSED_KEY = "autopilot_processed_designs"

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
        state = PipelineState()
        processed_hashes: Set[str] = set()

        try:
            with get_db() as db:
                state_data = _get_project_context(db, self.STATE_KEY)
            if state_data:
                state = PipelineState.from_dict(state_data)
                logger.info(
                    f"Loaded pipeline state: {state.designs_processed} designs processed"
                )
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
            return (
                current_design is not None or queue_status.get("status") == "processing"
            )
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


def api_post(
    endpoint: str, data: dict = None, timeout: int = 5, headers: dict = None
) -> Optional[dict]:
    """Legacy HTTP POST - prefer direct DB access functions below."""
    try:
        r = requests.post(
            f"{API_BASE}{endpoint}", json=data, timeout=timeout, headers=headers or {}
        )
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


def create_agent_for_task_direct(
    task_id: str, workflow_id: str, phase_id: Optional[str] = None
) -> Optional[dict]:
    """Create an agent for a pending task directly in-process (H-2 fix).

    Mirrors /api/create_agent_for_task (src/mcp/server.py) without a
    self-HTTP round trip. Callers here run in a background thread (not the
    asyncio event loop), so a fresh event loop is spun up to drive the
    async AgentManager.create_agent_for_task call.
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
                    agent_type="phase",
                    use_existing_worktree=True,
                )
            )
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
                agent_ids = (
                    session.query(Task.assigned_agent_id)
                    .filter(Task.workflow_id == workflow_id, Task.assigned_agent_id.isnot(None))
                    .distinct()
                    .all()
                )
                agent_ids = [a[0] for a in agent_ids]
                query = query.filter(Agent.id.in_(agent_ids))
            agents = query.all()
            return [
                {
                    "id": a.id,
                    "status": a.status,
                    "cli_type": a.cli_type,
                    "agent_type": a.agent_type if hasattr(a, 'agent_type') else None,
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
    agent_active = [
        t for t in tasks_in_progress if t.get("assigned_agent_id") == agent_id
    ]
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
                "name": wf.name if hasattr(wf, 'name') else None,
                "created_at": wf.created_at.isoformat() if wf.created_at else None,
            }
    except Exception as e:
        logger.debug(f"[get_workflow_status] Failed: {e}")
        return {}


def get_active_workflows() -> list:
    """Get list of active workflows directly from database (H-2 fix)."""
    try:
        with get_db() as session:
            workflows = session.query(Workflow).filter(Workflow.status == "active").all()
            return [
                {
                    "id": wf.id,
                    "status": wf.status,
                    "name": wf.name if hasattr(wf, 'name') else None,
                    "created_at": wf.created_at.isoformat() if wf.created_at else None,
                }
                for wf in workflows
            ]
    except Exception as e:
        logger.debug(f"[get_active_workflows] Failed: {e}")
        return []


def is_design_fully_complete(
    workflow_id: str, logger: OrchestratorLogger
) -> Tuple[bool, str]:
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
    real_pending = [
        t
        for t in (pending + queued + in_progress + assigned)
        if not (t.get("raw_description") or "").startswith(DIAGNOSTIC_TASK_PREFIX)
    ]
    if real_pending:
        task_ids = [t.get("id", "")[:8] for t in real_pending[:3]]
        return False, f"{len(real_pending)} task(s) still active: {', '.join(task_ids)}"

    # Failed tasks: only block if the same phase has NO subsequent done task
    # (i.e., a retry succeeded → the failure is resolved).
    done_phase_ids = {t.get("phase_id") for t in done if t.get("phase_id")}
    unresolved_failures = [
        t
        for t in failed
        if t.get("phase_id") not in done_phase_ids
        and not (t.get("raw_description") or "").startswith(DIAGNOSTIC_TASK_PREFIX)
    ]
    if unresolved_failures:
        task_ids = [t.get("id", "")[:8] for t in unresolved_failures[:3]]
        return (
            False,
            f"{len(unresolved_failures)} unresolved failed task(s): {', '.join(task_ids)}",
        )

    # Check for active agents
    agents = get_agents(workflow_id=workflow_id)
    active_agents = [
        a for a in agents if a.get("status") in ("working", "starting", "idle")
    ]
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
            branches = [
                b.strip().lstrip("* ")
                for b in result.stdout.strip().split("\n")
                if b.strip()
            ]
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

        # Only retry if not retried too many times
        retry_count = task.get("retry_count", 0)
        if retry_count >= 2:
            logger.info(
                f"  Task {task_id[:8]} failed {retry_count} times - skipping retry"
            )
            continue

        logger.info(f"  Retrying failed task {task_id[:8]} (retry #{retry_count + 1})")
        # Persist the increment before attempting — counting only successful
        # attempts would let a task that fails every single retry (e.g. a
        # deleted worktree) loop forever, since retry_count would never
        # reach the >= 2 cutoff above.
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
                logger.error(
                    f"  Agent {agent_id[:8]} created for task {task_id[:8]} but "
                    f"failed to link it to the task row: {e3}"
                )
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
                        logger.info(
                            f"  Task {task.id[:8]} assigned to terminated agent {task.assigned_agent_id[:8]} — marking failed"
                        )
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
    active_agents = [
        a for a in agents if a.get("status") in ("working", "starting", "idle")
    ]
    for agent in active_agents:
        aid = agent.get("id", "")
        tmux_name = agent.get("tmux_session_name")
        try:
            alive = bool(tmux_name) and subprocess.run(
                ["tmux", "has-session", "-t", tmux_name],
                capture_output=True,
                timeout=3,
            ).returncode == 0
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


def detect_hard_error(
    agents: list, failed_tasks: list, workflow_id: str = None
) -> Tuple[bool, str]:
    # Filter to only tasks from the current workflow if provided
    if workflow_id:
        failed_tasks = [t for t in failed_tasks if t.get("workflow_id") == workflow_id]

    # Check for crashed/errored agents (agents list is already scoped by get_agents)
    crashed_agents = [a for a in agents if a.get("status") == "error"]
    if crashed_agents:
        names = [a.get("id", "unknown")[:20] for a in crashed_agents[:3]]
        return True, f"Crashed agents: {', '.join(names)}"

    critical_failures = [
        t
        for t in failed_tasks
        if t.get("priority") == "critical"
        or "architectural" in (t.get("description", "") or "").lower()
    ]
    if critical_failures:
        descs = [t.get("description", "")[:60] for t in critical_failures[:3]]
        return True, f"Critical task failures: {descs}"

    return False, ""


def detect_impasse(
    agents: list, pending_tasks: list, in_progress_tasks: list, elapsed_seconds: int = 0
) -> Tuple[bool, str]:
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
                    response_file.unlink(
                        missing_ok=True
                    )  # Delete response, keep waiting
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
    logger.event(
        "human_input", {"choice": "timeout", "reason": reason, "request_id": request_id}
    )
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
            ordered.extend(
                sorted(by_filename.values(), key=lambda d: d.path.name.lower())
            )
            return ordered
        except (json.JSONDecodeError, KeyError):
            pass  # Fall back to default sort

    designs.sort(key=lambda d: d.path.name.lower())
    return designs


def pick_next_design(
    queue_dir: Path, processed_hashes: Set[str], logger: OrchestratorLogger
) -> Optional[DesignEntry]:
    """Pick the next design to process.

    Reads from DB (autopilot_designs) if available, falls back to file scan.
    Uses file_path column if available, falls back to filename-based path.
    """
    # Try DB-based queue first
    try:
        from src.core.database import AutopilotDesign, AutopilotProject, Feature, Workflow, get_db

        with get_db() as db:
            # Find active project
            project = db.query(AutopilotProject).filter_by(is_active=True).first()
            if not project:
                logger.info("pick_next_design: no active project found")
                return None
            
            logger.info(
                f"pick_next_design: searching project '{project.name}' ({project.id[:8]})"
            )
            
            # Get next pending design ordered by ordinal
            design = (
                db.query(AutopilotDesign)
                .filter_by(project_id=project.id, status="pending")
                .order_by(AutopilotDesign.ordinal, AutopilotDesign.filename)
                .first()
            )

            if design:
                logger.info(
                    f"pick_next_design: found pending design '{design.name}' ({design.id[:8]})"
                )
            
            if design is None:
                # Resume support: a design that already finished Phase 0
                # (status moved to "active") but was stopped mid-feature-
                # pipeline is invisible to the "pending" query above.
                active_designs = (
                    db.query(AutopilotDesign)
                    .filter_by(project_id=project.id, status="active")
                    .order_by(AutopilotDesign.ordinal, AutopilotDesign.filename)
                    .all()
                )
                logger.info(
                    f"pick_next_design: no pending designs, found "
                    f"{len(active_designs)} active design(s)"
                )
                for candidate in active_designs:
                    incomplete = (
                        db.query(Feature)
                        .filter(
                            Feature.design_id == candidate.id,
                            Feature.status.notin_(["completed", "skipped"]),
                        )
                        .count()
                    )
                    
                    # Check if any associated workflow has failed
                    failed_wf = (
                        db.query(Workflow)
                        .filter(
                            Workflow.design_id == candidate.id,
                            Workflow.status == "failed",
                        )
                        .first()
                    )
                    
                    logger.info(
                        f"  Active design '{candidate.name}' ({candidate.id[:8]}): "
                        f"incomplete={incomplete}, failed_wf={failed_wf.id[:8] if failed_wf else 'None'}, "
                        f"status={candidate.status}"
                    )
                    
                    if incomplete > 0:
                        if failed_wf:
                            # Failed workflow with incomplete features — retry
                            retry_key = f"autopilot_retry_{candidate.id}"
                            retry_count = _get_project_context(db, retry_key) or 0
                            if retry_count >= MAX_DESIGN_RETRIES:
                                logger.info(
                                    f"Design {candidate.name} has failed "
                                    f"workflow {failed_wf.id[:8]} and "
                                    f"exceeded {MAX_DESIGN_RETRIES} retries "
                                    f"({retry_count}/{MAX_DESIGN_RETRIES}) — marking failed"
                                )
                                candidate.status = "failed"
                                # Surfaced by /autopilot/status's last_error so
                                # the UI can show a real popup instead of this
                                # silently sitting in a log file no one reads
                                # -- the pipeline itself keeps polling "queue
                                # empty" every 60s afterward, looking healthy,
                                # while having permanently given up on the
                                # only design in the queue.
                                candidate.error = (
                                    f"Gave up after {MAX_DESIGN_RETRIES} retries: "
                                    f"workflow {failed_wf.id[:8]} kept failing"
                                )
                                db.commit()
                                continue
                            logger.info(
                                f"Design {candidate.name} has failed "
                                f"workflow {failed_wf.id[:8]} — resetting "
                                f"to pending for retry ({retry_count + 1}/{MAX_DESIGN_RETRIES})"
                            )
                            _set_project_context(db, retry_key, retry_count + 1)
                            candidate.status = "pending"
                            candidate.error = None  # clear any stale exhausted-retry message
                            db.commit()
                            continue
                        design = candidate
                        logger.info(
                            f"Resuming active design {design.name} "
                            f"({incomplete} feature(s) not yet complete)"
                        )
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
                            logger.info(
                                f"Design {candidate.name} has all features done "
                                f"but failed workflow {failed_wf.id[:8]} — "
                                f"retrying ({retry_count + 1}/{MAX_DESIGN_RETRIES})"
                            )
                            _set_project_context(db, retry_key, retry_count + 1)
                            candidate.status = "pending"
                            db.commit()
                            continue
                        # All features done, no failed workflows — mark done.
                        candidate.status = "completed"
                        db.commit()
                        logger.info(
                            f"Design {candidate.name} has all features "
                            f"completed/skipped — marking done"
                        )
                
                if design is None:
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
                    design_path = (
                        Path(project.base_dir) / DESIGN_CONTEXT_SUBDIR / design.filename
                    )

                if design_path.exists():
                    entry = DesignEntry(
                        path=design_path,
                        name=design.name,
                        content_hash=design.content_hash or file_hash(design_path),
                        db_id=design.id,
                        file_path=str(design_path),
                    )
                    logger.info(
                        f"Selected from DB: {design.name} (ordinal={design.ordinal})"
                    )
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
            project = _db.query(AutopilotProject).filter_by(is_active=True).first()
            if project:
                db_design = _db.query(AutopilotDesign).filter_by(
                    project_id=project.id, filename=next_design.path.name
                ).first()
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
                    logger.info(
                        f"Auto-created AutopilotDesign row for "
                        f"{next_design.path.name} (none existed for project "
                        f"{project.id})"
                    )
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
            goto_lines = [l for l in lines if "[GOTO]" in l]
            decision_lines = [l for l in lines if "DECISION POINT" in l]
            health["goto_count"] = len(goto_lines)
            health["goto_events"] = goto_lines[-10:]
            health["decision_points"] = [l.strip() for l in decision_lines]
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
                hits = [
                    l.strip()
                    for l in text.splitlines()
                    if any(p in l for p in error_patterns)
                ]
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
        logger.info(
            f"Run health: PROBLEMS DETECTED — "
            f"gotos={health['goto_count']} tmux_errors={health['error_count']}"
        )

    return health


def create_feature_folder(
    project_path: Path, design_name: str, logger: OrchestratorLogger
) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = design_name.lower().replace(" ", "_")[:40]
    # Features go in .hephaestus/features/ to keep project root clean
    feature_folder = (
        project_path / CONTEXT_DIR_NAME / "features" / f"{timestamp}_{safe_name}"
    )
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
                logger.warning(
                    f"Found stale non-worktree directory at {wt_path} "
                    "(no .git) -- removing before recreating"
                )
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
                        wfs = _s.query(Workflow).filter(
                            Workflow.working_directory == str(worktree),
                            Workflow.status.notin_(["active", "paused"]),
                        ).all()
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
    designs_folder = (
        project_path
        / CONTEXT_DIR_NAME
        / "designs"
        / f"{timestamp}_{safe_name}_{design_entry.db_id or 'unknown'}"
    )
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
        feature = (
            db.query(Feature).filter_by(id=feature_id, design_id=design_id).first()
        )
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
                    logger.warning(
                        f"_update_design_status: unknown field {key!r} for AutopilotDesign"
                    )
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

    Note: assumes exactly one Phase/PhaseExecution per Phase 0 workflow, true today
    (01_feature_architect.yaml declares a single phase). If Phase 0 ever gains a
    second phase this needs revisiting.

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
        if not wf:
            return None
        phase = db.query(Phase).filter_by(workflow_id=wf.id).first()
        execution = (
            db.query(PhaseExecution).filter_by(phase_id=phase.id).first()
            if phase
            else None
        )
        if execution and execution.status == "completed":
            return {"workflow_id": wf.id, "designs_folder": design.designs_folder}
        return None


def _link_workflow_to_feature(workflow_id: str, feature_id: str) -> None:
    """Link a workflow to a feature.

    Sets Feature.workflow_id so the UI can find tasks for each feature.

    Args:
        workflow_id: Workflow ID
        feature_id: Feature ID (feat-...)
    """
    from src.core.database import Feature, get_db

    with get_db() as db:
        feature = db.query(Feature).filter_by(id=feature_id).first()
        if feature:
            feature.workflow_id = workflow_id
            db.commit()
            logger.info(f"Linked workflow {workflow_id[:8]} to feature {feature_id}")


def _relink_features_to_workflows(design_id: str, logger: OrchestratorLogger) -> None:
    """Re-link features to their workflows if workflow_id is missing.

    Handles pipeline restarts where features exist but their workflow link
    was lost. Matches features to workflows by feature_key in launch_params.
    """
    import json as _json

    from src.core.database import Feature, Workflow, get_db

    with get_db() as db:
        unlinked = (
            db.query(Feature)
            .filter_by(design_id=design_id, workflow_id=None)
            .all()
        )
        if not unlinked:
            return

        # Get all autopilot workflows for this design's project
        workflows = (
            db.query(Workflow)
            .filter(Workflow.definition_id == "autopilot")
            .order_by(Workflow.created_at.desc())
            .all()
        )

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
    """Clean tasks that are 'assigned' or 'in_progress' to terminated agents.

    Called periodically from the polling loop to prevent tasks from hanging
    forever when agents crash or are killed.
    """
    from src.core.database import Agent, Task, get_db

    with get_db() as db:
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
                logger.info(
                    f"[STALE-TASK] Task {task.id[:8]} assigned to terminated agent "
                    f"{task.assigned_agent_id[:8]} — marking failed"
                )
                task.status = "failed"
                # Don't clobber a real reason: update_task_status already
                # records why a "done" claim was rejected (e.g. a missing
                # output artifact) on this same field before the agent's
                # session ends. Only fall back to the generic message when
                # nothing more specific is already there -- otherwise the
                # retry below loses exactly the feedback it needs to fix.
                if not task.failure_reason:
                    task.failure_reason = (
                        f"Agent {task.assigned_agent_id[:8]} terminated unexpectedly"
                    )
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
        raise ValueError(
            f"features array has {len(features)} entries -- that's not a "
            "feature decomposition, it looks like one feature per file"
        )

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
                    raise ValueError(
                        f"File overlap between features: {f} and {existing}"
                    )
            all_files.append(f)

    # Validate depends_on references
    for feat in features:
        depends_on = feat.get("depends_on", [])
        for dep in depends_on:
            if dep not in ids:
                raise ValueError(
                    f"Feature {feat['id']} depends on unknown feature: {dep}"
                )

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
        logger.warning(
            f"Cycle detected in dependencies among {unprocessed}; "
            "appending cyclic features sequentially after already-resolved groups"
        )
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
    "qa_result.json",
    "product_validation.json",
    "scope_review_result.json",
    "arbitration_result.json",
}
_STRAY_DIRS: set = set()  # no directories swept until re-validated


def _sweep_stray_files(
    project_path: Path,
    feature_folder: Path,
    docs_dir: Path,
    logger: OrchestratorLogger,
) -> None:
    """Move known ephemeral report files from project root into feature docs.

    Only files whose lowercased name appears in _SWEEP_REPORT_NAMES are
    eligible — source files, design docs, scripts, and anything else in
    the project tree are never touched.
    """
    if not SWEEP_ENABLED:
        return

    docs_dir.mkdir(parents=True, exist_ok=True)

    # ── known report files written to ./docs/ by agents ────────────
    proj_docs = project_path / _REPORT_SUBDIR
    if proj_docs.is_dir() and proj_docs.resolve() != docs_dir.resolve():
        for f in proj_docs.iterdir():
            if f.is_file() and f.name.lower() in _SWEEP_REPORT_NAMES:
                dest = docs_dir / f.name
                if not dest.exists():
                    shutil.copy2(str(f), str(dest))
                    logger.info(f"Copied report: docs/{f.name} -> features/.../docs/")

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


_REPORT_SUBDIR = "docs"


def _report_path(project_path: Path, filename: str) -> Path:
    """Locate a report an agent wrote.

    Under worktree isolation agents write reports to ./docs/ (relative to their
    worktree), which merges to <project>/docs/. Prefer that location; fall back
    to the project root. Does NOT iterate worktrees (too slow for per-turn calls).
    """
    in_docs = project_path / _REPORT_SUBDIR / filename
    if in_docs.exists():
        return in_docs
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
                if (
                    ".venv" in str(f)
                    or "node_modules" in str(f)
                    or "__pycache__" in str(f)
                ):
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

    # Read existing structured result from Phase 8 (preferred)
    structured_path = _report_path(project_path, "product_validation.json")
    if structured_path.exists():
        try:
            result = json.loads(structured_path.read_text())
            verdict = result.get("verdict", "").upper()
            meets_spec = verdict == "PASS" and qa_passed
            logger.info(f"Using structured product_validation.json: verdict={verdict}")
            return meets_spec, structured_path.read_text()
        except Exception:
            pass

    # Fallback: read existing markdown report from Phase 8
    if validation_path.exists():
        try:
            existing = validation_path.read_text()
            meets_spec = qa_passed and (
                "PASS" in existing or "pass" in existing.lower()
            )
            logger.info("Using existing product validation from Phase 8")
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

    Skips workflows the user explicitly paused (wf.paused_by == "user", set
    by the /workflow-executions/{id}/stop endpoint). Without this check, a
    deliberate pause could get silently reverted within one sweep tick
    (~20s) whenever the paused workflow's in-progress phase happens to have
    a done task sitting in it -- a state pausing itself commonly produces
    (the running task finishes right after being told to stop). Observed
    live: a user's pause click appeared to do nothing for a long time,
    because this function kept flipping the workflow back to "active"
    every cycle until whatever made the phase look stalled resolved on its
    own.
    """
    if wf.paused_by == "user":
        return
    phases = (
        db.query(Phase)
        .filter_by(workflow_id=workflow_id)
        .order_by(Phase.order)
        .all()
    )
    for phase in phases:
        exec = db.query(PhaseExecution).filter_by(phase_id=phase.id).first()
        if exec and exec.status == "in_progress":
            done_task = (
                db.query(Task)
                .filter_by(phase_id=phase.id, status="done")
                .first()
            )
            if done_task:
                logger.info(
                    f"[PHASE-ADVANCE] Auto-resuming paused workflow — "
                    f"{phase.name} has done task {done_task.id[:8]}"
                )
                wf.status = "active"
                db.commit()
                break


def _get_phase_statuses(db, workflow_id: str) -> list:
    """Get all phases with their execution statuses."""
    phases = (
        db.query(Phase)
        .filter_by(workflow_id=workflow_id)
        .order_by(Phase.order)
        .all()
    )

    phase_statuses = []
    for phase in phases:
        exec = db.query(PhaseExecution).filter_by(phase_id=phase.id).first()
        phase_statuses.append({
            "phase": phase,
            "execution": exec,
            "status": exec.status if exec else "pending",
        })
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
    claimed_at = datetime.now()
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


def _case_start_first_phase(
    db, workflow_id: str, pending: list, in_progress: list, completed: list, logger: OrchestratorLogger
) -> Optional[bool]:
    """Case 0: No in-progress phase and first phase is pending — start it.

    Returns None if this case doesn't apply, True/False otherwise.
    """
    if not in_progress and not completed and pending:
        first_phase = min(pending, key=lambda p: p["phase"].order)
        # Check if it already has tasks
        existing = (
            db.query(Task)
            .filter_by(phase_id=first_phase["phase"].id)
            .count()
        )
        if existing == 0 and not _claim_phase_task_creation(db, first_phase["phase"].id):
            # Someone else (or a previous iteration of this same loop) is
            # already creating this phase's first task -- don't duplicate it.
            existing = 1
        if existing == 0:
            logger.info(
                f"[PHASE-ADVANCE] Starting first phase: {first_phase['phase'].name}"
            )
            return _create_phase_task(
                workflow_id,
                first_phase["phase"].id,
                first_phase["phase"].name,
                "continue",
                logger,
            )
    return None


def _case_in_progress_no_tasks(
    db, workflow_id: str, in_progress: list, logger: OrchestratorLogger
) -> Optional[bool]:
    """Case 0b: In-progress phase with no tasks at all.
    
    Workflow engine set it but didn't create task.
    Returns None if this case doesn't apply, True/False otherwise.
    """
    for ps in in_progress:
        phase = ps["phase"]
        task_count = (
            db.query(Task)
            .filter_by(phase_id=phase.id)
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
            logger.info(
                f"[PHASE-ADVANCE] Phase {phase.name} is in_progress but has no tasks — creating one"
            )
            return _create_phase_task(
                workflow_id,
                phase.id,
                phase.name,
                "continue",
                logger,
            )
    return None


def _case_completed_with_successor(
    db, workflow_id: str, completed: list, pending: list, in_progress: list, logger: OrchestratorLogger
) -> Optional[bool]:
    """Case 1: Completed phase with pending successor.
    
    Phase N done, next never started.
    Returns None if this case doesn't apply, True/False otherwise.
    """
    if completed and pending and not in_progress:
        completed.sort(key=lambda p: p["phase"].order)
        last_completed = completed[-1]
        # Find the next pending phase by order (handles non-sequential orders)
        successor = min(
            (p for p in pending if p["phase"].order > last_completed["phase"].order),
            key=lambda p: p["phase"].order,
            default=None,
        )
        if successor:
            # Check if successor already has tasks (transition already fired)
            existing_tasks = (
                db.query(Task)
                .filter_by(phase_id=successor["phase"].id)
                .count()
            )
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

            logger.info(
                f"[PHASE-ADVANCE] {last_completed['phase'].name} completed, "
                f"advancing to {successor['phase'].name}"
            )
            return _create_phase_task(
                workflow_id,
                successor["phase"].id,
                successor["phase"].name,
                "continue",
                logger,
            )
    return None


def _case_in_progress_complete(
    db, workflow_id: str, in_progress: list, logger: OrchestratorLogger
) -> Optional[bool]:
    """Case 2: In-progress phase that is now complete.
    
    Returns None if this case doesn't apply, True/False otherwise.
    """
    for ps in in_progress:
        phase = ps["phase"]
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
        incomplete = (
            db.query(Task)
            .filter(
                Task.phase_id == phase.id,
                Task.status.in_(["pending", "assigned", "in_progress"]),
                ~Task.raw_description.like(f"{DIAGNOSTIC_TASK_PREFIX}%"),
            )
            .count()
        )
        if incomplete > 0:
            continue  # Still has active tasks

        done_count = (
            db.query(Task)
            .filter_by(phase_id=phase.id, status="done")
            .count()
        )
        if done_count == 0:
            # Check if ALL tasks are failed — retry them
            result = _maybe_retry_failed_tasks(db, phase, logger)
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
            logger.info(
                f"[PHASE-ADVANCE] {phase.name} transition already being "
                "evaluated by another caller — skipping"
            )
            continue

        logger.info(
            f"[PHASE-ADVANCE] {phase.name} appears complete "
            f"({done_count} tasks done, 0 active), evaluating transition"
        )
        # Extract primitives before session closes to avoid DetachedInstanceError
        phase_id = phase.id
        phase_name = phase.name
        return _fire_phase_transition(workflow_id, phase_id, phase_name, logger)
    return None


def _maybe_retry_failed_tasks(db, phase, logger: OrchestratorLogger) -> Optional[bool]:
    """Retry all failed tasks in a phase if all tasks are failed.
    
    Returns None if no retry was needed, True if tasks were reset for retry.
    """
    failed_count = (
        db.query(Task)
        .filter_by(phase_id=phase.id, status="failed")
        .count()
    )
    total_count = db.query(Task).filter_by(phase_id=phase.id).count()
    if failed_count > 0 and failed_count == total_count:
        logger.info(
            f"[PHASE-ADVANCE] Phase {phase.name} has {failed_count} failed tasks "
            f"and 0 done — retrying all"
        )
        # Reset all failed tasks to pending for retry. Per-task (not a bulk
        # .update()) so each one's own failure_reason -- e.g. a specific
        # "missing output artifact: X" from update_task_status's validation
        # gate, or a real error preserved by _clean_stale_assigned_tasks --
        # gets folded into what the next agent actually reads
        # (enriched_description) before being cleared. A blind reset here
        # previously threw the reason away entirely, so the retried agent
        # got the same generic phase description and no idea what to fix.
        failed_tasks = (
            db.query(Task)
            .filter(Task.phase_id == phase.id, Task.status == "failed")
            .all()
        )
        reset_task_ids = []
        for task in failed_tasks:
            if task.failure_reason:
                base = task.enriched_description or task.raw_description or ""
                task.enriched_description = (
                    f"{base}\n\n--- RETRY: your previous attempt failed with this "
                    f"specific error, fix it rather than repeating the same "
                    f"mistake ---\n{task.failure_reason}"
                )
            task.status = "pending"
            task.failure_reason = None
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
                    logger.warning(
                        f"[PHASE-ADVANCE] Retry agent creation failed for task "
                        f"{task_id[:8]} in {phase.name} -- marked failed for "
                        "another retry pass"
                    )
                    continue
                retry_task.assigned_agent_id = agent_data.get("agent_id", "unknown")
                retry_task.status = "in_progress"
                retry_task.started_at = datetime.utcnow()
                retry_db.commit()
        return True
    return None


def _fire_phase_transition(
    workflow_id: str, phase_id: str, phase_name: str, logger: OrchestratorLogger
) -> bool:
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
                        phase_name, Path(wf.working_directory)
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

        logger.info(
            f"[PHASE-ADVANCE] Engine decision for {phase_name}: {action}" +
            (f" -> {target_phase_name}" if target_phase_name else "")
        )

        if action == "already_completed":
            # Phase was already advanced by another caller (spec gate, etc.)
            # Don't create a duplicate task.
            return False

        if action == "arbitrate":
            # TODO: spawn arbitration agent via API
            logger.warning(f"[PHASE-ADVANCE] Arbitration needed for {phase_name}")
            return True

        if not target_phase_id:
            # Workflow complete or no next phase
            return True

        # Create task and agent for the next phase
        return _create_phase_task(workflow_id, target_phase_id, target_phase_name, action, logger)

    except Exception as e:
        logger.warning(f"[PHASE-ADVANCE] Transition error: {e}")
        return False


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
        logger.info(
            f"[ASH] Automated security scan complete (exit code {result.returncode}), "
            f"results written to {results_path}"
        )
    except subprocess.TimeoutExpired:
        logger.warning("[ASH] Automated security scan timed out after 300s")
        results_path.write_text("SCAN TIMED OUT after 300s")
    except Exception as e:
        logger.warning(f"[ASH] Automated security scan failed: {e}")
        try:
            results_path.write_text(f"SCAN FAILED TO RUN: {e}")
        except Exception:
            pass


def _create_phase_task(
    workflow_id: str,
    phase_id: str,
    phase_name: str,
    action: str,
    logger: OrchestratorLogger,
) -> bool:
    """Create a task and agent for a phase via API."""
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
                        logger.info(
                            "[PHASE-TASK] forensics_analysis skipped — run was "
                            "clean (no tmux error patterns detected)"
                        )
                        # _fire_phase_transition marks this phase complete via
                        # PhaseManager itself and advances to the next phase —
                        # the same completion path a real agent would trigger
                        # via update_task_status, just fired synthetically.
                        return _fire_phase_transition(
                            workflow_id, phase_id, phase_name, logger
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
                logger.info(
                    f"[PHASE-TASK] {phase_name} already has active task {existing.id[:8]}, skipping"
                )
                return False

            # Check for active agent on this phase
            active_agent = (
                db.query(Agent)
                .filter(Agent.status.in_(["working", "idle", "starting"]))
                .join(Task, Task.assigned_agent_id == Agent.id)
                .filter(Task.phase_id == phase_id)
                .first()
            )
            if active_agent:
                logger.info(
                    f"[PHASE-TASK] {phase_name} has active agent {active_agent.id[:8]}, skipping"
                )
                return False

            # Check retry/goto bounds
            MAX_PHASE_ATTEMPTS = 3
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
                if retries >= MAX_PHASE_ATTEMPTS:
                    logger.warning(
                        f"[PHASE-TASK] {phase_name} hit retry bound ({retries}/{MAX_PHASE_ATTEMPTS}), pausing"
                    )
                    wf = db.query(Workflow).filter_by(id=workflow_id).first()
                    if wf and wf.status == "active":
                        wf.status = "paused"
                        db.commit()
                    return False

            # Get phase info
            phase = db.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                return False

            # Create task
            task_id = str(uuid.uuid4())
            task = Task(
                id=task_id,
                raw_description=f"Execute {phase.name}: {phase.description}",
                enriched_description=f"Execute {phase.name}: {phase.description}",
                done_definition=(
                    " AND ".join(phase.done_definitions)
                    if phase.done_definitions
                    else "Complete phase objectives"
                ),
                status="pending",
                priority="high",
                phase_id=phase.id,
                workflow_id=workflow_id,
                created_by_agent_id="orchestrator",
                action=action,
            )
            db.add(task)

            # Update phase execution to in_progress
            execution = db.query(PhaseExecution).filter_by(phase_id=phase_id).first()
            if execution:
                if execution.status in ("pending", "completed"):
                    execution.status = "in_progress"
                    from datetime import datetime
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
            logger.warning(
                f"[PHASE-TASK] Failed to create agent for {phase_name}, cleaning up task {task_id[:8]}"
            )
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
                from datetime import datetime
                task.started_at = datetime.utcnow()
                db.commit()

        logger.info(
            f"[PHASE-TASK] Created task {task_id[:8]} and agent {agent_id[:8]} for {phase_name}"
        )
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
        if wf.paused_by == "user":
            # Same class of bug _try_auto_resume_paused_workflow was fixed
            # for: don't override a deliberate pause. Unlike that function
            # (which just skips and leaves the workflow alone), this one
            # would otherwise both reactivate the workflow AND immediately
            # spawn a live agent against it -- silently resuming real work
            # on something the user explicitly stopped.
            logger.info(
                f"[CORRECTIVE-TASK] Workflow {workflow_id[:8]} is user-paused — "
                "skipping corrective task"
            )
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
        done_def = (
            " AND ".join(phase.done_definitions)
            if phase and phase.done_definitions
            else "Complete phase objectives"
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
            done_definition=done_def,
            status="pending",
            priority="high",
            phase_id=phase_id,
            workflow_id=workflow_id,
            created_by_agent_id="orchestrator",
            action="retry",
        )
        db.add(task)
        db.commit()

    agent_data = create_agent_for_task_direct(task_id, workflow_id, phase_id)
    if not agent_data:
        logger.warning(
            f"[CORRECTIVE-TASK] Failed to create agent for corrective task on {phase_name}"
        )
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

    logger.info(
        f"[CORRECTIVE-TASK] Created task {task_id[:8]} and agent {agent_id[:8]} "
        f"to fix: {feedback}"
    )
    return task_id


def _wait_for_task_terminal(task_id: str, timeout_seconds: int, logger: OrchestratorLogger) -> str:
    """Poll a task until it reaches a terminal status or times out.

    Returns "done", "failed", "timeout", or "interrupted".
    """
    from src.core.database import Task, get_db

    start = time.time()
    while time.time() - start < timeout_seconds:
        if _should_stop():
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
        logger.info(
            f"[NEGOTIATE] Attempt {attempt}/{max_attempts} for {phase_name}: {error}"
        )
        task_id = _create_corrective_task(workflow_id, phase_id, phase_name, error, logger)
        if not task_id:
            return False, None

        result = _wait_for_task_terminal(task_id, timeout_seconds, logger)
        if result not in ("done",):
            logger.warning(
                f"[NEGOTIATE] Corrective task {result} for {phase_name} — giving up"
            )
            return False, None

        try:
            parsed = json.loads(output_path.read_text())
            validate_fn(parsed)
            logger.info(f"[NEGOTIATE] {phase_name} fixed on attempt {attempt}")
            return True, parsed
        except (json.JSONDecodeError, ValueError) as e:
            error = str(e)
            logger.warning(f"[NEGOTIATE] Still invalid after attempt {attempt}: {error}")

    logger.error(
        f"[NEGOTIATE] {phase_name} still failing validation after {max_attempts} "
        f"corrective attempts: {error}"
    )
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
        if wf.status == "paused" and wf.paused_by == "user":
            # Same class of bug _try_auto_resume_paused_workflow was fixed
            # for: this runs whenever the design/feature queue loop cycles
            # back to a workflow it already has an id for, which can
            # include one the user deliberately paused -- don't silently
            # un-pause and restart work on it.
            logger.info(
                f"[RESUME-STUCK] Workflow {workflow_id[:8]} is user-paused — skipping"
            )
            return 0
        if wf.status in ("paused", "failed"):
            wf.status = "active"

        candidates = (
            db.query(Task)
            .filter(
                Task.workflow_id == workflow_id,
                Task.status.in_(["blocked", "failed", "assigned", "in_progress"]),
            )
            .all()
        )
        restartable = []
        for t in candidates:
            if t.status in ("blocked", "failed"):
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
) -> str:
    """Run a single workflow execution.

    Args:
        max_iterations: Maps to the engine's max_total_gotos.
        timeout_seconds: Hard deadline for this workflow (default: from config).
            Pass 0 or a custom value for Phase 0 runs.
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
    if not pause_existing:
        existing_workflows = []
    else:
        existing_workflows = [
            wf for wf in get_active_workflows() if wf.get("id") != existing_workflow_id
        ]
    if existing_workflows:
        logger.info(
            f"Found {len(existing_workflows)} active workflow(s) - stopping them..."
        )
        for wf in existing_workflows:
            wf_id = wf.get("id", "")
            try:
                # Terminate agents for this workflow
                agents = get_agents(workflow_id=wf_id)
                for agent in agents:
                    if agent.get("status") in ACTIVE_AGENT_STATUSES:
                        try:
                            terminate_agent_direct(agent["id"])
                            logger.info(
                                f"  Terminated agent {agent['id'][:8]} for workflow {wf_id[:8]}"
                            )
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
    design_name = (
        Path(design_doc).stem.replace("_", " ").replace("-", " ") if design_doc else ""
    )
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
        if '.worktrees/' in str(project_path):
            design_worktree_path = str(project_path)
            logger.info(
                f"Using existing worktree directly: {design_worktree_path}"
            )
        else:
            # Reload to point at the actual project repo (not config.main_repo_path)
            wt_mgr.reload(Path(project_path))

            # Create feature branch from main
            import git as _git

            # Use design_entry name if available, otherwise derive from design_doc
            _design_label = (
                design_name.replace(" ", "-").lower() if design_name else "design"
            )
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
            logger.info(
                f"Created shared worktree: {design_worktree_path} (branch: {feature_branch})"
            )

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
                    logger.info(
                        f"Restoring working_directory for {exec_id[:8]}: "
                        f"{_wf_resume.working_directory!r} -> {design_worktree_path}"
                    )
                    _wf_resume.working_directory = design_worktree_path
            restarted = _resume_stuck_workflow_tasks(exec_id, logger)
            logger.info(
                f"Resume: reset {restarted} stuck task(s) for workflow {exec_id[:8]}"
            )
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
            PersistentPipelineState().save_state_only(state)

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
                        logger.info(
                            f"  [GOTO] {name}: completed → in_progress (rewound by earlier phase)"
                        )
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
            if _should_stop():
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
            active_agents = [
                a for a in agents if a.get("status") in ACTIVE_AGENT_STATUSES
            ]
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
            non_terminal = (
                assigned + queued + under_review + validation + needs_work + blocked
            )

            _log_phase_transitions(exec_id)

            # Log agent spawns and terminations
            current_agent_states = {
                a["id"]: (a.get("status", ""), a.get("agent_type", "")) for a in agents
            }
            for aid, (status, atype) in current_agent_states.items():
                prev_status, _ = _last_agent_states.get(aid, (None, None))
                if prev_status is None and status in ACTIVE_AGENT_STATUSES:
                    logger.info(f"  [AGENT SPAWN] {aid[:8]} ({atype}) status={status}")
                elif prev_status in ACTIVE_AGENT_STATUSES and status == "terminated":
                    logger.info(f"  [AGENT DONE]  {aid[:8]} ({atype}) terminated")
                elif prev_status is not None and prev_status != status:
                    logger.info(
                        f"  [AGENT]       {aid[:8]} ({atype}): {prev_status} → {status}"
                    )
            _last_agent_states = current_agent_states

            logger.info(
                f"[{workflow_id}] [{elapsed}s] Agents: {len(active_agents)} active | "
                f"Tasks: {len(pending)} pending, {len(in_progress)} active, "
                f"{len(done)} done, {len(failed)} failed"
            )

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
                        lines = [
                            l.strip() for l in output.strip().split("\n") if l.strip()
                        ][-8:]
                        if lines:
                            preview = " | ".join(lines[-3:])  # last 3 lines
                            logger.info(f"  [{aid[:8]}] {preview}")

            wf_state = wf_status.get("status", "")
            if wf_state in ("completed", "failed", "paused"):
                logger.info(f"Workflow {wf_state}: {exec_id}")
                return wf_state

            # Check if workflow should be considered complete:
            # No active agents AND no pending/in-progress/non-terminal tasks
            if (
                not active_agents
                and not pending
                and not in_progress
                and not non_terminal
            ):
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
                                    PhaseExecution.status.in_(
                                        ["pending", "in_progress"]
                                    ),
                                )
                                .count()
                            )
                            if pending_phases > 0:
                                logger.info(
                                    f"{len(done)} tasks done but {pending_phases} phases still pending/in_progress — waiting"
                                )
                                # Don't declare complete yet; monitor will create next task
                                time.sleep(POLL_INTERVAL)
                                continue
                        finally:
                            _session.close()
                    except Exception as e:
                        logger.warning(f"Could not check phase status: {e}")

                    logger.info(
                        f"Workflow complete: {len(done)} tasks done, no agents active, all phases done"
                    )

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
                                logger.info(
                                    f"Final merge complete: {design_branch} -> main ({merge_sha[:8]})"
                                )
                            except _git.exc.GitCommandError as e:
                                if "CONFLICT" in str(e):
                                    logger.warning(
                                        f"Merge conflict on {design_branch} -> main, aborting"
                                    )
                                    wt_mgr.main_repo.git.merge("--abort")
                                    # Create PR instead
                                    logger.info(
                                        f"Conflict detected — branch {design_branch} preserved for manual merge/PR"
                                    )
                                else:
                                    raise

                            # Worktree is intentionally kept — UI references artifacts there
                        else:
                            logger.info(
                                "No design branch tracked — skipping final merge"
                            )
                    except Exception as e:
                        logger.warning(f"Final merge failed: {e}")

                    if state:
                        state.current_workflow_id = None
                    return "completed"
                elif elapsed > 300 and not done:
                    # No tasks AND no done tasks after 5 minutes — something is wrong
                    logger.error(
                        f"No tasks exist after {elapsed}s — workflow appears broken"
                    )
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
                logger.info(
                    f"[ORCHESTRATOR] Received {len(high_confidence_signals)} "
                    f"monitor signals for workflow {exec_id[:8]}"
                )
                for sig in high_confidence_signals:
                    logger.info(f"[ORCHESTRATOR] Signal: {sig}")
                    # Signal metadata could be used for more nuanced decisions
                    # For now, signals factor into stuck_count below

            hard_error, error_reason = detect_hard_error(
                agents, failed, workflow_id=exec_id
            )
            if hard_error:
                logger.error(f"Hard error detected: {error_reason}")
                return "hard_error"

            impasse, impasse_reason = detect_impasse(
                agents, pending, in_progress, elapsed
            )
            # Enhancement 4: Monitor signals can also indicate impasse.
            # Require at least 2 high-confidence stuck signals to avoid false
            # positives from a single Guardian assessment firing too aggressively.
            if not impasse and high_confidence_signals:
                stuck_signals = [
                    s for s in high_confidence_signals
                    if s.type in (SignalType.STUCK_PATTERN, SignalType.PHASE_STUCK)
                ]
                if len(stuck_signals) >= 2:
                    impasse = True
                    impasse_reason = (
                        f"Monitor detected {len(stuck_signals)} stuck signals: "
                        f"{'; '.join(s.evidence[:50] for s in stuck_signals[:3])}"
                    )
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
                                    logger.info(
                                        f"Terminated agent {a['id'][:8]} (skip)"
                                    )
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
) -> Tuple[Optional[dict], Optional[Path]]:
    """Run Phase 0: Feature Architect to decompose design into features.

    Args:
        sdk: HephaestusSDK instance
        design_entry: Design entry being processed
        project_path: Path to the project root
        logger: Orchestrator logger
        state: Pipeline state

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
        existing_feature_data = [
            {"id": f.feature_key, "name": f.name, "scope": f.scope, "files": f.files or [], "depends_on": f.depends_on or [], "execution": f.execution}
            for f in existing_features
        ]
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
                logger.info(
                    f"Phase 0 workflow {completion['workflow_id'][:8]} already "
                    f"completed for {design_entry.name} — resuming feature-record "
                    "creation without re-running the agent"
                )
                feature_records = _create_feature_records(
                    design_entry.db_id, features_json, designs_folder, logger
                )
                logger.info(
                    f"Phase 0 resumed: {len(feature_records)} features created"
                )
                return features_json, designs_folder
            except (json.JSONDecodeError, ValueError, OSError) as e:
                logger.warning(
                    f"Phase 0 workflow {completion['workflow_id'][:8]} completed "
                    f"but its features.json could not be resumed ({e}) — "
                    "falling through to a full re-run"
                )
        else:
            logger.warning(
                f"Phase 0 workflow {completion['workflow_id'][:8]} completed but "
                f"no features.json found at {features_json_path} — falling "
                "through to a full re-run"
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
    branch = f"autopilot-phase0/{design_entry.db_id or 'unknown'}"
    worktree = _create_integration_worktree(
        project_path, design_entry.db_id or "", branch, logger
    )

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
            "autopilot-phase0",
            str(worktree),
            description,
            logger,
            launch_params=launch_params,
            state=state,
            max_iterations=3,
            design_id=design_entry.db_id,
            timeout_seconds=_get_phase0_timeout(),
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
            candidates = [
                p for p in worktree.rglob("features.json")
                if p.stat().st_size > 0
            ]
            if candidates:
                features_json_path = candidates[0]
                logger.warning(
                    f"features.json not at expected path; found at {features_json_path}"
                )
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
                    neg_wf = (
                        _ndb.query(_NegWF)
                        .filter_by(design_id=design_entry.db_id, definition_id="autopilot-phase0")
                        .order_by(_NegWF.created_at.desc())
                        .first()
                    )
                    neg_phase = (
                        _ndb.query(_NegPhase)
                        .filter_by(workflow_id=neg_wf.id)
                        .order_by(_NegPhase.order)
                        .first()
                        if neg_wf else None
                    )

                if neg_wf and neg_phase:
                    fixed, negotiated_json = _negotiate_validation_fix(
                        neg_wf.id,
                        neg_phase.id,
                        neg_phase.name,
                        features_json_path,
                        _validate_features_json,
                        str(e),
                        logger,
                    )
                    if fixed:
                        features_json = negotiated_json
                else:
                    logger.warning(
                        "Could not locate Phase 0 workflow/phase for corrective "
                        "negotiation — failing outright"
                    )
            except Exception as negotiate_err:
                logger.error(
                    f"Corrective negotiation itself failed unexpectedly: "
                    f"{negotiate_err} — failing design with the original "
                    "validation error"
                )
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
        # already gone (the same reason _run_one_feature's analogous
        # _link_workflow_to_feature call below is a pre-existing no-op; not
        # fixing that here, out of scope, but avoiding relying on the same
        # broken channel for this new code). Query the just-created Workflow
        # row directly instead, via the design_id/definition_id it was
        # created with — robust regardless of that state-clearing behavior.
        # Clear any stale error from a prior failed attempt on this same
        # design (e.g. a validation failure that negotiation then fixed, or
        # an earlier run that failed before a later retry succeeded) --
        # otherwise a resolved problem keeps showing up in the design modal
        # forever, since nothing else ever clears this column.
        update_kwargs = {"designs_folder": str(designs_folder), "error": None}
        from src.core.database import Workflow as _WF
        with _get_db() as _db:
            phase0_wf = (
                _db.query(_WF)
                .filter_by(design_id=design_entry.db_id, definition_id="autopilot-phase0")
                .order_by(_WF.created_at.desc())
                .first()
            )
            phase0_wf_id = phase0_wf.id if phase0_wf else None
        if phase0_wf_id:
            update_kwargs["phase0_workflow_id"] = phase0_wf_id
            _set_workflow_type(phase0_wf_id, "design")
        else:
            logger.warning(
                f"Could not find the just-created Phase 0 workflow for design "
                f"{design_entry.db_id} — phase0_workflow_id will not be set, "
                "so a future crash-recovery re-entrant call will re-run the "
                "full agent instead of resuming (correctness is unaffected, "
                "only the recovery optimization is skipped)"
            )
        _update_design_status(
            design_entry.db_id,
            "active",
            logger=logger,
            **update_kwargs,
        )

        # Create Feature DB records
        feature_records = _create_feature_records(
            design_entry.db_id, features_json, designs_folder, logger
        )

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

    Returns:
        Feature status string (completed, failed, skipped)
    """
    feature_key = feature.get("id", "unknown")
    feature_name = feature.get("name", feature_key)

    logger.info(f"Starting feature pipeline: {feature_name} ({feature_key})")

    # Find feature record in DB
    from src.core.database import Feature, Workflow, get_db

    feature_id = None
    existing_workflow_id = None
    with get_db() as db:
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
                    logger.info(
                        f"Feature {feature_key} already completed (workflow "
                        f"{wf.id[:8]}) — skipping"
                    )
                    return "completed"
                if wf:
                    existing_workflow_id = wf.id

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
        _update_feature_status(
            feature_id, design_entry.db_id, "failed", "Worktree creation failed", logger
        )
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

        wf_status = run_single_workflow(
            sdk,
            "autopilot",
            str(worktree),
            description,
            logger,
            launch_params=launch_params,
            state=state,
            max_iterations=max_iterations,
            design_id=design_entry.db_id,
            pause_existing=False,  # features run in parallel; don't clobber each other
            existing_workflow_id=existing_workflow_id,
        )

        # Link workflow to feature in DB
        if state and state.current_workflow_id and feature_id:
            _link_workflow_to_feature(state.current_workflow_id, feature_id)

        # Determine final status
        if wf_status == "completed":
            # Check if product validation passed
            # For now, mark as completed if workflow completed
            final_status = "completed"
        elif wf_status == "interrupted":
            final_status = "failed"
        else:
            final_status = "failed"

        # Update feature status
        _update_feature_status(
            feature_id, design_entry.db_id, final_status, logger=logger
        )

        # Sweep artifacts to permanent record
        docs_dir = worktree / "docs"
        if docs_dir.exists():
            for f in docs_dir.iterdir():
                if f.is_file():
                    dest = feature_record_path / f.name
                    if not dest.exists():
                        shutil.copy2(f, dest)

        return final_status

    except Exception as e:
        logger.error(f"Feature pipeline failed for {feature_key}: {e}")
        _update_feature_status(feature_id, design_entry.db_id, "failed", str(e), logger)
        return "failed"
    finally:
        # Cleanup worktree
        _cleanup_worktree(worktree, branch, project_path, logger)


def run_feature_pipelines(
    sdk,
    design_entry: DesignEntry,
    features_json: dict,
    designs_folder: Path,
    project_path: Path,
    logger: OrchestratorLogger,
    state: Optional[PipelineState] = None,
    max_iterations: int = 10,
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
            )
            feature_results[feature_key] = status
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
                    ): feat
                    for feat in features_to_run
                }

                for future in as_completed(future_to_feature):
                    feat = future_to_feature[future]
                    feature_key = feat.get("id", "unknown")
                    try:
                        status = future.result()
                        feature_results[feature_key] = status
                    except Exception as e:
                        logger.error(f"Feature {feature_key} failed: {e}")
                        feature_results[feature_key] = "failed"

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
        _generate_design_report_html(
            design_entry, feature_results, designs_folder, logger
        )
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
                        "started_at": feat.started_at.isoformat()
                        if feat.started_at
                        else None,
                        "completed_at": feat.completed_at.isoformat()
                        if feat.completed_at
                        else None,
                    }
                )

    context = {
        "design_name": design_entry.name,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "features": feature_records,
        "total_features": len(feature_records),
        "completed_features": sum(
            1 for f in feature_records if f["status"] == "completed"
        ),
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

    Copies docs/*.md, *.json, *.html from the shared worktree into
    designs_folder/docs/ so artifacts survive worktree removal.
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

    # Copy docs
    worktree_docs = worktree / "docs"
    dest_docs = designs_folder / "docs"
    if worktree_docs.exists():
        dest_docs.mkdir(parents=True, exist_ok=True)
        for f in worktree_docs.iterdir():
            if f.is_file():
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
    features_json, designs_folder = run_phase0(
        sdk, design_entry, project_path, logger, state
    )
    if features_json is None:
        raise RuntimeError(
            f"Phase 0 failed to produce features.json for design '{design_entry.name}'. "
            "Check the autopilot-phase0 workflow and agent logs."
        )

    # ── Stage 2: Per-feature pipelines ──
    # Re-link features to their workflows if missing (handles pipeline restarts)
    _relink_features_to_workflows(design_entry.db_id, logger)

    feature_results = run_feature_pipelines(
        sdk, design_entry, features_json, designs_folder,
        project_path, logger, state, max_iterations,
    )

    # ── Stage 3: Design aggregate ──
    status, report = run_design_aggregate(
        design_entry, feature_results, designs_folder, logger
    )

    design_entry.completed_at = datetime.now().isoformat()

    # Note: Phase 0 and feature worktrees are cleaned up by their own finally blocks
    # inside run_phase0() and _run_one_feature(). No additional cleanup needed here.

    return status, report


def _should_stop() -> bool:
    """Check if the pipeline should stop.

    Returns True if the in-process AutopilotService has requested a stop
    (via the module-level _service_stop_event).
    """
    event = globals().get("_service_stop_event")
    if event is not None:
        try:
            # Non-blocking check
            return event.is_set()
        except Exception:
            pass
    return False


def run_continuous_pipeline(args) -> None:
    log_dir = Path(AUTOPILOT_STATE_DIR) / datetime.now().strftime("run-%Y%m%d-%H%M%S")
    logger = OrchestratorLogger(log_dir)

    # Load persistent state from previous runs
    persistent_state = PersistentPipelineState()
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
    logger.info(
        f"Control Model: Engine evaluation points (max_total_gotos={args.max_iterations})"
    )
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

    # Load all workflow definitions from registry (including autopilot-phase0)
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
        database_path=os.environ.get(
            "DATABASE_PATH", str(HEPHAESTUS_DIR / "hephaestus.db")
        ),
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
    try:
        import uuid

        from src.core.database import Agent, DatabaseManager

        db_manager = DatabaseManager()
        session = db_manager.get_session()
        try:
            _orchestrator_agent_id = f"orchestrator-{uuid.uuid4().hex[:8]}"
            orchestrator_agent = session.query(Agent).filter_by(id=_orchestrator_agent_id).first()
            if orchestrator_agent:
                orchestrator_agent.status = "working"
                orchestrator_agent.last_activity = datetime.utcnow()
            else:
                # Check if tmux_session_name is already taken
                existing = session.query(Agent).filter_by(tmux_session_name="orchestrator").first()
                if existing:
                    existing.status = "terminated"
                    existing.current_task_id = None  # Clear stale reference
                orchestrator_agent = Agent(
                    id=_orchestrator_agent_id,
                    system_prompt=f"LOG_DIR:{log_dir}",
                    status="working",
                    cli_type=cli_tool,
                    agent_type="orchestrator",
                    tmux_session_name="orchestrator",
                )
                session.add(orchestrator_agent)
            session.commit()
            logger.info(f"Registered orchestrator agent: {orchestrator_agent.id[:8]}")
        except Exception as e:
            logger.warning(f"Warning: Could not register orchestrator agent: {e}")
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"Warning: Could not register orchestrator agent: {e}")

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

    try:
        while True:
            # Check if in-process service requested a stop
            if _should_stop():
                logger.info("Stop requested by AutopilotService")
                break

            now = time.time()

            if now - last_queue_scan >= DESIGN_QUEUE_SCAN_INTERVAL:
                last_queue_scan = now

                # Check if any workflow is still active - don't start a new design while one is running
                try:
                    active_workflows = get_active_workflows()
                    if active_workflows:
                        wf_ids = [wf.get("id", "")[:8] for wf in active_workflows]
                        logger.info(
                            f"Workflow still active ({', '.join(wf_ids)}) - waiting before picking next design"
                        )
                        state.queue_status = {
                            "status": "waiting",
                            "reason": "workflow_active",
                            "active_workflows": wf_ids,
                        }
                        logger.save_state(state)
                        persistent_state.save(state, processed_hashes)
                        time.sleep(POLL_INTERVAL)
                        continue

                    # Also check previous workflow is fully complete (all phases done, branches merged)
                    if state.current_workflow_id:
                        # First check if workflow still exists in DB
                        try:
                            wf_check = get_workflow_status(state.current_workflow_id)
                            wf_check_status = wf_check.get("status", "")
                            if not wf_check_status:
                                # Workflow no longer exists in DB — clear stale state
                                logger.info(
                                    f"Previous workflow {state.current_workflow_id[:8]} no longer exists in DB, clearing stale state"
                                )
                                state.current_workflow_id = None
                                continue
                        except Exception:
                            logger.info(
                                f"Previous workflow {state.current_workflow_id[:8]} could not be checked, clearing stale state"
                            )
                            state.current_workflow_id = None
                            continue

                        is_complete, reason = is_design_fully_complete(
                            state.current_workflow_id, logger
                        )

                        # Periodic stale task cleanup (every cycle)
                        try:
                            _clean_stale_assigned_tasks(state.current_workflow_id, logger)
                        except Exception as e:
                            logger.debug(f"Stale task cleanup error: {e}")

                        if not is_complete:
                            logger.info(f"Previous workflow not yet complete: {reason}")

                            # Track recovery attempts to prevent infinite loops
                            if not hasattr(state, "_recovery_attempts"):
                                state._recovery_attempts = 0
                            state._recovery_attempts += 1

                            if state._recovery_attempts > 5:
                                logger.warning(
                                    f"Recovery failed after {state._recovery_attempts} attempts, escalating to impasse for workflow {state.current_workflow_id[:8]}"
                                )
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
                                        wf = (
                                            db.query(Workflow)
                                            .filter_by(id=state.current_workflow_id)
                                            .first()
                                        )
                                        if wf:
                                            wf.status = "failed"
                                            db.commit()
                                            logger.warning(
                                                f"Workflow {state.current_workflow_id[:8]} marked as failed (abandoned phase)"
                                            )
                                except Exception as e:
                                    logger.error(
                                        f"Failed to mark workflow as failed: {e}"
                                    )
                                state.current_workflow_id = None
                                state._recovery_attempts = 0
                                continue

                            # Attempt recovery
                            success, recovery_msg = attempt_recovery(
                                state.current_workflow_id, logger
                            )
                            if success:
                                logger.info(f"Recovery actions: {recovery_msg}")

                            state.queue_status = {
                                "status": "waiting",
                                "reason": reason,
                                "recovery": recovery_msg if success else None,
                            }
                            logger.save_state(state)
                            persistent_state.save(state, processed_hashes)
                            time.sleep(POLL_INTERVAL)
                            continue
                        else:
                            logger.info(f"Previous workflow fully complete: {reason}")
                            state.current_workflow_id = None
                except Exception as e:
                    logger.warning(f"Warning: Could not check active workflows: {e}")

                next_design = pick_next_design(queue_dir, processed_hashes, logger)

                if next_design is None:
                    logger.info(
                        f"Queue empty. Scanning again in {DESIGN_QUEUE_SCAN_INTERVAL}s..."
                    )
                    state.queue_status = {
                        "status": "empty",
                        "processed": len(processed_hashes),
                    }
                    logger.save_state(state)
                    _update_orchestrator_status("idle")
                    persistent_state.save(state, processed_hashes)
                    time.sleep(DESIGN_QUEUE_SCAN_INTERVAL)
                    continue

                next_design.status = DesignStatus.IN_PROGRESS
                state.current_design = next_design.name
                state.current_feature_folder = (
                    str(next_design.feature_folder)
                    if next_design.feature_folder
                    else None
                )
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
                    )
                    # Save state AFTER run_single_design so current_workflow_id is captured
                    logger.save_state(state)
                    persistent_state.save(state, processed_hashes)
                except Exception as _design_err:
                    logger.error(
                        f"run_single_design raised unexpectedly for "
                        f"'{next_design.name}': {_design_err}"
                    )
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
                        _proj = (
                            _db.query(AutopilotProject)
                            .filter_by(is_active=True)
                            .first()
                        )
                        if _proj:
                            _des = (
                                _db.query(AutopilotDesign)
                                .filter_by(
                                    project_id=_proj.id, filename=next_design.path.name
                                )
                                .first()
                            )
                            if _des:
                                _des.status = (
                                    status.value
                                    if hasattr(status, "value")
                                    else str(status)
                                )
                                _des.feature_folder = (
                                    str(next_design.feature_folder)
                                    if next_design.feature_folder
                                    else None
                                )
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
                logger.info(
                    f"Design '{next_design.name}' complete. Status: {status.value}"
                )
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

        # Pause all active autopilot workflows
        try:
            active_workflows = get_active_workflows()
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

    parser = argparse.ArgumentParser(
        description="Autopilot Continuous Pipeline - Design Queue to Validated Software"
    )
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
    parser.add_argument(
        "--drop-db", action="store_true", help="Drop database before starting"
    )

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
