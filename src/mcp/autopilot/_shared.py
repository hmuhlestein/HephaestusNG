"""Shared constants, cross-cutting helpers, and Pydantic models for the Autopilot API. — extracted from src/mcp/autopilot_api.py (backend_module_decomposition.md §3.2)."""

import collections
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypeVar

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.core.constants import (
    AUTOPILOT_STATE_DIR,
    CONTEXT_DIR_NAME,
    DESIGN_CONTEXT_SUBDIR,
)

# Import authentication function from server module


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/autopilot", tags=["Autopilot"])

DESIGN_QUEUE_DIR = ""

FEATURES_DIR = ""

_active_project_id_cache: Optional[str] = None  # Track which project the cached dirs belong to

_queue_dir_by_project: Dict[str, str] = {}

_features_dir_by_project: Dict[str, str] = {}

ALLOWED_EXTENSIONS = {".md", ".txt"}

def _get_active_project_id() -> Optional[str]:
    """Get the current active project ID from the database."""
    from src.core.database import AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).filter_by(is_active=True).first()
        return proj.id if proj else None

def _invalidate_project_dirs():
    """Invalidate cached project directories so they are recomputed.

    Call this whenever the active project changes.
    """
    global DESIGN_QUEUE_DIR, FEATURES_DIR, _active_project_id_cache
    DESIGN_QUEUE_DIR = ""
    FEATURES_DIR = ""
    _active_project_id_cache = None
    _queue_dir_by_project.clear()
    _features_dir_by_project.clear()
    _invalidate("queue", "features", "status")

def _get_effective_queue_dir(project_id: Optional[str] = None) -> str:
    """Get the effective design queue directory.

    The explicit DESIGN_QUEUE_DIR override (configure_autopilot_api/env
    var) always wins regardless of project_id -- it's a "pin to one fixed
    directory, ignore the DB entirely" escape hatch (tests, simple
    single-directory deployments), inherently incompatible with genuine
    multi-project use, so a caller that set it gets it back unconditionally.

    Otherwise: when project_id is given, resolves and caches THAT
    project's queue dir specifically -- does not fall back to the
    is_active-derived global, since a caller asking for a specific
    project wants that project's real directory regardless of which
    project (if any) currently occupies the global "active" slot. When
    project_id is omitted, preserves the original is_active-derived
    fallback for callers not yet updated to pass project_id.

    Raises:
        FileNotFoundError: If queue directory doesn't exist
        RuntimeError: If no active project configured
    """
    global DESIGN_QUEUE_DIR, _active_project_id_cache

    if DESIGN_QUEUE_DIR:
        if not Path(DESIGN_QUEUE_DIR).exists():
            raise FileNotFoundError(f"Design queue directory does not exist: {DESIGN_QUEUE_DIR}")
        return DESIGN_QUEUE_DIR

    if project_id:
        cached = _queue_dir_by_project.get(project_id)
        if cached:
            if not Path(cached).exists():
                raise FileNotFoundError(f"Design queue directory does not exist: {cached}")
            return cached

        from src.core.database import AutopilotProject, get_db

        with get_db() as db:
            proj = db.query(AutopilotProject).filter_by(id=project_id).first()
            if not proj or not proj.base_dir:
                raise RuntimeError(f"Project not found or has no base_dir: {project_id}")
            queue_dir = Path(proj.base_dir) / DESIGN_CONTEXT_SUBDIR
            queue_dir.mkdir(parents=True, exist_ok=True)
            _queue_dir_by_project[project_id] = str(queue_dir)
            return str(queue_dir)

    # Check if the active project has changed since we last cached
    current_project_id = _get_active_project_id()
    if current_project_id != _active_project_id_cache:
        # Project changed — invalidate cached dirs
        DESIGN_QUEUE_DIR = ""
        _active_project_id_cache = current_project_id

    # Get from active project
    from src.core.database import AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).filter_by(is_active=True).first()
        if not proj or not proj.base_dir:
            raise RuntimeError("No active project configured. Set DESIGN_QUEUE_DIR or activate a project.")

        queue_dir = Path(proj.base_dir) / DESIGN_CONTEXT_SUBDIR
        queue_dir.mkdir(parents=True, exist_ok=True)

        DESIGN_QUEUE_DIR = str(queue_dir)
        return DESIGN_QUEUE_DIR

