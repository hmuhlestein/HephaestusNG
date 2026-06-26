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

import os
import sys
import git as _git
import time
import json
import shutil
import subprocess
import hashlib
import logging
import html as html_mod
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any, Set
from dataclasses import dataclass, field
from enum import Enum

# Module-level logger for persistent state operations
logger = logging.getLogger(__name__)

HEPHAESTUS_DIR = Path(__file__).parent.parent.parent
API_BASE = os.environ.get("HEPHAESTUS_API_BASE", "http://127.0.0.1:8300")

from src.core.constants import AUTOPILOT_STATE_DIR
from src.core.simple_config import get_config

POLL_INTERVAL = 15
STUCK_THRESHOLD = 3
DESIGN_QUEUE_SCAN_INTERVAL = 60
HEARTBEAT_INTERVAL = 300
MAX_WORKFLOW_TIME = 7200  # 2 hours per workflow execution
ACTIVE_AGENT_STATUSES = {"working", "idle"}  # Excludes 'created' (not yet started), 'stuck', 'terminated'
PARENT_PEEK_INTERVAL = int(os.environ.get("HEPH_PEEK_INTERVAL", "60"))  # seconds between parent peeks


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


class PersistentPipelineState:
    """Manages pipeline state that survives restarts."""

    def __init__(self):
        self.state_dir = Path(AUTOPILOT_STATE_DIR)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "pipeline_state.json"
        self.processed_file = self.state_dir / "processed_designs.json"

    def save(self, state: PipelineState, processed_hashes: Set[str]):
        """Save pipeline state and processed designs to disk.

        Write order: processed_designs first, then state.
        If crash occurs between them, design is safely skipped (in processed)
        but state undercounts by 1 - safer than double-processing.
        """
        # Write processed_designs first (safer on crash)
        with open(self.processed_file, "w") as f:
            json.dump(list(processed_hashes), f)

        # Then write state
        state_data = state.to_dict()
        state_data["saved_at"] = datetime.now().isoformat()
        with open(self.state_file, "w") as f:
            json.dump(state_data, f, indent=2)

    def load(self) -> Tuple[PipelineState, Set[str]]:
        """Load pipeline state and processed designs from disk."""
        state = PipelineState()
        processed_hashes: Set[str] = set()

        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    state_data = json.load(f)
                state = PipelineState.from_dict(state_data)
                logger.info(f"Loaded pipeline state: {state.designs_processed} designs processed")
            except Exception as e:
                logger.warning(f"Failed to load pipeline state: {e}")

        if self.processed_file.exists():
            try:
                with open(self.processed_file) as f:
                    processed_hashes = set(json.load(f))
                logger.info(f"Loaded {len(processed_hashes)} processed designs")
            except Exception as e:
                logger.warning(f"Failed to load processed designs: {e}")

        return state, processed_hashes

    def clear(self):
        """Clear persisted state (for fresh start)."""
        if self.state_file.exists():
            self.state_file.unlink()
        if self.processed_file.exists():
            self.processed_file.unlink()

    def has_incomplete_work(self) -> bool:
        """Check if there's incomplete work from a previous run."""
        if not self.state_file.exists():
            return False

        try:
            with open(self.state_file) as f:
                state_data = json.load(f)

            # Check if there was a design in progress
            current_design = state_data.get("current_design")
            queue_status = state_data.get("queue_status", {})

            return current_design is not None or queue_status.get("status") == "processing"
        except Exception:
            return False

    def get_last_run_id(self) -> Optional[str]:
        """Get the run ID from the last persisted state."""
        if not self.state_file.exists():
            return None
        try:
            with open(self.state_file) as f:
                state_data = json.load(f)
            return state_data.get("run_id")
        except Exception:
            return None