def _get_effective_features_dir(project_id: Optional[str] = None) -> str:
    """Get the effective features directory.

    See _get_effective_queue_dir's docstring -- same override-first,
    project_id-second, global-fallback-last design.

    Raises:
        FileNotFoundError: If features directory doesn't exist
        RuntimeError: If no active project configured
    """
    global FEATURES_DIR, _active_project_id_cache

    if FEATURES_DIR:
        if not Path(FEATURES_DIR).exists():
            raise FileNotFoundError(f"Features directory does not exist: {FEATURES_DIR}")
        return FEATURES_DIR

    if project_id:
        cached = _features_dir_by_project.get(project_id)
        if cached:
            if not Path(cached).exists():
                raise FileNotFoundError(f"Features directory does not exist: {cached}")
            return cached

        from src.core.database import AutopilotProject, get_db

        with get_db() as db:
            proj = db.query(AutopilotProject).filter_by(id=project_id).first()
            if not proj or not proj.base_dir:
                raise RuntimeError(f"Project not found or has no base_dir: {project_id}")
            features_dir = Path(proj.base_dir) / CONTEXT_DIR_NAME / "features"
            if not features_dir.exists():
                raise FileNotFoundError(
                    f"Features directory does not exist: {features_dir}. Run the autopilot pipeline first."
                )
            _features_dir_by_project[project_id] = str(features_dir)
            return str(features_dir)

    # Check if the active project has changed since we last cached
    current_project_id = _get_active_project_id()
    if current_project_id != _active_project_id_cache:
        # Project changed — invalidate cached dirs
        FEATURES_DIR = ""
        _active_project_id_cache = current_project_id

    # Get from active project
    from src.core.database import AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).filter_by(is_active=True).first()
        if not proj or not proj.base_dir:
            raise RuntimeError("No active project configured. Set FEATURES_DIR or activate a project.")

        features_dir = Path(proj.base_dir) / CONTEXT_DIR_NAME / "features"
        if not features_dir.exists():
            raise FileNotFoundError(f"Features directory does not exist: {features_dir}. Run the autopilot pipeline first.")

        FEATURES_DIR = str(features_dir)
        return FEATURES_DIR

T = TypeVar("T")

_cache: Dict[str, Tuple[Any, float]] = {}

CACHE_TTL = 10.0

def _cached(key: str, ttl: float = CACHE_TTL) -> Optional[Any]:
    entry = _cache.get(key)
    if entry is None:
        return None
    data, ts = entry
    if time.monotonic() - ts >= ttl:
        return None
    return data

def _store(key: str, data: Any) -> Any:
    _cache[key] = (data, time.monotonic())
    return data

def _invalidate(*keys: str):
    for k in keys:
        _cache.pop(k, None)

def _safe_path(base: str, *parts: str) -> Path:
    """Validate that the resulting path is within the base directory.

    Security: Uses resolved (realpath) path to prevent symlink traversal attacks.
    Requires exact match or trailing separator to prevent prefix-based traversal
    (e.g., /app/design-evil passing when base is /app/design).
    """
    if not base:
        raise HTTPException(500, "Directory not configured")
    base_resolved = Path(base).resolve()
    resolved = (Path(base) / Path(*parts)).resolve()
    if not (resolved == base_resolved or str(resolved).startswith(str(base_resolved) + os.sep)):
        raise HTTPException(400, "Invalid path")
    return resolved

def _feature_status(metrics: dict) -> str:
    if metrics.get("product_validated"):
        return "validated"
    if metrics.get("stop_reason") in ("hard_error", "impasse", "architectural_issue"):
        return "failed"
    return "needs_review"

def _extract_pr_url(db, workflow_id: str, phase_map: dict) -> Optional[str]:
    """Extract PR URL from the git_commit_push task's key_learnings."""
    import re
    from src.core.database import Memory, Task, Phase
    if not workflow_id:
        return None
    # Find the git_commit_push phase
    git_phase = db.query(Phase).filter_by(
        workflow_id=workflow_id, name="git_commit_push"
    ).first()
    if not git_phase:
        return None
    # Find the completed task for that phase
    git_task = db.query(Task).filter_by(
        phase_id=git_phase.id, status="done"
    ).first()
    if not git_task:
        return None
    # Look for PR URL in key_learnings (stored as memories)
    memories = db.query(Memory).filter_by(
        related_task_id=git_task.id, memory_type="learning"
    ).all()
    pr_pattern = re.compile(r"https://github\.com/[^\s]+/pull/\d+")
    for mem in memories:
        match = pr_pattern.search(mem.content or "")
        if match:
            return match.group(0)
    # Also check completion_notes
    match = pr_pattern.search(git_task.completion_notes or "")
    if match:
        return match.group(0)
    return None