class OrchestratorLogger:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = log_dir / "orchestrator.log"
        self.events_file = log_dir / "events.jsonl"
        self.state_file = log_dir / "state.json"

    def log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] [{level}] {message}"
        try:
            print(line, flush=True)
        except OSError:
            pass  # Broken pipe when running as subprocess with DEVNULL
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
        with open(self.events_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def save_state(self, state: PipelineState):
        with open(self.state_file, "w") as f:
            json.dump({
                "designs_processed": state.designs_processed,
                "designs_succeeded": state.designs_succeeded,
                "designs_failed": state.designs_failed,
                "total_elapsed": state.total_elapsed,
                "current_design": state.current_design,
                "queue_status": state.queue_status,
            }, f, indent=2)


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def api_get(endpoint: str, timeout: int = 5) -> Optional[dict]:
    try:
        r = requests.get(f"{API_BASE}{endpoint}", timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def api_post(endpoint: str, data: dict = None, timeout: int = 5, headers: dict = None) -> Optional[dict]:
    try:
        r = requests.post(f"{API_BASE}{endpoint}", json=data, timeout=timeout, headers=headers or {})
        if r.status_code == 200:
            return r.json()
        else:
            print(f"[api_post] {endpoint} returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[api_post] {endpoint} failed: {e}")
    return None


def get_tasks(status: str = None, workflow_id: str = None) -> list:
    params = []
    if status:
        params.append(f"status={status}")
    if workflow_id:
        params.append(f"workflow_id={workflow_id}")
    query = f"?{'&'.join(params)}" if params else ""
    data = api_get(f"/api/tasks{query}")
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("tasks", [])


def get_agents(workflow_id: str = None) -> list:
    """Get agents, optionally filtered by workflow_id via their assigned tasks."""
    # Get ALL agents (not paginated) for internal use
    data = api_get("/api/agents?status=all&per_page=100")
    if data is None:
        return []
    agents = data if isinstance(data, list) else data.get("agents", [])

    if not workflow_id:
        return agents

    # Filter agents to only those working on tasks in this workflow
    # Get all tasks for this workflow
    tasks = get_tasks(workflow_id=workflow_id)
    agent_ids = set()
    for t in tasks:
        if t.get('assigned_agent_id'):
            agent_ids.add(t['assigned_agent_id'])
        if t.get('created_by_agent_id'):
            agent_ids.add(t['created_by_agent_id'])

    return [a for a in agents if a.get('id') in agent_ids]


def peek_agent_output(agent_id: str, lines: int = 30) -> str:
    """Peek at the last N lines of an agent's tmux output."""
    data = api_get(f"/api/agents/{agent_id}/output?lines={lines}")
    if data is None:
        return ""
    return data.get("output", "") if isinstance(data, dict) else str(data)


def get_task_progress(agent_id: str) -> dict:
    """Check an agent's task progress."""
    tasks = get_tasks(status="done")
    agent_done = [t for t in tasks if t.get("assigned_agent_id") == agent_id]
    tasks_in_progress = get_tasks(status="in_progress")
    agent_active = [t for t in tasks_in_progress if t.get("assigned_agent_id") == agent_id]
    return {"done": len(agent_done), "in_progress": len(agent_active)}


def get_workflow_status(workflow_id: str) -> dict:
    return api_get(f"/api/workflow-executions/{workflow_id}") or {}


def get_active_workflows() -> list:
    """Get list of active workflows (excluding the one we're about to start)."""
    data = api_get("/api/workflow-executions") or []
    if isinstance(data, dict):
        data = data.get("executions", [])
    return [w for w in data if w.get("status") in ("active", "running")]


def is_design_fully_complete(workflow_id: str, logger: OrchestratorLogger) -> Tuple[bool, str]:
    """Check if a design is fully complete:
    1. All phases done (no pending/in_progress/queued tasks)
    2. No active agents
    3. All agent branches merged to main
    4. No failed tasks

    Returns:
        (is_complete, reason) tuple
    """
    # Check workflow status
    wf = get_workflow_status(workflow_id)
    wf_status = wf.get('status', '')
    if wf_status not in ('completed', 'active', 'running', 'paused'):
        return False, f"Workflow status: {wf_status}"

    # Check task statuses
    pending = get_tasks(status='pending', workflow_id=workflow_id)
    queued = get_tasks(status='queued', workflow_id=workflow_id)
    in_progress = get_tasks(status='in_progress', workflow_id=workflow_id)
    assigned = get_tasks(status='assigned', workflow_id=workflow_id)
    failed = get_tasks(status='failed', workflow_id=workflow_id)
    done = get_tasks(status='done', workflow_id=workflow_id)

    # All non-done statuses that indicate work remaining
    active_tasks = pending + queued + in_progress + assigned

    if active_tasks:
        task_ids = [t.get('id', '')[:8] for t in active_tasks[:3]]
        return False, f"{len(active_tasks)} task(s) still active: {', '.join(task_ids)}"

    if failed:
        return False, f"{len(failed)} task(s) failed"

    # Check for active agents
    agents = get_agents(workflow_id=workflow_id)
    active_agents = [a for a in agents if a.get('status') in ('working', 'starting', 'idle')]
    if active_agents:
        agent_ids = [a.get('id', '')[:8] for a in active_agents[:3]]
        return False, f"{len(active_agents)} agent(s) still active: {', '.join(agent_ids)}"

    # Check for unmerged agent branches
    try:
        project_path = os.getenv("PROJECT_PATH", "/Users/hmuhlestein/code/sotto")
        result = subprocess.run(
            ["git", "branch", "--list", "agent-*"],
            capture_output=True, text=True, timeout=10,
            cwd=project_path
        )
        if result.returncode == 0:
            branches = [b.strip().lstrip('* ') for b in result.stdout.strip().split('\n') if b.strip()]
            if branches:
                return False, f"{len(branches)} unmerged agent branch(es)"
    except Exception:
        pass

    # Check if all phases are done (for autopilot: 10 phases = 10 tasks)
    if len(done) < 10:
        return False, f"Only {len(done)}/10 phases done"

    return True, "All phases done, branches merged"


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
    failed = get_tasks(status='failed', workflow_id=workflow_id)
    for task in failed:
        task_id = task.get('id')
        phase_id = task.get('phase_id')
        task.get('enriched_description') or task.get('raw_description') or ''

        # Only retry if not retried too many times
        retry_count = task.get('retry_count', 0)
        if retry_count >= 2:
            logger.info(f"  Task {task_id[:8]} failed {retry_count} times - skipping retry")
            continue

        logger.info(f"  Retrying failed task {task_id[:8]} (retry #{retry_count + 1})")
        try:
            # Reset task status to pending
            api_post(f"/api/tasks/{task_id}/status", {"status": "pending"})
            # Create agent for it
            agent_data = api_post("/api/create_agent_for_task", {
                "task_id": task_id,
                "workflow_id": workflow_id,
                "phase_id": phase_id,
            })
            agent_id = agent_data.get('agent_id', 'unknown')
            logger.info(f"  Created agent {agent_id[:8]} for retried task")
            recovered.append(f"retried task {task_id[:8]}")
        except Exception as e:
            logger.error(f"  Failed to retry task {task_id[:8]}: {e}")

    # 2. Clean stale merge state if repo is dirty (do NOT merge branches here —
    #    the WorktreeManager handles merges in update_task_status. Raw git merge
    #    corrupts the repo because attempt_recovery runs from the orchestrator's
    #    thread, not the agent's worktree context.)
    try:
        project_path = os.getenv("PROJECT_PATH", "/Users/hmuhlestein/code/sotto")
        # Check if repo needs cleanup
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10, cwd=project_path
        )
        is_dirty = bool(status_result.stdout.strip())
        merge_in_progress = Path(project_path, ".git", "MERGE_HEAD").exists()

        if is_dirty or merge_in_progress:
            # Abort any in-progress merge that's blocking the repo
            subprocess.run(["git", "merge", "--abort"], capture_output=True, timeout=10, cwd=project_path)
            # Ensure we're on main
            subprocess.run(["git", "checkout", "main"], capture_output=True, timeout=10, cwd=project_path)
            # Clean untracked files that accumulate from failed merges
            subprocess.run(["git", "clean", "-fd"], capture_output=True, timeout=10, cwd=project_path)
            # Reset any staged but uncommitted changes
            subprocess.run(["git", "reset", "--hard", "HEAD"], capture_output=True, timeout=10, cwd=project_path)
            recovered.append("cleaned repo state")
    except Exception as e:
        logger.warning(f"  Failed to clean repo state: {e}")

    # 3. Terminate stale agents
    agents = get_agents(workflow_id=workflow_id)
    active_agents = [a for a in agents if a.get('status') in ('working', 'starting', 'idle')]
    for agent in active_agents:
        aid = agent.get('id', '')
        logger.info(f"  Terminating stale agent {aid[:8]}")
        try:
            api_post(f"/api/agents/{aid}/terminate")
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
        "credit", "quota", "billing", "payment",
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
                    return True, f"API credit issue in agent {agent.get('id', '')[:8]}: {keyword}"

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
    crashed_agents = [
        a for a in agents
        if a.get("status") == "error"
    ]
    if crashed_agents:
        names = [a.get("id", "unknown")[:20] for a in crashed_agents[:3]]
        return True, f"Crashed agents: {', '.join(names)}"

    critical_failures = [
        t for t in failed_tasks
        if t.get("priority") == "critical" or "architectural" in (t.get("description", "") or "").lower()
    ]
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
    # But give a 300 second grace period for agents to start (monitor needs
    # time to detect phase completion, evaluate, create task, then spawn agent)
    if not active_agents and pending_tasks and elapsed_seconds > 300:
        return True, f"No active agents but {len(pending_tasks)} tasks pending"

    # Check for agents that have been working too long without progress
    # (assigned tasks that never move to done)
    if in_progress_tasks and not pending_tasks:
        # Tasks are in progress - check if they've been stuck
        for task in in_progress_tasks:
            started = task.get('started_at')
            if started:
                from datetime import datetime, timezone
                try:
                    started_dt = datetime.fromisoformat(started)
                    if started_dt.tzinfo is None:
                        started_dt = started_dt.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - started_dt).total_seconds()
                    if elapsed > 1800:  # 30 minutes
                        return True, f"Task {task.get('id', '?')[:8]} stuck for {int(elapsed)}s"
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
    payload = json.dumps({
        "id": request_id,
        "reason": reason,
        "timestamp": datetime.now().isoformat(),
        "options": ["c", "s", "q"],
        "labels": {"c": "Continue", "s": "Skip design", "q": "Quit pipeline"},
        "timeout_seconds": timeout,
    }, indent=2)
    tmp = request_file.with_suffix(".tmp")
    tmp.write_text(payload)
    os.rename(tmp, request_file)

    logger.event("human_input_required", {"reason": reason, "request_id": request_id})

    print("\n" + "=" * 60)
    print("HUMAN INTERVENTION REQUIRED")
    print("=" * 60)
    print(f"Reason: {reason}")
    print(f"Request ID: {request_id}")
    print(f"Options: [c] Continue  [s] Skip  [q] Quit  (auto-continue in {timeout}s)")
    print("Respond via web UI or terminal.")
    print("=" * 60)

    start = time.time()
    while time.time() - start < timeout:
        # Check if request file was dismissed (deleted by API)
        if not request_file.exists():
            logger.info("Input request was dismissed (auto-continuing)", "WARN")
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
                    logger.event("human_input", {"choice": "m", "message": message, "reason": reason, "source": "web", "request_id": request_id})
                    response_file.unlink(missing_ok=True)  # Delete response, keep waiting
                    continue

                if choice in ("c", "s", "q"):
                    logger.event("human_input", {"choice": choice, "reason": reason, "source": "web", "request_id": request_id})
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
                        logger.event("human_input", {"choice": choice, "reason": reason, "source": "terminal", "request_id": request_id})
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
                continue
            name = filepath.stem.replace("_", " ").replace("-", " ").title()
            designs.append(DesignEntry(
                path=filepath,
                name=name,
                content_hash=content_hash,
            ))

    # Check for manual reorder file
    order_file = queue_dir / ".queue_order.json"
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