def _get_latest_run_dir() -> Optional[Path]:
    base = Path(AUTOPILOT_STATE_DIR)
    if not base.exists():
        return None
    runs = sorted(base.glob("run-*"), reverse=True)
    return runs[0] if runs else None

def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None

def _read_jsonl_tail(path: Path, limit: int = 100) -> List[dict]:
    if not path.exists():
        return []
    try:
        with open(path) as f:
            raw_lines = collections.deque(f, maxlen=limit)
        entries = []
        for line in raw_lines:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries
    except Exception:
        return []

class DesignQueueItem(BaseModel):
    filename: str
    name: str
    size_bytes: int
    modified: str
    extension: str

class DesignQueueAdd(BaseModel):
    name: str
    content: str
    extension: str = ".md"
    project_id: Optional[str] = None

class FeatureSummary(BaseModel):
    id: str
    name: str
    status: str
    iterations: int
    total_time_seconds: int
    stop_reason: str
    cost_total: float
    cost_currency: str
    created_at: str
    has_report: bool

class FeatureDetail(BaseModel):
    id: str
    name: str
    status: str
    iterations: int
    total_time_seconds: int
    stop_reason: str
    qa_passed: bool
    product_validated: bool
    has_report: bool
    design_name: str
    project_path: str
    feature_folder: str
    requirements_summary: str
    architecture_summary: str
    security_summary: str
    qa_summary: str
    product_validation_summary: str
    forensics_summary: str
    files_created: List[str]
    issues_resolved: List[str]
    outstanding_issues: List[str]
    cost_total: float
    cost_breakdown: Dict[str, float]
    cost_currency: str
    created_at: str
    docs: List[Dict[str, Any]]

class PipelineStatus(BaseModel):
    running: bool
    current_design: Optional[str] = None
    current_workflow_id: Optional[str] = None
    designs_processed: int = 0
    designs_succeeded: int = 0
    designs_failed: int = 0
    total_elapsed: int = 0
    queue_depth: int = 0
    last_event: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None
    active_agents: int = 0
    # Which project the (globally single) AutopilotService is actually
    # running, if any -- lets the UI say "X is running" instead of a vague
    # "another project pipeline is running" that doesn't even distinguish
    # a genuine cross-project conflict from the caller's own just-started run.
    # Kept for backward compat (mirrors running_projects[0] when >=1 is
    # running) -- new callers should use running_projects, which is the
    # only field that can actually represent more than one concurrently
    # running project.
    running_project_path: Optional[str] = None
    running_project_name: Optional[str] = None
    # True when the running project matches the requested project (after
    # realpath resolution, so /tmp == /private/tmp on macOS).
    is_self_conflict: bool = False
    # Every currently-running project (0 to max_concurrent_projects), for
    # the global (no project_id) status check. Populated so a caller
    # hitting the concurrency cap can identify and stop EXACTLY the
    # project(s) blocking it instead of resorting to a bare stop-all call.
    running_projects: List[Dict[str, Any]] = Field(default_factory=list)
    # Review mode
    review_mode: bool = False
    features_awaiting_review: int = 0

class MessageItem(BaseModel):
    timestamp: str
    type: str
    data: Dict[str, Any]

def configure_autopilot_api(
    design_queue_dir: str = "",
    features_dir: str = "",
):
    global DESIGN_QUEUE_DIR, FEATURES_DIR, _active_project_id_cache
    DESIGN_QUEUE_DIR = design_queue_dir or os.getenv("DESIGN_QUEUE_DIR", "")
    FEATURES_DIR = features_dir or os.getenv("FEATURES_DIR", "")
    _active_project_id_cache = None  # Reset so next request rechecks active project
    _invalidate("queue", "features", "status")
    logger.info(f"Autopilot API configured: queue={DESIGN_QUEUE_DIR}, features={FEATURES_DIR}")