def pick_next_design(queue_dir: Path, processed_hashes: Set[str], logger: OrchestratorLogger) -> Optional[DesignEntry]:
    """Pick the next design to process.

    Reads from DB (autopilot_designs) if available, falls back to file scan.
    """
    # Try DB-based queue first
    try:
        from src.core.database import AutopilotDesign, AutopilotProject, get_db
        with get_db() as db:
            # Find active project
            project = db.query(AutopilotProject).filter_by(is_active=True).first()
            if project:
                # Get next pending design ordered by ordinal
                design = (
                    db.query(AutopilotDesign)
                    .filter_by(project_id=project.id, status="pending")
                    .order_by(AutopilotDesign.ordinal, AutopilotDesign.filename)
                    .first()
                )
                if design:
                    # Mark as processing
                    design.status = "processing"
                    db.commit()

                    # Construct DesignEntry from DB record
                    design_path = Path(project.base_dir) / "docs" / "design-queue" / design.filename
                    if design_path.exists():
                        entry = DesignEntry(
                            path=design_path,
                            name=design.name,
                            content_hash=design.content_hash or file_hash(design_path),
                            db_id=design.id,
                        )
                        logger.info(f"Selected from DB: {design.name} (ordinal={design.ordinal})")
                        return entry
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
    return next_design


def create_feature_folder(project_path: Path, design_name: str, logger: OrchestratorLogger) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = design_name.lower().replace(" ", "_")[:40]
    # Features go in .hephaestus/features/ to keep project root clean
    feature_folder = project_path / ".hephaestus" / "features" / f"{timestamp}_{safe_name}"
    feature_folder.mkdir(parents=True, exist_ok=True)
    (feature_folder / "docs").mkdir(exist_ok=True)

    # Note: .hephaestus/ is excluded from git via .git/info/exclude
    # (managed by WorktreeManager). We do NOT modify the user's .gitignore.

    logger.info(f"Feature folder: {feature_folder}")
    return feature_folder


def copy_design_document(design_entry: DesignEntry, feature_folder: Path) -> Path:
    dest = feature_folder / "docs" / design_entry.path.name
    shutil.copy2(design_entry.path, dest)
    return dest


# ── Stray-file sweep ────────────────────────────────────────────────
# Agents may accidentally write docs, reports, scripts, or diagnostic
# files to the project root instead of the feature docs dir.  This
# function copies (not moves - the iteration loop may still need them
# in the root) every stray artifact into docs_dir so that nothing is
# lost and the project root stays clean.

_DOC_EXTENSIONS = {".md", ".json", ".txt", ".log", ".csv", ".html"}
_SKIP_ROOT_FILES = {
    "README.md", "AGENTS.md", "CHANGELOG.md", "LICENSE",
    "package.json", "tsconfig.json", "pyproject.toml", "poetry.lock",
    "requirements.txt", "setup.py", "setup.cfg",
}
_STRAY_DIRS = {"evidence", "plans", "scripts"}


def _sweep_stray_files(
    project_path: Path,
    feature_folder: Path,
    docs_dir: Path,
    logger: OrchestratorLogger,
) -> None:
    """Move stray docs/reports/scripts from project root into feature docs."""
    docs_dir.mkdir(parents=True, exist_ok=True)

    # ── reports agents wrote to ./docs/ (merged from worktrees) ────
    # These stay committed in the project repo; copy them into the feature
    # bundle so the HTML report / forensics can read them in one place.
    proj_docs = project_path / _REPORT_SUBDIR
    if proj_docs.is_dir() and proj_docs.resolve() != docs_dir.resolve():
        for f in proj_docs.iterdir():
            if f.is_file() and f.suffix in _DOC_EXTENSIONS:
                dest = docs_dir / f.name
                if not dest.exists():
                    shutil.copy2(str(f), str(dest))
                    logger.info(f"Copied report: docs/{f.name} -> features/.../docs/")

    # ── files in project root ──────────────────────────────────────
    for f in project_path.iterdir():
        if not f.is_file():
            continue
        if f.name in _SKIP_ROOT_FILES:
            continue
        if f.suffix not in _DOC_EXTENSIONS:
            continue
        dest = docs_dir / f.name
        if not dest.exists():
            shutil.move(str(f), str(dest))
            logger.info(f"Moved root file: {f.name} -> features/.../docs/")

    # ── files in feature_folder root (above docs/) ─────────────────
    for f in feature_folder.iterdir():
        if not f.is_file():
            continue
        if f.suffix not in _DOC_EXTENSIONS:
            continue
        dest = docs_dir / f.name
        if not dest.exists():
            shutil.move(str(f), str(dest))
            logger.info(f"Moved feature file: {f.name} -> features/.../docs/")

    # ── stray directories in project root ──────────────────────────
    for d_name in _STRAY_DIRS:
        src_dir = project_path / d_name
        if src_dir.is_dir():
            dest_dir = docs_dir / d_name
            if not dest_dir.exists():
                shutil.move(str(src_dir), str(dest_dir))
                logger.info(f"Moved stray dir: {d_name}/ -> features/.../docs/")
            else:
                # merge contents then remove source
                for item in src_dir.rglob("*"):
                    if item.is_file():
                        rel = item.relative_to(src_dir)
                        target = dest_dir / rel
                        if not target.exists():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(item), str(target))
                            logger.info(f"Moved file from {d_name}/: {rel} -> features/.../docs/{d_name}/")
                shutil.rmtree(src_dir)

    # ── stray directories in feature_folder root ───────────────────
    for d_name in _STRAY_DIRS:
        src_dir = feature_folder / d_name
        if src_dir.is_dir() and src_dir != docs_dir:
            dest_dir = docs_dir / d_name
            if not dest_dir.exists():
                shutil.move(str(src_dir), str(dest_dir))
                logger.info(f"Moved feature dir: {d_name}/ -> features/.../docs/")
            else:
                for item in src_dir.rglob("*"):
                    if item.is_file():
                        rel = item.relative_to(src_dir)
                        target = dest_dir / rel
                        if not target.exists():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(item), str(target))
                            logger.info(f"Moved file from {d_name}/: {rel} -> features/.../docs/{d_name}/")
                shutil.rmtree(src_dir)


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
        for pattern in ["**/*.py", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.html", "**/*.css", "**/*.md"]:
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
        "generated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
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
            meets_spec = qa_passed and ("PASS" in existing or "pass" in existing.lower())
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
                config["max_total_gotos"] = max_gotos
                defn.orchestrator_config = config
                db.commit()
                if old_val != max_gotos:
                    logger.info(f"Updated max_total_gotos: {old_val} -> {max_gotos}")
    except Exception as e:
        logger.warning(f"Failed to update max_total_gotos: {e}")


def run_single_workflow(sdk, workflow_id: str, project_path: str, description: str,
                        logger: OrchestratorLogger,
                        launch_params: Dict[str, Any] = None,
                        state: PipelineState = None,
                        max_iterations: int = 10,
                        design_id: Optional[str] = None) -> str:
    """Run a single workflow execution.

    Args:
        max_iterations: Maps to the engine's max_total_gotos. Updates the workflow
            definition's orchestrator_config before launching.
    """
    # Update the workflow definition's orchestrator_config with the requested max_iterations.
    # This makes --max-iterations control the engine's max_total_gotos.
    _update_orchestrator_max_gotos(max_iterations, logger)

    # Check for existing active workflows and stop them
    existing_workflows = get_active_workflows()
    if existing_workflows:
        logger.info(f"Found {len(existing_workflows)} active workflow(s) - stopping them...")
        for wf in existing_workflows:
            wf_id = wf.get('id', '')
            try:
                # Terminate agents for this workflow
                agents = get_agents(workflow_id=wf_id)
                for agent in agents:
                    if agent.get('status') in ACTIVE_AGENT_STATUSES:
                        try:
                            api_post(f"/api/agents/{agent['id']}/terminate")
                            logger.info(f"  Terminated agent {agent['id'][:8]} for workflow {wf_id[:8]}")
                        except Exception:
                            pass
                # Mark workflow as paused
                api_post(f"/api/workflow-executions/{wf_id}/pause")
                logger.info(f"  Paused workflow {wf_id[:8]}")
            except Exception as e:
                logger.warning(f"  Failed to stop workflow {wf_id[:8]}: {e}")

    logger.info(f"Launching workflow: {workflow_id} (max_iterations={max_iterations})")
    # Extract design document from launch_params for the event
    design_doc = (launch_params or {}).get("design_document", "")
    design_name = Path(design_doc).stem.replace("_", " ").replace("-", " ") if design_doc else ""
    logger.event("workflow_launch", {
        "workflow": workflow_id,
        "path": project_path,
        "design": design_name or design_doc,
    })

    # Create a shared worktree for this design (all phases commit here)
    design_worktree_path = None
    design_branch_name = None
    try:
        from src.core.simple_config import get_config
        from src.core.worktree_manager import WorktreeManager
        from src.core.database import DatabaseManager as DbManager

        cfg = get_config()
        db = DbManager(cfg)
        wt_mgr = WorktreeManager(db_manager=db)
        # Reload to point at the actual project repo (not config.main_repo_path)
        wt_mgr.reload(Path(project_path))

        # Create feature branch from main
        import git as _git
        # Use design_entry name if available, otherwise derive from design_doc
        _design_label = design_name.replace(' ', '-').lower() if design_name else 'design'
        feature_branch = f"feature/{_design_label}"
        # Ensure branch name is unique (append short hash if needed)
        try:
            wt_mgr.main_repo.git.branch(feature_branch)
        except _git.exc.GitCommandError:
            # Branch exists — use it (idempotent)
            pass

        # Create worktree for the feature branch
        # Use flattened name for worktree path (branch name has / which creates subdirs)
        safe_branch = feature_branch.replace('/', '-')
        wt_path = wt_mgr.worktree_base / f"wt_{safe_branch}"
        if not wt_path.exists():
            wt_mgr.main_repo.git.worktree("add", str(wt_path), feature_branch)
        design_worktree_path = str(wt_path)
        design_branch_name = feature_branch
        logger.info(f"Created shared worktree: {design_worktree_path} (branch: {feature_branch})")
    except Exception as e:
        logger.warning(f"Failed to create shared worktree, using project path: {e}")
        design_worktree_path = project_path

    try:
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
    except Exception as e:
        logger.error(f"Failed to launch workflow {workflow_id}: {e}")
        return "failed"

    stuck_count = 0
    credit_stuck_count = 0
    start_time = time.time()

    try:
        while True:
            time.sleep(POLL_INTERVAL)

            # Check if in-process service requested a stop
            if _should_stop():
                logger.info("Stop requested during workflow execution")
                return "interrupted"

            # Timeout check
            elapsed = int(time.time() - start_time)
            if elapsed > MAX_WORKFLOW_TIME:
                logger.error(f"Workflow timed out after {MAX_WORKFLOW_TIME}s")
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

            logger.info(
                f"[{workflow_id}] [{elapsed}s] Agents: {len(active_agents)} active | "
                f"Tasks: {len(pending)} pending, {len(in_progress)} active, "
                f"{len(done)} done, {len(failed)} failed"
            )

            # Agent scheduling is handled by the server's background_queue_processor.
            # Stuck-agent detection is handled by Guardian/Conductor.
            # The orchestrator only monitors and logs.

            # Parent peeks at children's output periodically for observability
            if elapsed > 0 and elapsed % PARENT_PEEK_INTERVAL < POLL_INTERVAL:
                for agent in active_agents:
                    aid = agent.get('id', '')
                    output = peek_agent_output(aid, lines=15)
                    if output:
                        # Show last meaningful lines (skip blank)
                        lines = [l.strip() for l in output.strip().split('\n') if l.strip()][-8:]
                        if lines:
                            preview = ' | '.join(lines[-3:])  # last 3 lines
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
                        from src.core.database import PhaseExecution, Phase
                        from src.core.database import DatabaseManager
                        _db = DatabaseManager()
                        _session = _db.get_session()
                        try:
                            pending_phases = _session.query(PhaseExecution).filter(
                                PhaseExecution.workflow_execution_id == exec_id,
                                PhaseExecution.status.in_(["pending", "in_progress"])
                            ).count()
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
                        design_branch = getattr(state, '_design_branch', None)
                        if design_branch:
                            from src.core.simple_config import get_config
                            from src.core.worktree_manager import WorktreeManager
                            from src.core.database import DatabaseManager as DbManager
                            cfg = get_config()
                            db = DbManager(cfg)
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
                                result = wt_mgr.main_repo.git.merge(
                                    design_branch, no_ff=True,
                                    m=f"Merge design branch {design_branch} into main"
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

                            # Clean up worktree
                            try:
                                safe_branch = design_branch.replace('/', '-')
                                wt_mgr.main_repo.git.worktree("remove", str(wt_mgr.worktree_base / f"wt_{safe_branch}"), "--force")
                            except Exception:
                                pass
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

            hard_error, error_reason = detect_hard_error(agents, failed, workflow_id=exec_id)
            if hard_error:
                logger.error(f"Hard error detected: {error_reason}")
                return "hard_error"

            impasse, impasse_reason = detect_impasse(agents, pending, in_progress, elapsed)
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
                                    api_post(f"/api/agents/{a['id']}/terminate")
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
        # Clean up: mark workflow as paused (not completed) so it can be resumed
        # Only clean up if we have an exec_id and the workflow is still active
        if exec_id:
            try:
                wf_status = get_workflow_status(exec_id)
                if wf_status.get('status') == 'active':
                    api_post(f"/api/workflow-executions/{exec_id}/pause")
                    logger.info(f"Paused workflow {exec_id[:8]}")
            except Exception as e:
                logger.warning(f"Workflow cleanup failed: {e}")


def run_single_design(
    sdk,
    design_entry: DesignEntry,
    project_path: Path,
    logger: OrchestratorLogger,
    state: Optional[PipelineState] = None,
    max_iterations: int = 10,
) -> Tuple[DesignStatus, FeatureReport]:
    """Run a single design through the autopilot pipeline.

    The engine's evaluation points (goto/retry/continue) are the SOLE authority
    for iteration. This function runs the workflow once; the engine handles
    retries via `max_total_gotos` in AUTOPILOT_ORCHESTRATOR_CONFIG.

    Args:
        max_iterations: Maps to the engine's max_total_gotos. Controls how many
            times the engine can goto/retry phases before giving up. Default 10.
    """
    # project_path: where implementation code lives (the actual project)
    # docs_dir: where generated docs/reports go (inside feature folder)
    project_path.mkdir(parents=True, exist_ok=True)

    feature_folder = create_feature_folder(project_path, design_entry.name, logger)
    design_entry.project_path = project_path
    design_entry.feature_folder = feature_folder
    design_entry.started_at = datetime.now().isoformat()

    design_copy = copy_design_document(design_entry, feature_folder)

    logger.info("=" * 70)
    logger.info(f"PROCESSING DESIGN: {design_entry.name}")
    logger.info(f"  Source: {design_entry.path}")
    logger.info(f"  Project: {project_path}")
    logger.info(f"  Feature: {feature_folder}")
    logger.info("=" * 70)

    report = FeatureReport(
        design_name=design_entry.name,
        project_path=str(project_path),
        feature_folder=str(feature_folder),
        design_document=str(design_entry.path),
        iterations=0,
        total_time_seconds=0,
        qa_passed=False,
        product_validated=False,
        stop_reason="",
    )

    design_start = time.time()
    stop_reason = StopReason.COMPLETED

    # docs_dir: where generated docs (requirements, architecture, reports) go
    docs_dir = feature_folder / "docs"
    docs_dir.mkdir(exist_ok=True)

    # Copy phase definitions BEFORE workflow so forensics agent can read them
    phases_dir = docs_dir / "phase_prompts"
    phases_dir.mkdir(exist_ok=True)
    phase_files = list((HEPHAESTUS_DIR / "src" / "autopilot").glob("phase_*.py"))
    for pf in sorted(phase_files):
        shutil.copy2(pf, phases_dir / pf.name)
    logger.info(f"Copied {len(phase_files)} phase prompts to {phases_dir}")

    # Write initial pipeline_metrics.json BEFORE workflow so forensics agent can read it
    # (will be updated with final values after workflow completes)
    initial_metrics = {
        "design_name": design_entry.name,
        "design_document": str(design_entry.path),
        "project_path": str(project_path),
        "docs_dir": str(docs_dir),
        "feature_folder": str(feature_folder),
        "started_at": design_entry.started_at,
        "control_model": "engine_evaluation_points",
        "max_iterations": max_iterations,
        "max_gotos": max_iterations,  # max_iterations maps to engine's max_total_gotos
        "phases": [
            {"id": 1, "name": "product_requirements", "output": "requirements_analysis.md"},
            {"id": 2, "name": "architecture_design", "output": "architecture.md"},
            {"id": 3, "name": "development", "output": "source code in project path"},
            {"id": 4, "name": "adversarial_review", "output": "review_report.md"},
            {"id": 5, "name": "doc_review", "output": "doc_review_report.md"},
            {"id": 6, "name": "security_review", "output": "security_report.md"},
            {"id": 7, "name": "qa_validation", "output": "qa_report.md"},
            {"id": 8, "name": "product_validation", "output": "product_validation.md"},
            {"id": 9, "name": "forensics_analysis", "output": "forensics_report.md"},
            {"id": 10, "name": "git_commit_push", "output": "git history"},
        ],
    }
    metrics_path = docs_dir / "pipeline_metrics.json"
    metrics_path.write_text(json.dumps(initial_metrics, indent=2, default=str))
    logger.info(f"Initial pipeline metrics: {metrics_path}")

    try:
        # Single workflow run — the engine's evaluation points handle all iteration.
        # If product_validation scores < 0.7, the engine does goto development/architecture
        # bounded by max_total_gotos (default 10) in AUTOPILOT_ORCHESTRATOR_CONFIG.
        logger.info("")
        logger.info("-" * 60)
        logger.info(f"DESIGN: {design_entry.name} | Single run (engine-controlled iteration)")
        logger.info("-" * 60)

        description = (
            f"Autopilot: {design_entry.name}\n"
            f"Your working directory is an isolated git worktree (the project root).\n"
            f"Design Document: ./.hephaestus/design.md (copied into your worktree)\n"
            f"Project Path: . (your working directory)\n"
            f"Docs Path: ./docs/ (generated requirements, architecture, reports)\n"
            f"Implementation code (src/, tests/) goes in your working directory.\n"
            f"Inputs (design, context, qa_spec) are in ./.hephaestus/.\n"
            f"Read the design doc carefully, extract requirements, "
            f"create architecture, implement, review, security check, and QA."
        )

        # Session ID components for persistent agent sessions (§10.1.1).
        # The agent manager uses these to generate deterministic session IDs
        # so pi agents resume with full conversational context on gotos.
        _project_slug = Path(project_path).name.lower().replace(' ', '-')[:30]
        _design_slug = design_entry.name.lower().replace(' ', '-')[:30]

        # design_document is the absolute SOURCE path; the backend (AgentManager)
        # reads it and copies it into each worktree's .hephaestus/design.md. Agents
        # only ever read the worktree-relative copy.
        launch_params = {
            "design_document": str(design_copy),
            "project_path": str(project_path),
            "project_id": _project_slug,
            "design_slug": _design_slug,
            "project_context": (
                "Your working directory is the project root. Write code/tests there, "
                "generated docs in ./docs/. Read inputs from ./.hephaestus/."
            ),
        }

        wf_status = run_single_workflow(
            sdk, "autopilot", str(project_path), description, logger,
            launch_params=launch_params,
            state=state,
            max_iterations=max_iterations,
            design_id=design_entry.db_id,
        )

        report.iterations = 1  # Single run; engine handles iteration via gotos

        if wf_status == "interrupted":
            stop_reason = StopReason.USER_INTERRUPT
        elif wf_status == "hard_error":
            stop_reason = StopReason.HARD_ERROR
        elif wf_status == "skipped":
            logger.info("Design skipped by user")
            stop_reason = StopReason.USER_SKIP
        elif wf_status == "timeout":
            stop_reason = StopReason.MAX_ITERATIONS
        elif wf_status == "completed":
            # Workflow completed — read spec gate results from structured files.
            # The engine's evaluation points already drove iteration via gotos;
            # we just need to determine if the final state passed the gate.
            logger.info("")
            logger.info("Workflow completed. Reading spec gate results...")

            from src.autopilot.spec import load_spec, read_result, score_qa, score_product_validation

            spec = load_spec()

            # Check QA result
            qa_result = read_result(project_path, "qa_result.json")
            if qa_result:
                qa_score, qa_meta = score_qa(qa_result, spec)
                report.qa_passed = qa_score >= 0.7
                logger.info(f"QA gate: score={qa_score:.2f}, band={qa_meta.get('band', 'unknown')}")
            else:
                # Fallback: check for qa_report.md existence as a weak signal
                qa_report = _report_path(project_path, "qa_report.md")
                report.qa_passed = qa_report.exists()

            # Check product validation result
            pv_result = read_result(project_path, "product_validation.json")
            if pv_result:
                pv_score, pv_meta = score_product_validation(pv_result, spec)
                report.product_validated = pv_score >= 0.7
                logger.info(f"Product validation gate: score={pv_score:.2f}, band={pv_meta.get('band', 'unknown')}")
            else:
                # Fallback: check for product_validation.md existence
                pv_report = _report_path(project_path, "product_validation.md")
                report.product_validated = pv_report.exists()

            if report.product_validated:
                logger.info(f"DESIGN VALIDATED: {design_entry.name}")
                stop_reason = StopReason.COMPLETED
            else:
                # Engine exhausted gotos without passing the gate
                stop_reason = StopReason.MAX_ITERATIONS
                logger.info(f"Design did not pass validation gate after engine-controlled iterations")
        else:
            # Unknown status
            stop_reason = StopReason.HARD_ERROR
            logger.warning(f"Unexpected workflow status: {wf_status}")

    except KeyboardInterrupt:
        stop_reason = StopReason.USER_INTERRUPT
        logger.info("Design processing interrupted")

    # Organize: copy stray docs from project root into feature docs (don't move - iteration loop needs them)
    _sweep_stray_files(project_path, feature_folder, docs_dir, logger)

    # Collect summaries from the correct locations (docs in features, code in builds)
    summaries = collect_report_summaries(docs_dir)
    report.requirements_summary = summaries.get("requirements", "")
    report.architecture_summary = summaries.get("architecture", "")
    report.doc_review_summary = summaries.get("doc_review", "")
    report.security_summary = summaries.get("security", "")
    report.qa_summary = summaries.get("qa", "")
    report.product_validation_summary = summaries.get("product_validation", "")
    report.forensics_summary = summaries.get("forensics", "")
    report.files_created = collect_files_created(project_path, feature_folder)

    # Update pipeline_metrics.json with final values
    metrics = {
        "design_name": design_entry.name,
        "design_document": str(design_entry.path),
        "project_path": str(project_path),
        "docs_dir": str(docs_dir),
        "feature_folder": str(feature_folder),
        "iterations": report.iterations,
        "total_time_seconds": report.total_time_seconds,
        "stop_reason": report.stop_reason,
        "qa_passed": report.qa_passed,
        "product_validated": report.product_validated,
        "cost_total": report.cost_total,
        "files_created_count": len(report.files_created),
        "started_at": design_entry.started_at,
        "completed_at": design_entry.completed_at,
        "phases": [
            {"id": 1, "name": "product_requirements", "output": "requirements_analysis.md"},
            {"id": 2, "name": "architecture_design", "output": "architecture.md"},
            {"id": 3, "name": "development", "output": "source code in project path"},
            {"id": 4, "name": "adversarial_review", "output": "review_report.md"},
            {"id": 5, "name": "doc_review", "output": "doc_review_report.md"},
            {"id": 6, "name": "security_review", "output": "security_report.md"},
            {"id": 7, "name": "qa_validation", "output": "qa_report.md"},
            {"id": 8, "name": "product_validation", "output": "product_validation.md"},
            {"id": 9, "name": "forensics_analysis", "output": "forensics_report.md"},
            {"id": 10, "name": "git_commit_push", "output": "git history"},
        ],
    }
    metrics_path = docs_dir / "pipeline_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2, default=str))
    logger.info(f"Final pipeline metrics: {metrics_path}")

    report.total_time_seconds = int(time.time() - design_start)
    report.stop_reason = stop_reason.value

    # Fetch cost data from LiteLLM proxy if configured
    litellm_config = get_litellm_config()
    if litellm_config["cost_tracking"] and litellm_config["url"] and litellm_config["cost_api_key"]:
        try:
            from src.interfaces.cost_tracker import CostTracker
            import asyncio

            tracker = CostTracker(
                proxy_url=litellm_config["url"],
                api_key=litellm_config["cost_api_key"],
            )

            # Use the safe_name as the user identifier for cost tracking
            feature_user = design_entry.name.lower().replace(" ", "_")[:40]

            async def fetch_costs():
                cost_info = await tracker.get_feature_cost(feature_user)
                daily = await tracker.get_daily_breakdown(feature_user, days=7)
                return cost_info, daily

            cost_info, daily_breakdown = asyncio.run(fetch_costs())

            report.cost_total = cost_info.get("spend", 0)
            logger.info(f"Cost for '{feature_user}': ${report.cost_total:.4f}")

            # Extract model breakdown from daily data
            for day_entry in daily_breakdown.get("results", []):
                metrics = day_entry.get("metrics", {})
                breakdown = day_entry.get("breakdown", {})
                for model_name, model_data in breakdown.get("models", {}).items():
                    model_spend = model_data.get("spend", 0)
                    if model_name not in report.cost_breakdown:
                        report.cost_breakdown[model_name] = 0
                    report.cost_breakdown[model_name] += model_spend

        except Exception as e:
            logger.warning(f"Failed to fetch cost data: {e}")

    generate_html_feature_report(report, summaries, feature_folder, logger)

    design_entry.completed_at = datetime.now().isoformat()

    if stop_reason == StopReason.USER_SKIP:
        status = DesignStatus.SKIPPED
    elif report.product_validated:
        status = DesignStatus.COMPLETED
    else:
        status = DesignStatus.FAILED
    return status, report


def _should_stop() -> bool:
    """Check if the pipeline should stop.

    Returns True if the in-process AutopilotService has requested a stop
    (via the module-level _service_stop_event).
    """
    event = globals().get('_service_stop_event')
    if event is not None:
        try:
            # Non-blocking check
            return event.is_set()
        except Exception:
            pass
    return False


def run_continuous_pipeline(args) -> None:
    log_dir = Path.home() / ".hephaestus" / "autopilot" / datetime.now().strftime("run-%Y%m%d-%H%M%S")
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

    processed_file = log_dir / "processed.json"

    sys.path.insert(0, str(HEPHAESTUS_DIR))
    from src.sdk import HephaestusSDK
    from src.sdk.models import WorkflowDefinition
    from src.autopilot.phases import AUTOPILOT_PHASES, AUTOPILOT_WORKFLOW_CONFIG, AUTOPILOT_LAUNCH_TEMPLATE
    from src.core.simple_config import get_config

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

    logger.info("Initializing SDK...")
    sdk = HephaestusSDK(
        workflow_definitions=[autopilot_def],
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
        sdk.start(enable_tui=False, timeout=60)
    except Exception as e:
        logger.error(f"Failed to start: {e}")
        sys.exit(1)

    logger.info("Services started.")

    # Register orchestrator as an agent
    try:
        import uuid
        from src.core.database import DatabaseManager, Agent
        db_manager = DatabaseManager()
        session = db_manager.get_session()
        try:
            orchestrator_agent = Agent(
                id=f"orchestrator-{uuid.uuid4().hex[:8]}",
                system_prompt="Autopilot Orchestrator - manages the 10-phase pipeline",
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

    # Clean up stale active workflows from previous runs
    try:
        active_workflows = get_active_workflows()
        if active_workflows:
            logger.info(f"Found {len(active_workflows)} stale active workflow(s) from previous runs - cleaning up...")
            for wf in active_workflows:
                wf_id = wf.get('id', '')
                try:
                    api_post(f"/api/workflow-executions/{wf_id}/complete")
                    logger.info(f"  Cleaned up stale workflow {wf_id[:8]}")
                except Exception as e:
                    logger.warning(f"  Failed to clean up {wf_id[:8]}: {e}")
    except Exception as e:
        logger.warning(f"Warning: Could not check for stale workflows: {e}")

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
                        wf_ids = [wf.get('id', '')[:8] for wf in active_workflows]
                        logger.info(f"Workflow still active ({', '.join(wf_ids)}) - waiting before picking next design")
                        state.queue_status = {"status": "waiting", "reason": "workflow_active", "active_workflows": wf_ids}
                        logger.save_state(state)
                        persistent_state.save(state, processed_hashes)
                        time.sleep(POLL_INTERVAL)
                        continue

                    # Also check previous workflow is fully complete (all phases done, branches merged)
                    if state.current_workflow_id:
                        # First check if workflow still exists in DB
                        try:
                            wf_check = get_workflow_status(state.current_workflow_id)
                            wf_check_status = wf_check.get('status', '')
                            if not wf_check_status:
                                # Workflow no longer exists in DB — clear stale state
                                logger.info(f"Previous workflow {state.current_workflow_id[:8]} no longer exists in DB, clearing stale state")
                                state.current_workflow_id = None
                                continue
                        except Exception:
                            logger.info(f"Previous workflow {state.current_workflow_id[:8]} could not be checked, clearing stale state")
                            state.current_workflow_id = None
                            continue

                        is_complete, reason = is_design_fully_complete(state.current_workflow_id, logger)
                        if not is_complete:
                            logger.info(f"Previous workflow not yet complete: {reason}")

                            # Track recovery attempts to prevent infinite loops
                            if not hasattr(state, '_recovery_attempts'):
                                state._recovery_attempts = 0
                            state._recovery_attempts += 1

                            if state._recovery_attempts > 5:
                                logger.warning(f"Recovery failed after {state._recovery_attempts} attempts, escalating to impasse for workflow {state.current_workflow_id[:8]}")
                                # Mark workflow as failed — required phase was abandoned
                                try:
                                    from src.core.database import get_db, Workflow
                                    with get_db() as db:
                                        wf = db.query(Workflow).filter_by(id=state.current_workflow_id).first()
                                        if wf:
                                            wf.status = "failed"
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

                            state.queue_status = {"status": "waiting", "reason": reason, "recovery": recovery_msg if success else None}
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
                    logger.info(f"Queue empty. Scanning again in {DESIGN_QUEUE_SCAN_INTERVAL}s...")
                    state.queue_status = {"status": "empty", "processed": len(processed_hashes)}
                    logger.save_state(state)
                    persistent_state.save(state, processed_hashes)
                    time.sleep(DESIGN_QUEUE_SCAN_INTERVAL)
                    continue

                next_design.status = DesignStatus.IN_PROGRESS
                state.current_design = next_design.name
                state.current_feature_folder = str(next_design.feature_folder) if next_design.feature_folder else None
                state.queue_status = {
                    "status": "processing",
                    "current": next_design.name,
                    "processed": len(processed_hashes),
                }
                logger.save_state(state)
                persistent_state.save(state, processed_hashes)

                status, feature_report = run_single_design(
                    sdk, next_design, project_path, logger, state,
                    max_iterations=args.max_iterations,
                )

                next_design.status = status
                processed_hashes.add(next_design.content_hash)
                processed_file.write_text(json.dumps(list(processed_hashes)))

                # Update DB design status
                try:
                    from src.core.database import AutopilotDesign, AutopilotProject, get_db as _get_db
                    with _get_db() as _db:
                        _proj = _db.query(AutopilotProject).filter_by(is_active=True).first()
                        if _proj:
                            _des = _db.query(AutopilotDesign).filter_by(
                                project_id=_proj.id, filename=next_design.path.name
                            ).first()
                            if _des:
                                _des.status = status.value if hasattr(status, 'value') else str(status)
                                _des.feature_folder = str(next_design.feature_folder) if next_design.feature_folder else None
                                if status == DesignStatus.COMPLETED:
                                    _des.completed_at = datetime.utcnow()
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
                logger.save_state(state)
                persistent_state.save(state, processed_hashes)

                logger.event("design_complete", {
                    "design": next_design.name,
                    "status": status.value,
                    "iterations": feature_report.iterations,
                    "qa_passed": feature_report.qa_passed,
                    "product_validated": feature_report.product_validated,
                    "elapsed_seconds": feature_report.total_time_seconds,
                    "feature_folder": str(next_design.feature_folder),
                })

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
        logger.event("pipeline_stop", {
            "total_designs": state.designs_processed,
            "succeeded": state.designs_succeeded,
            "failed": state.designs_failed,
            "elapsed_seconds": state.total_elapsed,
        })

        # Pause all active autopilot workflows
        try:
            active_workflows = get_active_workflows()
            for wf in active_workflows:
                wf_id = wf.get('id', '')
                try:
                    api_post(f"/api/workflow-executions/{wf_id}/pause")
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
    parser.add_argument("--design-queue", default=None,
                        help="Directory to watch for design documents (default: <project-path>/docs/design-queue)")
    parser.add_argument("--project-path", required=True,
                        help="Project directory for implementation code")
    parser.add_argument("--max-iterations", type=int, default=3,
                        help="Maximum review-fix-QA iterations per design")
    parser.add_argument("--drop-db", action="store_true",
                        help="Drop database before starting")

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
                print(f"Another orchestrator is already running (PID: {existing_pid}). Exiting.")
                sys.exit(1)
        except (ProcessLookupError, ValueError):
            # Process not alive or invalid PID, clean up
            pid_file.unlink(missing_ok=True)

    # Default design queue to <project-path>/docs/design-queue
    if not args.design_queue:
        args.design_queue = str(Path(args.project_path) / "docs" / "design-queue")

    if args.drop_db:
        db = HEPHAESTUS_DIR / "hephaestus.db"
        if db.exists():
            db.unlink()
            print(f"Dropped {db}")

    # Ensure DB tables and migrations are applied
    from src.core.database import DatabaseManager
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
