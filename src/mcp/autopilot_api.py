"""API endpoints for the Autopilot dashboard."""

import asyncio
import collections
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypeVar

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field, field_validator, model_validator, validator
from sqlalchemy import func as sqlfunc

from src.core.constants import (
    AUTOPILOT_STATE_DIR,
    CONTEXT_DIR_NAME,
    DESIGN_CONTEXT_SUBDIR,
    DESIGN_WORKFLOW_DEFINITION_IDS,
    GOTO_REASON_PREFIX,
    PHASE0_DEFINITION_IDS,
)

# Import authentication function from server module
from src.mcp.server import (
    KNOWN_SYSTEM_AGENTS,
    _check_rate_limit,
    verify_agent_authentication,
)
from src.prompts.loader import get_prompt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/autopilot", tags=["Autopilot"])

DESIGN_QUEUE_DIR = ""
FEATURES_DIR = ""
_active_project_id_cache: Optional[str] = None  # Track which project the cached dirs belong to

# Per-project resolved dirs, keyed by project_id -- required once more than
# one project can be active at once (max_concurrent_projects): the single
# DESIGN_QUEUE_DIR/FEATURES_DIR globals above silently resolved EVERY
# caller against whichever ONE project happened to be picked by
# is_active's .first(), regardless of which project the request was
# actually for. Callers that pass project_id explicitly get resolved from
# here instead; callers that don't (not yet updated, or genuinely
# project-agnostic) keep using the DESIGN_QUEUE_DIR/FEATURES_DIR globals
# above unchanged, including the configure_autopilot_api/env var override.
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


# ── TTL cache ────────────────────────────────────────────────────

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


# ── Path safety ──────────────────────────────────────────────────


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


# ── File I/O ─────────────────────────────────────────────────────


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


# ── Pydantic models ──────────────────────────────────────────────


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


# ── Pipeline Status ───────────────────────────────────────────────


@router.get("/status", response_model=PipelineStatus)
async def get_pipeline_status(
    project_id: Optional[str] = None,
    project_path: Optional[str] = None,
):
    from src.autopilot.service import get_autopilot_service, get_registry

    # project_path must be part of the key too, not just project_id: the
    # self-conflict check calls this with project_id=None (global status)
    # but a real project_path, and is_self_conflict depends on it -- without
    # this, two different projects' self-conflict checks within the 2s TTL
    # could get each other's cached result (both fall into the same "status"
    # bucket since project_id is None for both).
    cache_key = f"status:{project_id}:{project_path}" if (project_id or project_path) else "status"
    cached = _cached(cache_key, ttl=2.0)
    if cached is not None:
        return cached

    # AutopilotService is now per-project (see get_registry) -- there's no
    # longer a single global service to ask when project_id isn't given, so
    # the "any project running" fallback used below relies on the DB check,
    # not service_status. When project_id IS given, ask that project's own
    # service directly instead of the DB-workaround this endpoint used
    # before per-project services existed (kept below as a belt-and-
    # suspenders check, not the primary source of truth anymore).
    running_projects_list: List[Dict[str, Any]] = []
    if project_id:
        service_status = get_autopilot_service(project_id).status()
    else:
        # current_design/elapsed_seconds/error still only reflect one
        # project (the first running one) -- those genuinely have no
        # multi-project representation in this response shape. But
        # running_projects (below) reports EVERY running project, not just
        # the first, specifically so a caller hitting the concurrency cap
        # can identify and stop exactly the project(s) blocking it instead
        # of resorting to a bare stop-all call.
        running_services = get_registry().running()
        if running_services:
            service_status = dict(running_services[0].status())
            for extra in running_services[1:]:
                extra_status = extra.status()
                for key in ("designs_processed", "designs_succeeded", "designs_failed"):
                    service_status[key] = service_status.get(key, 0) + extra_status.get(key, 0)

            for svc in running_services:
                svc_path = svc.status().get("project_path")
                svc_name = None
                if svc_path:
                    try:
                        from src.core.database import AutopilotProject
                        from src.core.database import get_db as _get_db

                        with _get_db() as _db:
                            _rp = _db.query(AutopilotProject).filter_by(base_dir=svc_path).first()
                            svc_name = _rp.name if _rp else Path(svc_path).name
                    except Exception:
                        svc_name = Path(svc_path).name
                running_projects_list.append(
                    {"id": getattr(svc, "project_id", None), "name": svc_name, "base_dir": svc_path}
                )
        else:
            service_status = {}

    run_dir = _get_latest_run_dir()
    running = service_status.get("running", False)

    # When project_id is provided, also check if THIS project has an active
    # workflow OR an active agent -- a belt-and-suspenders promotion for
    # when the service object itself missed something (e.g. it crashed but
    # an agent it spawned is still working). This must only ever promote
    # False -> True, never demote a True from service_status: the pipeline
    # loop is legitimately "running" (alive, watching the queue) between
    # designs or while idling on an empty queue, with zero active
    # workflows/agents at that instant -- demoting to False here used to
    # flip the Play button straight back to "Paused" during any such lull,
    # even though get_autopilot_service(project_id) is already correctly
    # scoped per-project (unlike when this check was first written).
    if project_id and not running:
        try:
            from src.core.database import Agent, Task, Workflow, get_db

            with get_db() as db:
                has_active = db.query(Workflow).filter(Workflow.project_id == project_id, Workflow.status.in_(["active", "running"])).first()
                if has_active:
                    running = True
                else:
                    # Also check: are any agents working on tasks in this
                    # project's workflows? A workflow can be "failed" while
                    # an agent is still actively working on it.
                    project_wf_ids = [w.id for w in db.query(Workflow).filter(Workflow.project_id == project_id).all()]
                    if project_wf_ids:
                        active_agent = (
                            db.query(Agent).join(Task, Agent.current_task_id == Task.id).filter(Task.workflow_id.in_(project_wf_ids), Agent.status.in_(["working", "starting", "idle"])).first()
                        )
                        running = active_agent is not None
        except Exception:
            pass
    elif not running:
        # No project_id specified, fallback to checking any active workflow
        try:
            from src.core.database import Agent, Workflow, get_db

            with get_db() as db:
                active_wf = db.query(Workflow).filter(Workflow.status.in_(["active", "paused"])).first()
                if active_wf:
                    active_agents = (
                        db.query(Agent)
                        .filter(
                            Agent.agent_type == "phase",
                            Agent.status.in_(["working", "idle", "starting"]),
                        )
                        .count()
                    )
                    if active_agents > 0:
                        running = True
        except Exception:
            pass

    state = _cached("state", ttl=2.0)
    if state is None:
        # Try run-specific state first, then persistent state
        if run_dir:
            state = _read_json(run_dir / "state.json") or {}

        # Fall back to persistent state if run-specific state is empty
        if not state:
            try:
                from src.autopilot.orchestrator import PersistentPipelineState

                state_obj, _processed = PersistentPipelineState(project_id=project_id).load()
                state = state_obj.to_dict()
            except Exception:
                state = {}

        # No run dir AND no persistent state file: state was never assigned
        # above and stays None, crashing every state.get(...) call below.
        state = _store("state", state or {})

    # Count queue depth from DB when project_id is provided (consistent with
    # the queue panel which reads from the DB). Fall back to filesystem count.
    queue_depth = 0
    if project_id:
        from src.core.database import AutopilotDesign, get_db

        try:
            with get_db() as db:
                queue_depth = db.query(AutopilotDesign).filter(AutopilotDesign.project_id == project_id, AutopilotDesign.status.notin_(["completed", "failed", "skipped"])).count()
        except Exception:
            pass
    else:
        try:
            effective_dir = _get_effective_queue_dir()
            for ext in ALLOWED_EXTENSIONS:
                queue_depth += len(list(Path(effective_dir).glob(f"*{ext}")))
        except (FileNotFoundError, RuntimeError):
            pass  # Queue dir not configured or missing — return queue_depth=0

    last_event = _cached("last_event", ttl=5.0)
    if last_event is None:
        if run_dir:
            events = _read_jsonl_tail(run_dir / "events.jsonl", limit=1)
            last_event = _store("last_event", events[-1] if events else None)
        else:
            last_event = _store("last_event", None)

    # Count active agents
    from src.core.database import Agent
    from src.core.database import get_db as _get_db

    try:
        with _get_db() as _db:
            agent_query = _db.query(Agent).filter(Agent.status.in_(["working", "starting", "idle"]))
            if project_id:
                from src.core.database import Task, Workflow

                wf_ids = [wf.id for wf in _db.query(Workflow).filter_by(project_id=project_id).all()]
                task_ids = [t.id for t in _db.query(Task).filter(Task.workflow_id.in_(wf_ids)).all()]
                agent_query = agent_query.filter(Agent.current_task_id.in_(task_ids))
            active_agents = agent_query.count()
    except Exception:
        active_agents = 0

    # Resolve which project the (single, global) service is actually running,
    # if any -- so the UI can tell the user what's really running instead of
    # a generic "another project" message that's just as misleading when
    # it's actually the caller's own just-started run.
    running_project_path = service_status.get("project_path")
    running_project_name = None
    if running_project_path:
        try:
            from src.core.database import AutopilotProject
            from src.core.database import get_db as _get_db

            with _get_db() as _db:
                _rp = _db.query(AutopilotProject).filter_by(base_dir=running_project_path).first()
                running_project_name = _rp.name if _rp else Path(running_project_path).name
        except Exception:
            running_project_name = Path(running_project_path).name

    # Merge service status with file-based state
    # Derive error/reason for why the pipeline stopped
    designs_failed = service_status.get("designs_failed", 0) or state.get("designs_failed", 0)
    last_error = None
    if not running:
        service_error = service_status.get("error")
        if service_error:
            last_error = service_error
        elif last_event and last_event.get("type") == "error":
            last_error = last_event.get("message", "Unknown error")
        elif designs_failed > 0:
            last_error = f"{designs_failed} design(s) failed"

    result = PipelineStatus(
        running=running,
        current_design=service_status.get("current_design") or state.get("current_design"),
        current_workflow_id=state.get("current_workflow_id"),
        designs_processed=service_status.get("designs_processed", 0) or state.get("designs_processed", 0),
        designs_succeeded=service_status.get("designs_succeeded", 0) or state.get("designs_succeeded", 0),
        designs_failed=designs_failed,
        total_elapsed=service_status.get("elapsed_seconds", 0) or state.get("total_elapsed", 0),
        queue_depth=queue_depth,
        last_event=last_event,
        last_error=last_error,
        active_agents=active_agents,
        running_project_path=running_project_path,
        running_project_name=running_project_name,
        # Compute self-conflict server-side using realpath to handle
        # symlink resolution (/tmp -> /private/tmp on macOS). Checks BOTH
        # the single running_project_path (correct when project_id was
        # given above -- that's this project's own service) AND membership
        # in running_projects_list (needed when project_id was omitted --
        # running_project_path there is just running_services[0]'s path,
        # arbitrary order, so a caller whose own project is running but
        # isn't index 0 would otherwise be missed entirely and get told to
        # stop itself to start itself).
        is_self_conflict=(
            project_path is not None
            and (
                (running_project_path is not None and os.path.realpath(running_project_path) == os.path.realpath(project_path))
                or any(
                    p.get("base_dir") and os.path.realpath(p["base_dir"]) == os.path.realpath(project_path)
                    for p in running_projects_list
                )
            )
        ),
        running_projects=running_projects_list,
    )

    # Populate review_mode and features_awaiting_review for the requested project
    if project_id:
        try:
            from src.core.database import AutopilotProject, Feature, Workflow
            from src.core.database import get_db as _get_db

            with _get_db() as _db:
                _proj = _db.query(AutopilotProject).get(project_id)
                result.review_mode = bool(_proj and getattr(_proj, "review_mode", False))
                # Count features whose workflow is paused_by="review"
                proj_wf_ids = [
                    wf.id for wf in _db.query(Workflow).filter_by(project_id=project_id).all()
                ]
                if proj_wf_ids:
                    result.features_awaiting_review = (
                        _db.query(Feature)
                        .join(Workflow, Feature.workflow_id == Workflow.id)
                        .filter(
                            Feature.workflow_id.in_(proj_wf_ids),
                            Workflow.paused_by == "review",
                        )
                        .count()
                    )
        except Exception:
            pass

    return _store(cache_key, result)


# ── Design Queue ─────────────────────────────────────────────────


def _get_queue_order_path(project_id: Optional[str] = None) -> Optional[Path]:
    try:
        # Write alongside other server state under .hephaestus/, not inside
        # the tracked docs/design/ directory (which would pollute git status).
        effective_dir = _get_effective_queue_dir(project_id)
        hephaestus_dir = Path(effective_dir).parent.parent / CONTEXT_DIR_NAME
        hephaestus_dir.mkdir(parents=True, exist_ok=True)
        return hephaestus_dir / ".queue_order.json"
    except (FileNotFoundError, RuntimeError):
        return None


def _load_queue_order(project_id: Optional[str] = None) -> List[str]:
    path = _get_queue_order_path(project_id)
    if path and path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return []


def _save_queue_order(order: List[str], project_id: Optional[str] = None):
    path = _get_queue_order_path(project_id)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(order))


@router.get("/queue", response_model=List[DesignQueueItem])
async def list_design_queue(project_id: Optional[str] = None):
    cache_key = f"queue:{project_id}" if project_id else "queue"
    cached = _cached(cache_key)
    if cached is not None:
        return cached

    try:
        effective_dir = _get_effective_queue_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))

    queue_path = Path(effective_dir)
    saved_order = _load_queue_order(project_id)

    files_by_name: Dict[str, Path] = {}
    for ext in ALLOWED_EXTENSIONS:
        for f in queue_path.glob(f"*{ext}"):
            files_by_name[f.name] = f

    ordered_names = [n for n in saved_order if n in files_by_name]
    unordered = [n for n in files_by_name if n not in saved_order]
    all_names = ordered_names + sorted(unordered, key=lambda n: files_by_name[n].stat().st_mtime)

    items = []
    for fname in all_names:
        f = files_by_name[fname]
        stat = f.stat()
        name = f.stem.replace("_", " ").replace("-", " ").title()
        items.append(
            DesignQueueItem(
                filename=f.name,
                name=name,
                size_bytes=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                extension=f.suffix,
            )
        )

    return _store(cache_key, items)


class QueueReorderRequest(BaseModel):
    filenames: List[str]
    project_id: Optional[str] = None


@router.post("/queue/reorder")
async def reorder_queue(req: QueueReorderRequest):
    try:
        effective_dir = _get_effective_queue_dir(req.project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))

    queue_path = Path(effective_dir)
    existing = set()
    for ext in ALLOWED_EXTENSIONS:
        for f in queue_path.glob(f"*{ext}"):
            existing.add(f.name)

    for fname in req.filenames:
        if fname not in existing:
            raise HTTPException(400, f"Unknown file: {fname}")

    _save_queue_order(req.filenames, req.project_id)
    _invalidate("queue", f"queue:{req.project_id}" if req.project_id else "queue")
    return {"order": req.filenames}


@router.post("/queue/requeue")
async def requeue_design(request: dict):
    """Move a design to the front of the queue and pause its active workflow."""
    from src.core.database import Agent, Task, Workflow, get_db

    filename = request.get("filename")
    if not filename:
        raise HTTPException(400, "filename is required")
    req_project_id = request.get("project_id")

    # Get the queue order
    order = _load_queue_order(req_project_id)

    # Move to front
    if filename in order:
        order.remove(filename)
    order.insert(0, filename)
    _save_queue_order(order, req_project_id)
    _invalidate("queue", f"queue:{req_project_id}" if req_project_id else "queue")

    # Pause any active workflow processing this design
    paused_count = 0
    try:
        with get_db() as db:
            # Find autopilot workflows that are active
            active_workflows = (
                db.query(Workflow)
                .filter(
                    Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS),
                    Workflow.status.in_(["active", "running"]),
                )
                .all()
            )

            for wf in active_workflows:
                if wf.launch_params:
                    params = json.loads(wf.launch_params) if isinstance(wf.launch_params, str) else wf.launch_params
                    design_doc = params.get("design_document", "")
                    if filename in str(design_doc):
                        # Terminate agents for this workflow
                        task_ids = [
                            t.id
                            for t in db.query(Task)
                            .filter(
                                Task.workflow_id == wf.id,
                                Task.status.in_(["pending", "queued", "assigned", "in_progress"]),
                            )
                            .all()
                        ]

                        if task_ids:
                            agents = (
                                db.query(Agent)
                                .filter(
                                    Agent.current_task_id.in_(task_ids),
                                    Agent.status.in_(["working", "starting", "idle"]),
                                )
                                .all()
                            )
                            for agent in agents:
                                agent.status = "terminated"
                                agent.current_task_id = None  # Clear stale reference
                                agent.terminated_at = datetime.utcnow()

                        # Pause the workflow
                        wf.status = "paused"
                        paused_count += 1

            db.commit()
    except Exception as e:
        logger.error(f"Error pausing workflows for requeue: {e}")

    _invalidate("status")

    return {
        "requeued": True,
        "filename": filename,
        "paused_workflows": paused_count,
    }


@router.post("/queue/rerun")
async def rerun_design(request: dict):
    """Rerun a design: stop everything, move to front, start pipeline."""
    import signal
    import time
    from pathlib import Path

    from src.core.database import (
        Agent,
        AutopilotDesign,
        AutopilotProject,
        Feature,
        Task,
        Workflow,
        get_db,
    )

    filename = request.get("filename")
    if not filename:
        raise HTTPException(400, "filename is required")

    project_path = request.get("project_path")
    if not project_path:
        raise HTTPException(400, "project_path is required")

    # Validate project path exists
    project = Path(project_path).resolve()
    if not project.exists():
        raise HTTPException(400, f"Project path does not exist: {project_path}")

    # Resolved once and reused for every project-scoped step below (queue
    # order, pipeline state clearing, pipeline start) -- must all scope to
    # the SAME project, not independently-resolved ids that could diverge
    # once more than one project can be active at once.
    from src.autopilot.orchestrator import _get_or_create_project_id

    rerun_start_project_id = _get_or_create_project_id(str(project))

    # Validate design exists in queue
    queue_dir = project / DESIGN_CONTEXT_SUBDIR
    queue_dir.mkdir(parents=True, exist_ok=True)
    design_path = queue_dir / filename
    if not design_path.exists():
        raise HTTPException(404, f"Design not found in queue: {filename}")

    # Step 1: Stop the pipeline if running. Uses the in-process AutopilotService
    # (the same one the play/pause button drives) instead of spawning/killing a
    # separate `python -m src.autopilot.orchestrator` subprocess -- that older
    # subprocess path could run concurrently with the in-process service (both
    # calling run_phase0 independently), and was the root cause of design docs
    # ending up copied twice. See docs/MULTI_PROJECT_CONCURRENCY_DESIGN.md and
    # src/autopilot/service.py's module docstring for why the in-process
    # service replaced the subprocess approach in the first place.
    try:
        from src.autopilot.orchestrator import _resolve_project_id
        from src.autopilot.service import get_autopilot_service

        rerun_project_id = _resolve_project_id(str(project))
        if rerun_project_id:
            service = get_autopilot_service(rerun_project_id)
            if service.running:
                await service.stop()
    except Exception as e:
        logger.error(f"Error stopping in-process pipeline for rerun: {e}")

    # Defensive cleanup: kill any stray subprocess left over from before this
    # endpoint stopped spawning one (a currently-running old-style process
    # started by a previous backend version). Harmless no-op once nothing
    # writes orchestrator.pid anymore.
    try:
        pid_dir = Path(AUTOPILOT_STATE_DIR)
        pid_file = pid_dir / "orchestrator.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(10):
                    time.sleep(0.5)
                    try:
                        os.kill(pid, 0)  # Check if alive
                    except ProcessLookupError:
                        break
                try:
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(0.5)  # Give OS time to clean up
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
            pid_file.unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"Error killing stray orchestrator subprocess: {e}")

    # Step 2: Stop all active workflows and agents
    try:
        with get_db() as db:
            # Terminate all active agents
            active_agents = db.query(Agent).filter(Agent.status.in_(["working", "starting", "idle"])).all()
            for agent in active_agents:
                agent.status = "terminated"
                agent.current_task_id = None  # Clear stale reference
                agent.terminated_at = datetime.utcnow()

            # Mark all active workflows as paused (not active/running)
            active_workflows = db.query(Workflow).filter(Workflow.status.in_(["active", "running"])).all()
            for wf in active_workflows:
                wf.status = "paused"

            db.commit()
    except Exception as e:
        logger.error(f"Error stopping workflows for rerun: {e}")

    # Step 2b: Clean slate for this design's workflows, tasks, and features.
    # Rerun means "start fresh" — delete old rows so the orchestrator
    # doesn't see stale Feature rows and skip re-decomposition.
    try:
        from src.core.database import (
            AgentResult,
            BoardConfig,
            CostEntry,
            DiagnosticRun,
            Memory,
            Phase,
            PhaseExecution,
            TaskPromptOverride,
            Ticket,
            ValidationReview,
            WorkflowResult,
        )

        worktrees_to_clean: List[Tuple[str, dict]] = []
        with get_db() as db:
            matching_wfs = (
                db.query(Workflow)
                .filter(
                    Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS),
                    Workflow.launch_params.like(f"%{filename}%"),
                )
                .all()
            )
            wf_ids = [wf.id for wf in matching_wfs]

            # Get design to find features
            proj = db.query(AutopilotProject).filter_by(base_dir=str(project)).first()
            design = db.query(AutopilotDesign).filter_by(project_id=proj.id, filename=filename).first() if proj else None

            if wf_ids:
                # Get task IDs for dependent record cleanup
                task_ids = [t.id for t in db.query(Task).filter(Task.workflow_id.in_(wf_ids)).all()]

                # Delete dependent records (order matters for FK constraints)
                if task_ids:
                    db.query(TaskPromptOverride).filter(TaskPromptOverride.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(ValidationReview).filter(ValidationReview.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(AgentResult).filter(AgentResult.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(Memory).filter(Memory.related_task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(Ticket).filter(Ticket.task_id.in_(task_ids)).delete(synchronize_session=False)
                    # CostEntry.task_id/workflow_id are also enforced FKs -- a
                    # workflow that ever recorded real LLM cost (the common
                    # case now that cost tracking exists) would otherwise
                    # fail this delete with an IntegrityError.
                    db.query(CostEntry).filter(CostEntry.task_id.in_(task_ids)).delete(synchronize_session=False)

                # Delete workflow-level dependents
                db.query(DiagnosticRun).filter(DiagnosticRun.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(WorkflowResult).filter(WorkflowResult.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(BoardConfig).filter(BoardConfig.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(Ticket).filter(Ticket.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(CostEntry).filter(CostEntry.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

                # Collect worktree info before the Workflow rows are gone.
                # Without this, _create_integration_worktree's deterministic
                # per-design path (design_id-derived, unchanged by rerun)
                # finds the OLD worktree still sitting there and reuses it
                # as-is (it only creates fresh `if not wt_path.exists()`) --
                # "rerun" would silently continue from stale commits instead
                # of actually starting over. Step 2 above already terminated
                # every active agent and paused every active workflow, so
                # nothing is still writing to these worktrees by this point.
                for wf in db.query(Workflow).filter(Workflow.id.in_(wf_ids)).all():
                    if wf.working_directory and ".worktrees/" in wf.working_directory:
                        lp = wf.launch_params if isinstance(wf.launch_params, dict) else {}
                        worktrees_to_clean.append((wf.working_directory, lp))

                # Delete tasks -- must happen before Phase/PhaseExecution
                # below: Task.phase_id is a FK to phases.id, so deleting
                # Phase rows first (as an earlier version of this fix did)
                # fails with the same FOREIGN KEY error, just one table over.
                db.query(Task).filter(Task.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

                # Delete phase executions -- PhaseExecution links to a
                # workflow via phase_id -> Phase.workflow_id, not the
                # workflow_execution_id column (an unused legacy field
                # that's never actually populated with a workflow id, so
                # filtering on it matched zero rows and left every
                # PhaseExecution -- and the Phase rows below -- behind).
                phase_ids = [p.id for p in db.query(Phase.id).filter(Phase.workflow_id.in_(wf_ids)).all()]
                if phase_ids:
                    db.query(PhaseExecution).filter(PhaseExecution.phase_id.in_(phase_ids)).delete(synchronize_session=False)

                # Delete phases -- Phase.workflow_id is a NOT NULL FK to
                # workflows.id, so leaving these behind (as this always
                # did) made the Workflow delete below fail with a
                # FOREIGN KEY constraint error every time.
                db.query(Phase).filter(Phase.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

                # Delete workflows
                db.query(Workflow).filter(Workflow.id.in_(wf_ids)).delete(synchronize_session=False)

            # Delete features for this design
            if design:
                db.query(Feature).filter_by(design_id=design.id).delete(synchronize_session=False)
                # Reset design status so orchestrator picks it up fresh
                design.status = "pending"
                # Clear retry counter so fresh retry starts at 0
                from src.autopilot.orchestrator import _delete_project_context

                _delete_project_context(db, f"autopilot_retry_{design.id}")

            db.commit()
            logger.info(f"[RERUN] Cleaned up {len(wf_ids)} workflows and features for {filename}")

        # Best-effort worktree cleanup, now that the DB transaction above
        # has committed -- not fatal if any single one can't be resolved.
        for working_directory, launch_params in worktrees_to_clean:
            try:
                wt_path = Path(working_directory)
                if not (wt_path / ".git").exists():
                    continue
                project_path_str = launch_params.get("project_path")
                if not project_path_str:
                    logger.warning(
                        f"[RERUN] {wt_path} has no launch_params.project_path "
                        "to scope cleanup to -- left in place"
                    )
                    continue
                import git as _git

                from src.autopilot.orchestrator import _cleanup_worktree

                try:
                    branch = _git.Repo(wt_path).active_branch.name
                except Exception:
                    branch = ""
                _cleanup_worktree(wt_path, branch, Path(project_path_str), logger)
            except Exception as e:
                logger.warning(f"[RERUN] Failed to clean up worktree {working_directory}: {e}")
    except Exception as e:
        logger.error(f"Error cleaning up design state for rerun: {e}")

    # Step 3: Clean up branches (non-blocking)
    try:
        from src.core.database import DatabaseManager
        from src.core.worktree_manager import WorktreeManager

        db_manager = DatabaseManager()
        bm = WorktreeManager(db_manager)
        # Without this, WorktreeManager operates on whatever project happens
        # to be config.main_repo_path's current global default -- wrong
        # project entirely once more than one project exists (see the other
        # WorktreeManager(...).reload(...) call sites in orchestrator.py,
        # which already do this for the same reason).
        bm.reload(project)
        # Run cleanup in background thread to not block pipeline start
        import threading

        thread = threading.Thread(target=lambda: bm.cleanup_all_stale_branches(), daemon=True)
        thread.start()
    except Exception as e:
        logger.error(f"Error starting branch cleanup: {e}")

    # Step 4: Move design to front of queue
    order = _load_queue_order(rerun_start_project_id)
    if filename in order:
        order.remove(filename)
    order.insert(0, filename)
    _save_queue_order(order, rerun_start_project_id)
    _invalidate("queue", f"queue:{rerun_start_project_id}")

    # Step 5: Clear pipeline state so orchestrator starts fresh
    try:
        from src.autopilot.orchestrator import PersistentPipelineState

        PersistentPipelineState(project_id=rerun_start_project_id).clear()
    except Exception as e:
        logger.error(f"Error clearing pipeline state: {e}")

    # Step 6: Start pipeline via the in-process AutopilotService (the same
    # singleton the play/pause button drives), not a spawned subprocess.
    try:
        from src.autopilot.service import get_autopilot_service, get_registry

        # Same concurrency-cap check POST /start enforces -- without this,
        # rerun could start a brand-new, not-yet-running project's pipeline
        # even while already at max_concurrent_projects, silently exceeding
        # the cap that starting the identical project via POST /start would
        # have rejected with a 409. try_reserve (not can_start) also closes
        # the TOCTOU race between two concurrent starts both checking the
        # cap before either has actually started -- release it as soon as
        # service.start() resolves, success or not.
        can_start, cap_message = get_registry().try_reserve(rerun_start_project_id)
        if not can_start:
            raise HTTPException(409, cap_message)

        service = get_autopilot_service(rerun_start_project_id)
        try:
            await service.start(
                project_path=str(project),
                design_queue=str(queue_dir),
                max_iterations=3,
            )
        finally:
            get_registry().release_reservation(rerun_start_project_id)

        # Wait for new workflow to be created (up to 15 seconds). asyncio.sleep,
        # not time.sleep -- this is an async route handler, and a blocking
        # sleep here would stall every other request the whole backend is
        # serving for up to 15s, not just this one.
        new_workflow_id = None
        design_name_clean = filename.replace(".md", "").replace("_", " ").lower()
        for _ in range(30):  # 30 * 0.5s = 15s max
            await asyncio.sleep(0.5)
            try:
                with get_db() as db:
                    # Check for new active workflow
                    wf = (
                        db.query(Workflow)
                        .filter(
                            Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS),
                            Workflow.status == "active",
                        )
                        .order_by(Workflow.created_at.desc())
                        .first()
                    )
                    if wf:
                        # Verify it's for this design by checking description
                        desc = (wf.description or "").lower()
                        # Use exact match on design name (without extension)
                        if design_name_clean in desc:
                            new_workflow_id = wf.id
                            break
            except Exception:
                pass
    except HTTPException:
        raise
    except ValueError as e:
        # Matches /start's own convention: bad input (e.g. project path isn't
        # a git repo -- a real check service.start() does that the old
        # subprocess never surfaced clearly).
        logger.error(f"Error starting pipeline for rerun: {e}")
        raise HTTPException(400, f"Failed to start pipeline: {e}")
    except RuntimeError as e:
        # Matches /start's own convention: 409 means "already running" --
        # possible here despite Step 1's stop() if another request raced in
        # and started something else in the meantime.
        logger.error(f"Error starting pipeline for rerun: {e}")
        raise HTTPException(409, f"Failed to start pipeline: {e}")
    except Exception as e:
        logger.error(f"Error starting pipeline for rerun: {e}")
        raise HTTPException(500, f"Failed to start pipeline: {e}")

    _invalidate("status")

    return {
        "rerun": True,
        "filename": filename,
        "workflow_id": new_workflow_id,
        "message": f"Pipeline restarted for {filename}",
    }


@router.post("/queue/repair")
async def repair_design(request: dict):
    """Repair a design: spin up a recovery workflow and a review agent that checks
    and fixes stuck/incomplete tasks. (Branch reconciliation is obsolete under
    per-task worktree isolation — failed worktrees are discarded, never merged.)"""
    import uuid
    from pathlib import Path

    logger.info("[REPAIR] Received repair request")
    filename = request.get("filename")
    if not filename:
        raise HTTPException(400, "filename is required")

    project_path = request.get("project_path")
    if not project_path:
        raise HTTPException(400, "project_path is required")

    project = Path(project_path).resolve()
    if not project.exists():
        raise HTTPException(400, f"Project path does not exist: {project_path}")

    # Generate repair ID for tracking
    repair_id = str(uuid.uuid4())[:8]

    # Run repair in background thread pool (not async - uses sync subprocess calls)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_repair, repair_id, filename, project, logger)

    return {
        "repair_id": repair_id,
        "status": "started",
        "message": f"Repair started for {filename}. Check /api/autopilot/queue/repair/{repair_id} for results.",
    }


def spawn_repair_review_agent(wf_id: str, filename: str, project: Path, reason: str, logger, actions_taken: list):
    """Spawn a review agent that checks each task, acts, and monitors completion."""
    from src.autopilot.orchestrator import api_post, get_tasks

    try:
        logger.info(f"[REPAIR-AGENT] Starting for workflow {wf_id[:8]}, design={filename}")

        # Get tasks for this workflow
        failed_tasks = get_tasks(status="failed", workflow_id=wf_id)
        pending_tasks = get_tasks(status="pending", workflow_id=wf_id)
        in_progress_tasks = get_tasks(status="in_progress", workflow_id=wf_id)
        done_tasks = get_tasks(status="done", workflow_id=wf_id)

        logger.info(f"[REPAIR-AGENT] Task counts: done={len(done_tasks)}, failed={len(failed_tasks)}, pending={len(pending_tasks)}, in_progress={len(in_progress_tasks)}")

        # Build task summary for instructions
        task_summary = []
        for t in failed_tasks[:5]:
            desc = (t.get("enriched_description") or t.get("raw_description") or "")[:80]
            task_summary.append(f"  FAILED: {t.get('id', '')[:8]} - {desc}")
        for t in pending_tasks[:5]:
            desc = (t.get("enriched_description") or t.get("raw_description") or "")[:80]
            task_summary.append(f"  PENDING: {t.get('id', '')[:8]} - {desc}")
        for t in in_progress_tasks[:5]:
            desc = (t.get("enriched_description") or t.get("raw_description") or "")[:80]
            task_summary.append(f"  IN_PROGRESS: {t.get('id', '')[:8]} - {desc}")

        review_instructions = get_prompt("repair_agent_instructions", {
            "filename": filename,
            "wf_id_short": wf_id[:8],
            "reason": reason,
            "done_count": len(done_tasks),
            "failed_count": len(failed_tasks),
            "pending_count": len(pending_tasks),
            "in_progress_count": len(in_progress_tasks),
            "task_summary": chr(10).join(task_summary) if task_summary else "No tasks found",
            "design_doc_path": project / DESIGN_CONTEXT_SUBDIR / filename,
        })

        # Create the task
        logger.info(f"[REPAIR-AGENT] Creating task for workflow {wf_id[:8]}")
        task_data = api_post(
            "/create_task",
            {
                "task_description": review_instructions,
                "done_definition": "All tasks resolved, branches merged, repair_report.md written",
                "workflow_id": wf_id,
                "phase_id": "repair-review",
                "priority": "high",
                "ai_agent_id": "sdk-repair-agent",
            },
            headers={"X-Agent-ID": "sdk-repair-agent"},
        )

        if not task_data:
            logger.error("[REPAIR-AGENT] api_post /create_task returned None")
            return

        if "detail" in task_data:
            logger.error(f"[REPAIR-AGENT] /create_task error: {task_data['detail']}")
            return

        task_id = task_data.get("task_id")
        if not task_id:
            logger.error(f"[REPAIR-AGENT] /create_task returned no task_id: {task_data}")
            return

        logger.info(f"[REPAIR-AGENT] Task created: {task_id[:8]}")

        # Create the agent. This runs on a background executor thread (not an
        # awaited request path), so a generous timeout is safe — but it still
        # needs to exceed the agent's own ~25s+ tmux/pi init delay
        # (src/agents/manager.py), otherwise this silently returns None while
        # the agent keeps starting up in the background, leaving the task
        # never linked to it (same failure mode fixed in resume_feature).
        logger.info(f"[REPAIR-AGENT] Creating agent for task {task_id[:8]}")
        agent_data = api_post(
            "/api/create_agent_for_task",
            {"task_id": task_id, "workflow_id": wf_id, "phase_id": "repair-review"},
            timeout=120,
        )

        if not agent_data:
            logger.error("[REPAIR-AGENT] api_post /create_agent_for_task returned None")
            return

        if "detail" in agent_data:
            logger.error(f"[REPAIR-AGENT] /create_agent_for_task error: {agent_data['detail']}")
            return

        agent_id = agent_data.get("agent_id")
        if not agent_id:
            logger.error(f"[REPAIR-AGENT] /create_agent_for_task returned no agent_id: {agent_data}")
            return

        logger.info(f"[REPAIR-AGENT] Agent created: {agent_id[:8]}")
        actions_taken.append(f"Spawned review agent {agent_id[:8]} for workflow {wf_id[:8]}")

    except Exception as e:
        logger.error(f"[REPAIR-AGENT] Exception: {e}", exc_info=True)


def _run_repair(repair_id: str, filename: str, project: Path, logger):
    """Background repair task."""
    import json
    import uuid

    from src.core.database import Workflow, get_db

    logger.info(f"[REPAIR] Starting repair {repair_id} for {filename}")

    findings = []
    actions_taken = []

    try:
        # 1. Create a minimal repair workflow directly in DB
        logger.info("[REPAIR] Step 1: Creating repair workflow")
        wf_id = f"repair-{uuid.uuid4().hex[:8]}"

        with get_db() as db:
            workflow = Workflow(
                id=wf_id,
                name=f"Repair: {filename}",
                definition_id="autopilot",
                description=f"Repair: {filename}",
                phases_folder_path=str(project),
                status="active",
                launch_params=json.dumps(
                    {
                        "design_document": str(project / DESIGN_CONTEXT_SUBDIR / filename),
                        "project_path": str(project),
                        "repair_mode": True,
                    }
                ),
            )
            db.add(workflow)
            db.commit()
            logger.info(f"[REPAIR] Workflow created: {wf_id}")

        actions_taken.append(f"Created repair workflow {wf_id[:8]}")
        findings.append({"type": "info", "message": f"Created repair workflow {wf_id[:8]}"})

        # 2. Spawn review agent on the new workflow
        logger.info("[REPAIR] Step 2: Spawning review agent")
        spawn_repair_review_agent(wf_id, filename, project, "Repair initiated", logger, actions_taken)
        logger.info("[REPAIR] Step 2 complete: spawn_repair_review_agent returned")

        # 3. Find any existing workflows for context
        logger.info("[REPAIR] Step 3: Finding existing workflows for context")
        with get_db() as db:
            workflows = db.query(Workflow).filter(Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS)).all()

            existing_workflow_ids = []
            for wf in workflows:
                if wf.launch_params:
                    try:
                        params = json.loads(wf.launch_params) if isinstance(wf.launch_params, str) else wf.launch_params
                        doc = params.get("design_document", "")
                        if filename in doc:
                            existing_workflow_ids.append(wf.id)
                    except Exception:
                        pass

            logger.info(f"[REPAIR] Found {len(existing_workflow_ids)} existing workflow(s)")
            if existing_workflow_ids:
                findings.append(
                    {
                        "type": "info",
                        "message": f"Found {len(existing_workflow_ids)} existing workflow(s) for context",
                    }
                )

        # NOTE: Repair no longer merges/cleans stray agent branches. Under
        # per-task worktree isolation a failed agent's worktree is discarded and
        # never merged, so there are no half-baked branches to reconcile. Repair
        # is now purely workflow recovery (review agent on the tasks above).

        # 4. Store results
        logger.info("[REPAIR] Step 4: Storing results")
        result = {
            "repair_id": repair_id,
            "filename": filename,
            "findings": findings,
            "actions_taken": actions_taken,
            "summary": {
                "total_findings": len(findings),
                "actions_taken": len(actions_taken),
                "workflows_created": 1,
            },
        }

        result_file = Path(AUTOPILOT_STATE_DIR) / f"repair_{repair_id}.json"
        result_file.write_text(json.dumps(result, indent=2))
        logger.info(f"[REPAIR] Repair {repair_id} complete. Actions: {len(actions_taken)}, Findings: {len(findings)}")

    except Exception as e:
        logger.error(f"[REPAIR] Exception during repair: {e}", exc_info=True)
        findings.append({"type": "error", "message": str(e)})
        result = {
            "repair_id": repair_id,
            "filename": filename,
            "findings": findings,
            "actions_taken": actions_taken,
            "summary": {"error": str(e)},
        }
        result_file = Path(AUTOPILOT_STATE_DIR) / f"repair_{repair_id}.json"
        result_file.write_text(json.dumps(result, indent=2))


@router.get("/queue/repair/{repair_id}")
async def get_repair_status(repair_id: str):
    """Get repair status and results."""
    logger.info(f"[REPAIR] Status check for {repair_id}")
    result_file = Path(AUTOPILOT_STATE_DIR) / f"repair_{repair_id}.json"
    if not result_file.exists():
        logger.info(f"[REPAIR] {repair_id} still running (no result file yet)")
        return {
            "repair_id": repair_id,
            "status": "running",
            "message": "Repair still in progress...",
        }

    try:
        result = json.loads(result_file.read_text())
        result["status"] = "completed"
        logger.info(f"[REPAIR] {repair_id} completed")
        return result
    except Exception as e:
        logger.error(f"[REPAIR] {repair_id} error reading results: {e}")
        return {"repair_id": repair_id, "status": "error", "message": str(e)}


# ── Design Add (file_path based) ─────────────────────────────────


class DesignAddByPath(BaseModel):
    file_path: str
    project_path: str


@router.post("/designs/add")
async def add_design_by_path(req: DesignAddByPath):
    """Add a design document by file path.

    Validates file exists, finds/creates AutopilotProject, checks for duplicates,
    and creates AutopilotDesign record with file_path.

    Returns:
        Design ID, name, and status
    """
    import hashlib
    import uuid

    from src.core.database import AutopilotDesign, AutopilotProject, get_db
    from src.core.simple_config import get_config

    # Validate file exists
    file_path = Path(req.file_path).resolve()
    if not file_path.exists():
        raise HTTPException(400, f"File does not exist: {file_path}")
    if not file_path.is_file():
        raise HTTPException(400, f"Path is not a file: {file_path}")
    if file_path.suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            400,
            f"Invalid file extension: {file_path.suffix}. Allowed: {ALLOWED_EXTENSIONS}",
        )

    # Validate project path
    project_path = Path(req.project_path).resolve()
    if not project_path.exists():
        raise HTTPException(400, f"Project path does not exist: {project_path}")

    # Calculate content hash for dedup
    content_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()[:16]

    with get_db() as db:
        # Find or create project
        project = db.query(AutopilotProject).filter_by(base_dir=str(project_path)).first()
        if not project:
            # Cap simultaneously-active projects at max_concurrent_projects
            # instead of exclusively clearing every other project's flag --
            # mirrors projects_api.py's create_project/activate_project.
            # Lenient like create_project's own is_first path (not a 409
            # like activate_project): activation here is a side effect of
            # an unrelated "upload a design file" action, so a full project
            # cap shouldn't fail the upload -- create it inactive instead.
            active_count = db.query(AutopilotProject).filter_by(is_active=True).count()
            max_concurrent = get_config().max_concurrent_projects
            want_active = active_count < max_concurrent
            if not want_active:
                logger.warning(
                    f"Not auto-activating new project {project_path.name!r}: "
                    f"max_concurrent_projects ({max_concurrent}) already reached"
                )
            project = AutopilotProject(
                id=f"proj-{uuid.uuid4().hex[:12]}",
                name=project_path.name,
                base_dir=str(project_path),
                is_active=want_active,
            )
            db.add(project)
            db.flush()
            logger.info(f"Created project: {project.name} ({project.id})")

        # Check for duplicate file_path
        existing = (
            db.query(AutopilotDesign)
            .filter_by(
                project_id=project.id,
                file_path=str(file_path),
            )
            .first()
        )

        if existing:
            # Return existing design
            return {
                "id": existing.id,
                "name": existing.name,
                "status": existing.status,
            }

        # Create design record
        design_id = f"des-{uuid.uuid4().hex[:12]}"
        name = file_path.stem.replace("_", " ").replace("-", " ").title()

        # Get ordinal (max ordinal + 1)
        max_ordinal = db.query(AutopilotDesign).filter_by(project_id=project.id).count()

        design = AutopilotDesign(
            id=design_id,
            project_id=project.id,
            filename=file_path.name,
            name=name,
            ordinal=max_ordinal + 1,
            size_bytes=file_path.stat().st_size,
            extension=file_path.suffix,
            content_hash=content_hash,
            status="pending",
            file_path=str(file_path),
        )
        db.add(design)
        db.commit()

        logger.info(f"Added design: {name} ({design_id}) from {file_path}")

        return {
            "id": design_id,
            "name": name,
            "status": "pending",
        }


@router.post("/queue", response_model=DesignQueueItem)
async def add_to_queue(item: DesignQueueAdd):
    try:
        effective_dir = _get_effective_queue_dir(item.project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))

    queue_path = Path(effective_dir)
    queue_path.mkdir(parents=True, exist_ok=True)

    ext = item.extension if item.extension in ALLOWED_EXTENSIONS else ".md"
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in item.name)
    safe_name = safe_name.strip().replace(" ", "_")
    if not safe_name:
        raise HTTPException(400, "Invalid design name")
    filename = f"{safe_name}{ext}"
    filepath = _safe_path(effective_dir, filename)

    if filepath.exists():
        raise HTTPException(409, f"Design '{filename}' already exists in queue")

    filepath.write_text(item.content)
    stat = filepath.stat()

    _invalidate("queue", f"queue:{item.project_id}" if item.project_id else "queue", "status")

    return DesignQueueItem(
        filename=filename,
        name=item.name,
        size_bytes=stat.st_size,
        modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        extension=ext,
    )


@router.delete("/queue/{filename}")
async def remove_from_queue(filename: str, project_id: Optional[str] = None):
    try:
        effective_dir = _get_effective_queue_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    filepath = _safe_path(effective_dir, filename)
    if not filepath.exists():
        raise HTTPException(404, f"Design '{filename}' not found")
    filepath.unlink()
    _invalidate("queue", f"queue:{project_id}" if project_id else "queue", "status")
    return {"removed": filename}


@router.get("/queue/{filename}/content")
async def get_queue_item_content(filename: str, project_id: Optional[str] = None):
    try:
        effective_dir = _get_effective_queue_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    filepath = _safe_path(effective_dir, filename)
    if not filepath.exists():
        raise HTTPException(404, f"Design '{filename}' not found")
    return {"filename": filename, "content": filepath.read_text(errors="replace")}


# ── Projects ────────────────────────────────────────────────────

_ORDINAL_RE = re.compile(r"^(\d+)[-_]")


def _design_id(project_id: str, filename: str) -> str:
    """Generate a stable, deterministic ID for a design document."""
    h = hashlib.sha256(f"{project_id}:{filename}".encode()).hexdigest()[:12]
    return f"des-{h}"


class ProjectItem(BaseModel):
    id: str
    name: str
    base_dir: str
    is_default: bool
    is_active: bool = False
    design_count: int
    created_at: str
    updated_at: str
    cost_total_usd: float = 0.0
    cost_limit_usd: Optional[float] = None


class ProjectCreate(BaseModel):
    name: str
    base_dir: str
    is_default: bool = False


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    base_dir: Optional[str] = None
    is_default: Optional[bool] = None
    cost_limit_usd: Optional[float] = None
    clear_cost_limit: bool = False  # Explicit signal to clear the budget

    @field_validator("cost_limit_usd")
    @classmethod
    def validate_cost_limit_usd(cls, v: Optional[float]) -> Optional[float]:
        """Validate cost_limit_usd is a reasonable value.

        SECURITY: Prevents setting absurdly large or invalid budget limits
        that could bypass budget enforcement or cause floating-point issues.
        """
        if v is None:
            return v
        if math.isnan(v) or math.isinf(v):
            raise ValueError("cost_limit_usd must be a finite number")
        if v < 0:
            raise ValueError("cost_limit_usd must be non-negative")
        if v > 1_000_000:  # $1M max budget
            raise ValueError("cost_limit_usd exceeds maximum allowed value of $1,000,000")
        return v


class CostEntryCreate(BaseModel):
    """Request model for creating a cost entry."""

    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    workflow_id: Optional[str] = None
    source: str
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float
    raw_usage: Optional[dict] = None

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: str) -> str:
        """Validate source is a known cost collection source."""
        valid_sources = {"pi", "claude_code", "opencode", "codex", "openrouter_direct"}
        if v not in valid_sources:
            raise ValueError(f"source must be one of {valid_sources}, got '{v}'")
        return v

    @field_validator("cost_usd")
    @classmethod
    def validate_cost_usd(cls, v: float) -> float:
        """Validate cost_usd is a reasonable positive value."""
        if v < 0:
            raise ValueError("cost_usd must be non-negative")
        if v > 1000.0:  # Cap at $1000 per single LLM call
            raise ValueError("cost_usd exceeds maximum allowed value of $1000")
        return v

    @field_validator("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
    @classmethod
    def validate_token_counts(cls, v: int) -> int:
        """Validate token counts are non-negative."""
        if v < 0:
            raise ValueError("token counts must be non-negative")
        if v > 10_000_000:  # 10M tokens max per call
            raise ValueError("token count exceeds maximum allowed value")
        return v

    @validator("raw_usage")
    def validate_raw_usage(cls, v: Optional[dict]) -> Optional[dict]:
        """Validate raw_usage is not excessively large.

        SECURITY: Prevents abuse where a malicious caller could store
        arbitrarily large payloads in the raw_usage JSON column,
        consuming database storage and slowing queries.
        """
        if v is not None:
            import sys as _sys

            size = _sys.getsizeof(json.dumps(v))
            if size > 10_000:  # 10KB limit
                raise ValueError("raw_usage exceeds maximum size of 10KB")
        return v

    @validator("model")
    def validate_model(cls, v: Optional[str]) -> Optional[str]:
        """Validate model string length."""
        if v is not None and len(v) > 200:
            raise ValueError("model name exceeds maximum length of 200 characters")
        return v

    @model_validator(mode="after")
    def validate_entity_link(self) -> "CostEntryCreate":
        """Require at least one of task_id or workflow_id for cost attribution.

        Without an entity link, the cost entry bypasses budget enforcement
        because no derivation rollup occurs (record_cost skips derive_task_cost
        and derive_workflow_cost when both are None).
        """
        if self.task_id is None and self.workflow_id is None:
            raise ValueError("At least one of task_id or workflow_id must be provided for cost attribution and budget enforcement")
        return self


class DesignItem(BaseModel):
    id: str
    filename: str
    name: str
    ordinal: int
    size_bytes: int
    extension: str
    modified_at: Optional[str] = None


class DesignReorderRequest(BaseModel):
    design_ids: List[str]


class DesignAddRequest(BaseModel):
    name: str
    content: str
    extension: str = ".md"


_project_sync_locks: Dict[str, asyncio.Lock] = {}
_project_lock_guard = asyncio.Lock()


async def _get_project_lock(project_id: str) -> asyncio.Lock:
    async with _project_lock_guard:
        if project_id not in _project_sync_locks:
            _project_sync_locks[project_id] = asyncio.Lock()
        return _project_sync_locks[project_id]


def _get_design_queue_dir(project_base: str) -> Path:
    """Return the design queue directory (.hephaestus/designs/).

    Designs are stored outside the git repo so commits don't delete them.
    """
    return Path(project_base) / DESIGN_CONTEXT_SUBDIR


def _find_archived_feature_report(project_base: str, workflow_id: str) -> Optional[Path]:
    """Find a workflow's feature_report.html in the archived features
    gallery, once its worktree (and Workflow.working_directory) is gone.

    PhaseManager._populate_feature_folder archives a durable copy to
    <project_base>/.hephaestus/features/<timestamp>_<design-name>/ at full
    workflow completion, right before _cleanup_worktree removes the
    worktree that would otherwise be the only copy. Folder names are
    timestamp+design-name only, not feature-specific, so a design with
    more than one feature can't be matched by name alone -- match instead
    via the workflow_id each folder's own pipeline_metrics.json records.

    Shared by get_project_design_status's has_report flag and
    get_workflow_feature_report's actual file serving, so both agree on
    exactly the same report once a feature has fully completed.
    """
    features_gallery = Path(project_base) / CONTEXT_DIR_NAME / "features"
    if not features_gallery.is_dir():
        return None
    for gallery_dir in features_gallery.iterdir():
        metrics_path = gallery_dir / "docs" / "pipeline_metrics.json"
        if not metrics_path.is_file():
            continue
        try:
            metrics = json.loads(metrics_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if metrics.get("workflow_id") != workflow_id:
            continue
        for candidate in (
            gallery_dir / "docs" / "feature_report.html",
            gallery_dir / "feature_report.html",
        ):
            if candidate.is_file():
                return candidate
        return None
    return None


def _extract_ordinal(filename: str) -> Optional[int]:
    """Extract numeric ordinal from filename prefix (e.g. '01-foo.md' → 1).

    Requires a separator (- or _) between digits and the rest of the name
    to avoid treating random digit-prefixed filenames as ordered.
    """
    stem = Path(filename).stem
    m = _ORDINAL_RE.match(stem)
    return int(m.group(1)) if m else None


def _sync_project_designs(project_id: str, project_base: str, db) -> List[Dict[str, Any]]:
    """Scan filesystem and sync designs with DB using the provided session.

    MUST be called within an active DB session (the `db` parameter).
    Returns list of design dicts.
    """
    from src.core.database import AutopilotDesign

    design_dir = Path(project_base) / DESIGN_CONTEXT_SUBDIR
    design_dir.mkdir(parents=True, exist_ok=True)

    fs_files: Dict[str, Path] = {}
    for ext in ALLOWED_EXTENSIONS:
        for f in design_dir.glob(f"*{ext}"):
            fs_files[f.name] = f

    existing = {d.filename: d for d in db.query(AutopilotDesign).filter_by(project_id=project_id).all()}

    fs_filenames = set(fs_files.keys())
    db_filenames = set(existing.keys())

    # Remove DB records for deleted files
    for fname in db_filenames - fs_filenames:
        db.delete(existing[fname])

    # Add or update DB records — two passes: prefixed first, then unprefixed
    prefixed = []
    unprefixed = []
    for fname, fpath in fs_files.items():
        if _extract_ordinal(fname) is not None:
            prefixed.append((fname, fpath))
        else:
            unprefixed.append((fname, fpath))

    # Pass 1: files with numeric prefixes (sorted by prefix)
    for fname, fpath in sorted(prefixed, key=lambda x: _extract_ordinal(x[0])):
        stat = fpath.stat()
        stem = fpath.stem
        name = stem.replace("_", " ").replace("-", " ").title()
        ordinal = _extract_ordinal(fname)

        if fname in existing:
            d = existing[fname]
            d.size_bytes = stat.st_size
            d.modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            # Don't overwrite ordinal - preserve manual reorders
        else:
            d = AutopilotDesign(
                id=_design_id(project_id, fname),
                project_id=project_id,
                filename=fname,
                name=name,
                ordinal=ordinal,
                size_bytes=stat.st_size,
                extension=fpath.suffix,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
            db.add(d)

    db.flush()

    # Pass 2: unprefixed files (alphabetical), ordinals continue after prefixed max
    max_prefixed = db.query(AutopilotDesign).filter_by(project_id=project_id).count()

    for fname, fpath in sorted(unprefixed, key=lambda x: x[0]):
        stat = fpath.stat()
        stem = fpath.stem
        name = stem.replace("_", " ").replace("-", " ").title()

        if fname in existing:
            d = existing[fname]
            d.size_bytes = stat.st_size
            d.modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            # Only assign ordinal on first insert, don't overwrite manual reorders
        else:
            max_prefixed += 1
            d = AutopilotDesign(
                id=_design_id(project_id, fname),
                project_id=project_id,
                filename=fname,
                name=name,
                ordinal=max_prefixed,
                size_bytes=stat.st_size,
                extension=fpath.suffix,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
            db.add(d)

    db.flush()

    # Re-read to return fresh state (same session, post-flush)
    designs = db.query(AutopilotDesign).filter_by(project_id=project_id).order_by(AutopilotDesign.ordinal).all()
    return [
        {
            "id": d.id,
            "filename": d.filename,
            "name": d.name,
            "ordinal": d.ordinal,
            "size_bytes": d.size_bytes,
            "extension": d.extension,
            "modified_at": d.modified_at.isoformat() if d.modified_at else None,
        }
        for d in designs
    ]


def _validate_base_dir(base_dir: str) -> str:
    """Validate and resolve a project base directory. Returns resolved path or raises."""
    base = Path(base_dir).expanduser().resolve()
    if not base.exists():
        raise HTTPException(400, f"Directory does not exist: {base}")
    if not base.is_dir():
        raise HTTPException(400, f"Not a directory: {base}")
    if not os.access(base, os.R_OK | os.W_OK):
        raise HTTPException(403, f"Insufficient permissions: {base}")
    return str(base)


@router.get("/projects", response_model=List[ProjectItem])
async def list_projects():
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        projects = db.query(AutopilotProject).order_by(AutopilotProject.name).all()
        result = []
        for p in projects:
            count = db.query(AutopilotDesign).filter_by(project_id=p.id).count()
            result.append(
                ProjectItem(
                    id=p.id,
                    name=p.name,
                    base_dir=p.base_dir,
                    is_default=p.is_default,
                    is_active=getattr(p, "is_active", False),
                    design_count=count,
                    created_at=p.created_at.isoformat() if p.created_at else "",
                    updated_at=p.updated_at.isoformat() if p.updated_at else "",
                    cost_total_usd=p.cost_total_usd or 0.0,
                    cost_limit_usd=p.cost_limit_usd,
                )
            )
        return result


@router.post("/projects", response_model=ProjectItem)
async def create_project(
    req: ProjectCreate,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    # SECURITY: Verify agent authentication before allowing project creation
    if not await verify_agent_authentication(agent_id):
        logger.warning(f"Unauthenticated project creation attempt from agent {agent_id}")
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    from src.core.database import AutopilotProject, get_db

    resolved = _validate_base_dir(req.base_dir)

    with get_db() as db:
        existing_proj = db.query(AutopilotProject).filter_by(base_dir=resolved).first()
        if existing_proj:
            raise HTTPException(409, f"Project already exists for directory: {resolved}")

        if req.is_default:
            db.query(AutopilotProject).update({"is_default": False})

        proj = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:12]}",
            name=req.name,
            base_dir=resolved,
            is_default=req.is_default,
        )
        db.add(proj)
        db.flush()

        # Sync designs in the SAME session — no nested get_db()
        designs = _sync_project_designs(proj.id, resolved, db)

        _invalidate("queue", "status")

        return ProjectItem(
            id=proj.id,
            name=proj.name,
            base_dir=proj.base_dir,
            is_default=proj.is_default,
            is_active=getattr(proj, "is_active", False),
            design_count=len(designs),
            created_at=proj.created_at.isoformat() if proj.created_at else "",
            updated_at=proj.updated_at.isoformat() if proj.updated_at else "",
        )


@router.get("/projects/{project_id}", response_model=ProjectItem)
async def get_project(project_id: str):
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        count = db.query(AutopilotDesign).filter_by(project_id=proj.id).count()
        return ProjectItem(
            id=proj.id,
            name=proj.name,
            base_dir=proj.base_dir,
            is_default=proj.is_default,
            is_active=getattr(proj, "is_active", False),
            design_count=count,
            created_at=proj.created_at.isoformat() if proj.created_at else "",
            updated_at=proj.updated_at.isoformat() if proj.updated_at else "",
            cost_total_usd=proj.cost_total_usd or 0.0,
            cost_limit_usd=proj.cost_limit_usd,
        )


@router.put("/projects/{project_id}", response_model=ProjectItem)
async def update_project(
    project_id: str,
    req: ProjectUpdate,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    # SECURITY: Verify agent authentication before allowing project modifications
    if not await verify_agent_authentication(agent_id):
        logger.warning(f"Unauthenticated project update attempt from agent {agent_id}")
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    from src.core.database import AutopilotDesign, AutopilotProject, Workflow, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")

        if req.name is not None:
            proj.name = req.name
        if req.base_dir is not None:
            resolved = _validate_base_dir(req.base_dir)
            proj.base_dir = resolved
        if req.is_default is not None:
            if req.is_default:
                db.query(AutopilotProject).update({"is_default": False})
            proj.is_default = req.is_default

        # Handle cost_limit_usd update
        if req.clear_cost_limit:
            proj.cost_limit_usd = None
        elif req.cost_limit_usd is not None:
            proj.cost_limit_usd = req.cost_limit_usd
        # else: leave unchanged (don't wipe on partial updates)

        db.flush()

        # Clear budget-paused workflows if limit raised or cleared
        if proj.cost_limit_usd is None or proj.cost_total_usd < proj.cost_limit_usd:
            budget_paused = (
                db.query(Workflow)
                .filter(
                    Workflow.project_id == project_id,
                    Workflow.paused_by == "budget",
                )
                .all()
            )
            for wf in budget_paused:
                wf.paused_by = None
                wf.status = "active"
                wf.status_reason = None
                wf.paused_at = None
            if budget_paused:
                db.flush()
                logger.info(f"Cleared budget pause on {len(budget_paused)} workflow(s) for project {project_id[:8]}")

        # Re-sync if base_dir changed (same session)
        if req.base_dir is not None:
            _sync_project_designs(proj.id, proj.base_dir, db)

        count = db.query(AutopilotDesign).filter_by(project_id=proj.id).count()
        _invalidate("queue", "status", f"project_designs:{project_id}")

        return ProjectItem(
            id=proj.id,
            name=proj.name,
            base_dir=proj.base_dir,
            is_default=proj.is_default,
            is_active=getattr(proj, "is_active", False),
            design_count=count,
            created_at=proj.created_at.isoformat() if proj.created_at else "",
            updated_at=proj.updated_at.isoformat() if proj.updated_at else "",
            cost_total_usd=proj.cost_total_usd or 0.0,
            cost_limit_usd=proj.cost_limit_usd,
        )


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: str,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    # SECURITY: Verify agent authentication before allowing project deletion
    if not await verify_agent_authentication(agent_id):
        logger.warning(f"Unauthenticated project deletion attempt from agent {agent_id}")
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    from src.core.database import AutopilotProject, get_db

    replacement_base_dir = None

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")

        was_active = getattr(proj, "is_active", False)
        db.delete(proj)
        db.flush()

        if was_active:
            next_proj = db.query(AutopilotProject).order_by(AutopilotProject.name).first()
            if next_proj:
                next_proj.is_active = True
                replacement_base_dir = next_proj.base_dir

    if replacement_base_dir:
        try:
            from types import SimpleNamespace

            from src.mcp.projects_api import _apply_active_project

            # Pass a plain object, not the ORM instance — its session is
            # already closed here, so touching an attribute on it would
            # raise DetachedInstanceError.
            _apply_active_project(SimpleNamespace(base_dir=replacement_base_dir))
        except Exception as e:
            logger.error(f"Failed to activate replacement project: {e}")

    _invalidate("queue", "status", f"project_designs:{project_id}")
    return {"deleted": project_id}


# ── Cost Entries ───────────────────────────────────────────────


@router.post("/cost-entries")
async def create_cost_entry(
    req: CostEntryCreate,
    request: Request,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Create a cost entry and trigger cost derivation rollup.

    Used by Pi extension (real-time) and external callers.
    Requires valid agent authentication via X-Agent-ID header.
    """
    # SECURITY: Verify agent authentication before allowing cost entry creation
    if not await verify_agent_authentication(agent_id):
        logger.warning(f"Unauthenticated cost entry attempt from agent {agent_id}")
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    # SECURITY: Rate limit by client IP, not X-Agent-ID. The header is
    # caller-supplied and several prefixes (sdk-*, mcp-*) are trusted
    # unconditionally by verify_agent_authentication, so a caller could
    # otherwise reset the rate-limit bucket on every request just by
    # rotating the header value. The server binds 0.0.0.0 (hephaestus_config.yaml),
    # so this endpoint is reachable beyond localhost.
    client_host = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"cost_entry:{client_host}", max_requests=60):
        logger.warning(f"Rate limit exceeded for cost entries from {client_host} (agent {agent_id})")
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 60 cost entries per minute.",
        )

    # SECURITY (ticket-5a75167a): verify_agent_authentication only checks
    # that agent_id names a real/trusted caller -- it never binds that
    # identity to the entry being written. A caller authenticated as one
    # real agent could otherwise supply a *different* agent_id in the body
    # and post a cost entry that impersonates another agent's task, which
    # src/services/cost_collection_service.py's real-time-suppression logic
    # (see ticket-9259f) treats as proof that task's own session reported in
    # real time -- permanently hiding its real JSONL-derived cost. System/
    # SDK identities (KNOWN_SYSTEM_AGENTS, sdk-*/mcp-* prefixes) have no
    # single agent to bind to and post cost entries on behalf of whichever
    # agent/task they're servicing, so only a real per-agent UUID identity
    # is bound here.
    if agent_id not in KNOWN_SYSTEM_AGENTS and not agent_id.startswith(("sdk-", "mcp-")):
        if req.agent_id and req.agent_id != agent_id:
            raise HTTPException(
                status_code=403,
                detail="agent_id does not match authenticated X-Agent-ID",
            )
        req.agent_id = agent_id

    from src.core.cost_derivation import record_cost
    from src.core.database import get_db

    with get_db() as db:
        entry = record_cost(
            db=db,
            cost_usd=req.cost_usd,
            source=req.source,
            task_id=req.task_id,
            agent_id=req.agent_id,
            workflow_id=req.workflow_id,
            model=req.model,
            input_tokens=req.input_tokens,
            output_tokens=req.output_tokens,
            cache_read_tokens=req.cache_read_tokens,
            cache_write_tokens=req.cache_write_tokens,
            reasoning_tokens=req.reasoning_tokens,
            raw_usage=req.raw_usage,
        )

        return {"id": entry.id, "cost_usd": entry.cost_usd}


# ── Cost Query Endpoints ──────────────────────────────────────


class CostEntrySummary(BaseModel):
    """Summary of a single cost entry."""

    id: str
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    workflow_id: Optional[str] = None
    source: str
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float
    recorded_at: Optional[str] = None


class TaskCostSummary(BaseModel):
    """Cost summary for a task."""

    task_id: str
    task_description: str
    cost_total_usd: float
    entries: List[CostEntrySummary]


class WorkflowCostSummary(BaseModel):
    """Cost summary for a workflow."""

    workflow_id: str
    workflow_name: str
    cost_total_usd: float
    tasks: List[TaskCostSummary]


class FeatureCostSummary(BaseModel):
    """Cost summary for a feature."""

    feature_id: str
    feature_name: str
    cost_total_usd: float
    workflows: List[WorkflowCostSummary]


class DesignCostSummary(BaseModel):
    """Cost summary for a design."""

    design_id: str
    design_name: str
    cost_total_usd: float
    features: List[FeatureCostSummary]


class ProjectCostSummary(BaseModel):
    """Cost summary for a project."""

    project_id: str
    project_name: str
    cost_total_usd: float
    cost_limit_usd: Optional[float] = None
    remaining_usd: Optional[float] = None
    is_over_budget: bool = False
    designs: List[DesignCostSummary]


@router.get("/tasks/{task_id}/costs", response_model=TaskCostSummary)
async def get_task_costs(
    task_id: str,
    request: Request,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Get cost breakdown for a single task.

    SECURITY: Requires valid agent authentication.
    Cost data is sensitive financial information.
    """
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )
    client_host = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"cost_query:{client_host}", max_requests=60):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 60 cost queries per minute.",
        )
    from src.core.cost_derivation import derive_task_cost
    from src.core.database import CostEntry, Task, get_db

    with get_db() as db:
        task = db.query(Task).filter_by(id=task_id).first()
        if not task:
            raise HTTPException(404, "Task not found")

        cost = derive_task_cost(db, task_id, write_back=False)
        entries = db.query(CostEntry).filter(CostEntry.task_id == task_id).order_by(CostEntry.recorded_at.desc()).limit(100).all()

        return TaskCostSummary(
            task_id=task.id,
            task_description=(task.raw_description or "")[:200],
            cost_total_usd=cost,
            entries=[
                CostEntrySummary(
                    id=e.id,
                    task_id=e.task_id,
                    agent_id=e.agent_id,
                    workflow_id=e.workflow_id,
                    source=e.source,
                    model=e.model,
                    input_tokens=e.input_tokens or 0,
                    output_tokens=e.output_tokens or 0,
                    cost_usd=e.cost_usd,
                    recorded_at=e.recorded_at.isoformat() if e.recorded_at else None,
                )
                for e in entries
            ],
        )


@router.get("/workflows/{workflow_id}/costs", response_model=WorkflowCostSummary)
async def get_workflow_costs(
    workflow_id: str,
    request: Request,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Get cost breakdown for a workflow.

    SECURITY: Requires valid agent authentication.
    Cost data is sensitive financial information.
    """
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )
    client_host = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"cost_query:{client_host}", max_requests=60):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 60 cost queries per minute.",
        )
    from src.core.cost_derivation import derive_workflow_cost
    from src.core.database import CostEntry, Task, Workflow, get_db

    with get_db() as db:
        workflow = db.query(Workflow).filter_by(id=workflow_id).first()
        if not workflow:
            raise HTTPException(404, "Workflow not found")

        cost = derive_workflow_cost(db, workflow_id, write_back=False)

        # Get tasks with costs
        tasks = db.query(Task).filter(Task.workflow_id == workflow_id).all()
        task_summaries = []
        for t in tasks:
            task_cost = db.query(sqlfunc.sum(CostEntry.cost_usd)).filter(CostEntry.task_id == t.id).scalar() or 0.0
            if task_cost > 0:
                task_summaries.append(
                    TaskCostSummary(
                        task_id=t.id,
                        task_description=(t.raw_description or "")[:200],
                        cost_total_usd=task_cost,
                        entries=[],
                    )
                )

        return WorkflowCostSummary(
            workflow_id=workflow.id,
            workflow_name=workflow.name or workflow.id[:8],
            cost_total_usd=cost,
            tasks=task_summaries,
        )


@router.get("/features/{feature_id}/costs", response_model=FeatureCostSummary)
async def get_feature_costs(
    feature_id: str,
    request: Request,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Get cost breakdown for a feature.

    SECURITY: Requires valid agent authentication.
    Cost data is sensitive financial information.
    """
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )
    client_host = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"cost_query:{client_host}", max_requests=60):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 60 cost queries per minute.",
        )
    from src.core.cost_derivation import derive_feature_cost, derive_workflow_cost
    from src.core.database import Feature, Workflow, get_db

    with get_db() as db:
        feature = db.query(Feature).filter_by(id=feature_id).first()
        if not feature:
            raise HTTPException(404, "Feature not found")

        cost = derive_feature_cost(db, feature_id, write_back=False)

        # Get workflows for this feature
        workflows = db.query(Workflow).filter(Workflow.feature_id == feature_id).all()
        workflow_summaries = []
        for w in workflows:
            wf_cost = derive_workflow_cost(db, w.id, write_back=False)
            if wf_cost > 0:
                workflow_summaries.append(
                    WorkflowCostSummary(
                        workflow_id=w.id,
                        workflow_name=w.name or w.id[:8],
                        cost_total_usd=wf_cost,
                        tasks=[],
                    )
                )

        return FeatureCostSummary(
            feature_id=feature.id,
            feature_name=feature.name or feature.feature_key,
            cost_total_usd=cost,
            workflows=workflow_summaries,
        )


@router.get("/designs/{design_id}/costs", response_model=DesignCostSummary)
async def get_design_costs(
    design_id: str,
    request: Request,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Get cost breakdown for a design.

    SECURITY: Requires valid agent authentication.
    Cost data is sensitive financial information.
    """
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )
    client_host = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"cost_query:{client_host}", max_requests=60):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 60 cost queries per minute.",
        )
    from src.core.cost_derivation import derive_design_cost, derive_feature_cost
    from src.core.database import AutopilotDesign, Feature, get_db

    with get_db() as db:
        design = db.query(AutopilotDesign).filter_by(id=design_id).first()
        if not design:
            raise HTTPException(404, "Design not found")

        cost = derive_design_cost(db, design_id, write_back=False)

        # Get features for this design
        features = db.query(Feature).filter(Feature.design_id == design_id).all()
        feature_summaries = []
        for feat in features:
            feat_cost = derive_feature_cost(db, feat.id, write_back=False)
            if feat_cost > 0:
                feature_summaries.append(
                    FeatureCostSummary(
                        feature_id=feat.id,
                        feature_name=feat.name or feat.feature_key,
                        cost_total_usd=feat_cost,
                        workflows=[],
                    )
                )

        return DesignCostSummary(
            design_id=design.id,
            design_name=design.name or design.filename,
            cost_total_usd=cost,
            features=feature_summaries,
        )


@router.get("/projects/{project_id}/costs", response_model=ProjectCostSummary)
async def get_project_costs(
    project_id: str,
    request: Request,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Get cost breakdown for a project.

    SECURITY: Requires valid agent authentication.
    Cost data is sensitive financial information.
    """
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )
    client_host = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"cost_query:{client_host}", max_requests=60):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 60 cost queries per minute.",
        )
    from src.core.cost_derivation import derive_design_cost, derive_project_cost
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        project = db.query(AutopilotProject).filter_by(id=project_id).first()
        if not project:
            raise HTTPException(404, "Project not found")

        cost = derive_project_cost(db, project_id, write_back=False)

        # Get designs for this project
        designs = db.query(AutopilotDesign).filter(AutopilotDesign.project_id == project_id).all()
        design_summaries = []
        for d in designs:
            d_cost = derive_design_cost(db, d.id, write_back=False)
            if d_cost > 0:
                design_summaries.append(
                    DesignCostSummary(
                        design_id=d.id,
                        design_name=d.name or d.filename,
                        cost_total_usd=d_cost,
                        features=[],
                    )
                )

        remaining = None
        is_over = False
        if project.cost_limit_usd is not None:
            remaining = max(0.0, project.cost_limit_usd - cost)
            is_over = cost >= project.cost_limit_usd

        return ProjectCostSummary(
            project_id=project.id,
            project_name=project.name,
            cost_total_usd=cost,
            cost_limit_usd=project.cost_limit_usd,
            remaining_usd=remaining,
            is_over_budget=is_over,
            designs=design_summaries,
        )


# ── Project Designs (sync + CRUD) ──────────────────────────────


@router.post("/projects/{project_id}/sync", response_model=List[DesignItem])
async def sync_project_designs(project_id: str):
    from src.core.database import AutopilotProject, get_db

    lock = await _get_project_lock(project_id)
    async with lock:
        with get_db() as db:
            proj = db.query(AutopilotProject).get(project_id)
            if not proj:
                raise HTTPException(404, "Project not found")

            designs = _sync_project_designs(project_id, proj.base_dir, db)

        _invalidate("queue", "status", f"project_designs:{project_id}")
        return [DesignItem(**d) for d in designs]


@router.post("/projects/{project_id}/designs/reload", response_model=List[DesignItem])
async def reload_project_designs(project_id: str):
    """Force resync designs from filesystem."""
    from src.core.database import AutopilotProject, get_db

    cache_key = f"project_designs:{project_id}"
    _invalidate(cache_key)
    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        _sync_project_designs(project_id, proj.base_dir, db)
    # Now fetch fresh
    return await list_project_designs(project_id)


@router.get("/projects/{project_id}/designs", response_model=List[DesignItem])
async def list_project_designs(project_id: str):
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    cache_key = f"project_designs:{project_id}"
    cached = _cached(cache_key)
    if cached is not None:
        return cached

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")

        designs = db.query(AutopilotDesign).filter_by(project_id=project_id).order_by(AutopilotDesign.ordinal).all()
        result = [
            DesignItem(
                id=d.id,
                filename=d.filename,
                name=d.name,
                ordinal=d.ordinal,
                size_bytes=d.size_bytes,
                extension=d.extension,
                modified_at=d.modified_at.isoformat() if d.modified_at else None,
            )
            for d in designs
        ]
        return _store(cache_key, result)


@router.post("/projects/{project_id}/designs", response_model=DesignItem)
async def add_project_design(project_id: str, req: DesignAddRequest):
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir

    # Store in .hephaestus/designs/ (not git-tracked) so git commits
    # don't delete design files.
    design_dir = Path(base_dir) / DESIGN_CONTEXT_SUBDIR
    design_dir.mkdir(parents=True, exist_ok=True)

    ext = req.extension if req.extension in ALLOWED_EXTENSIONS else ".md"
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in req.name)
    safe_name = safe_name.strip().replace(" ", "_")
    if not safe_name:
        raise HTTPException(400, "Invalid design name")
    filename = f"{safe_name}{ext}"
    filepath = _safe_path(str(design_dir), filename)

    if filepath.exists():
        raise HTTPException(409, f"Design '{filename}' already exists")

    filepath.write_text(req.content)
    stat = filepath.stat()

    design_id = _design_id(project_id, filename)

    with get_db() as db:
        max_ord = db.query(AutopilotDesign).filter_by(project_id=project_id).count()
        d = AutopilotDesign(
            id=design_id,
            project_id=project_id,
            filename=filename,
            name=req.name,
            ordinal=max_ord + 1,
            size_bytes=stat.st_size,
            extension=ext,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )
        db.add(d)

    _invalidate("queue", "status", f"project_designs:{project_id}")
    return DesignItem(
        id=design_id,
        filename=filename,
        name=req.name,
        ordinal=max_ord + 1,
        size_bytes=stat.st_size,
        extension=ext,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    )


class BrowseEntry(BaseModel):
    name: str
    path: str
    type: str  # "dir" or "file"


class BrowseResult(BaseModel):
    path: str
    parent: Optional[str] = None
    entries: List[BrowseEntry]


@router.get("/projects/{project_id}/browse", response_model=BrowseResult)
async def browse_project_files(project_id: str, path: str = Query("")):
    """List directories and .md/.txt files under a project's base_dir.

    `path` is relative to base_dir; traversal above base_dir is rejected
    by `_safe_path`.
    """
    from src.core.database import AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir

    base_resolved = Path(base_dir).resolve()
    target = _safe_path(base_dir, path) if path else base_resolved
    if not target.is_dir():
        raise HTTPException(400, "Not a directory")

    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if child.name.startswith("."):
            continue
        try:
            rel = str(child.resolve().relative_to(base_resolved))
        except ValueError:
            # Symlink resolves outside base_dir -- skip rather than leak/crash.
            continue
        if child.is_dir():
            entries.append(BrowseEntry(name=child.name, path=rel, type="dir"))
        elif child.suffix in ALLOWED_EXTENSIONS:
            entries.append(BrowseEntry(name=child.name, path=rel, type="file"))

    rel_path = "" if target == base_resolved else str(target.relative_to(base_resolved))
    parent = None
    if rel_path:
        parent_path = str(Path(rel_path).parent)
        parent = "" if parent_path == "." else parent_path

    return BrowseResult(path=rel_path, parent=parent, entries=entries)


@router.get("/projects/{project_id}/browse/content")
async def browse_project_file_content(project_id: str, path: str = Query(...)):
    """Read the content of a .md/.txt file under a project's base_dir."""
    from src.core.database import AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir

    target = _safe_path(base_dir, path)
    if not target.is_file() or target.suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "Invalid file")

    return {
        "name": target.name,
        "content": target.read_text(errors="replace"),
        "size_bytes": target.stat().st_size,
    }


@router.put("/projects/{project_id}/designs/reorder")
async def reorder_project_designs(project_id: str, req: DesignReorderRequest):
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        designs = db.query(AutopilotDesign).filter_by(project_id=project_id).all()
        by_id = {d.id: d for d in designs}

        for i, design_id in enumerate(req.design_ids):
            if design_id not in by_id:
                raise HTTPException(400, f"Unknown design id: {design_id}")
            by_id[design_id].ordinal = i + 1

        # Also save order to file for orchestrator to read
        project = db.query(AutopilotProject).get(project_id)
        if project:
            hephaestus_dir = Path(project.base_dir) / CONTEXT_DIR_NAME
            hephaestus_dir.mkdir(parents=True, exist_ok=True)
            order_file = hephaestus_dir / ".queue_order.json"
            # Map design_ids back to filenames
            ordered_filenames = [by_id[did].filename for did in req.design_ids]
            order_file.write_text(json.dumps(ordered_filenames))

    _invalidate("queue", f"project_designs:{project_id}")
    return {"order": req.design_ids}


@router.delete("/projects/{project_id}/designs/{filename}")
async def remove_project_design(project_id: str, filename: str):
    logger.info(f"[DELETE] remove_project_design called: project={project_id}, file={filename}")
    from src.core.database import (
        Agent,
        AgentResult,
        AutopilotDesign,
        AutopilotProject,
        BoardConfig,
        CostEntry,
        DiagnosticRun,
        Feature,
        Memory,
        Phase,
        PhaseExecution,
        Task,
        TaskPromptOverride,
        Ticket,
        ValidationReview,
        Workflow,
        WorkflowResult,
        get_db,
    )

    # Delete DB record first, then file (atomic rollback if file delete fails)
    found = False
    worktrees_to_clean: List[Tuple[str, dict]] = []
    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir

        d = db.query(AutopilotDesign).filter_by(project_id=project_id, filename=filename).first()
        if d:
            # Cascade: terminate agents, delete tasks, workflows, features
            design_features = db.query(Feature).filter_by(design_id=d.id).all()
            wf_ids = []
            for feat in design_features:
                if feat.workflow_id:
                    wf_ids.append(feat.workflow_id)
            # Also get workflows directly linked to the design
            design_wfs = db.query(Workflow).filter_by(design_id=d.id).all()
            for wf in design_wfs:
                if wf.id not in wf_ids:
                    wf_ids.append(wf.id)

            # Fallback: catch orphaned phase0/feature workflows whose design_id
            # link never got set (observed live: Workflow.design_id ended up
            # NULL for a completed Phase 0 run + its first feature workflow,
            # so neither of the two lookups above found them, and they survived
            # a delete of the design that spawned them). Match by launch_params
            # instead, the same way _relink_features_to_workflows already does
            # for Feature.workflow_id.
            orphan_candidates = (
                db.query(Workflow)
                .filter(
                    Workflow.design_id.is_(None),
                    Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS),
                )
                .all()
            )
            for wf in orphan_candidates:
                if wf.id in wf_ids:
                    continue
                try:
                    params = wf.launch_params if isinstance(wf.launch_params, dict) else json.loads(wf.launch_params or "{}")
                except Exception:
                    continue
                if params.get("design_id") == d.id or Path(params.get("design_document", "")).name == filename:
                    wf_ids.append(wf.id)

            if wf_ids:
                # Terminate active agents for these workflows
                tasks = db.query(Task).filter(Task.workflow_id.in_(wf_ids)).all()
                task_ids = [t.id for t in tasks]
                if task_ids:
                    agents = db.query(Agent).filter(Agent.current_task_id.in_(task_ids)).filter(Agent.status.in_(["working", "starting", "idle"])).all()
                    for agent in agents:
                        try:
                            subprocess.run(
                                ["tmux", "kill-session", "-t", agent.tmux_session_name],
                                capture_output=True,
                                timeout=3,
                            )
                        except Exception:
                            pass
                        agent.status = "terminated"
                        agent.current_task_id = None
                        agent.terminated_at = datetime.utcnow()

                # Delete dependent records (order matters for FK constraints)
                if task_ids:
                    db.query(TaskPromptOverride).filter(TaskPromptOverride.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(ValidationReview).filter(ValidationReview.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(AgentResult).filter(AgentResult.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(Memory).filter(Memory.related_task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(Ticket).filter(Ticket.task_id.in_(task_ids)).delete(synchronize_session=False)
                    # CostEntry.task_id/workflow_id are also enforced FKs -- a
                    # workflow that ever recorded real LLM cost (the common
                    # case now that cost tracking exists) would otherwise
                    # fail this delete with an IntegrityError.
                    db.query(CostEntry).filter(CostEntry.task_id.in_(task_ids)).delete(synchronize_session=False)

                # Delete workflow-level dependents
                db.query(DiagnosticRun).filter(DiagnosticRun.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(WorkflowResult).filter(WorkflowResult.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(BoardConfig).filter(BoardConfig.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(Ticket).filter(Ticket.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(CostEntry).filter(CostEntry.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

                # Collect worktree info before the Workflow rows are gone --
                # otherwise these directories orphan permanently: they're
                # deterministic per-feature paths (_create_integration_worktree),
                # and nothing else will ever find them once the DB row
                # pointing at one no longer exists -- not even the startup
                # completion-worktree sweep, which only looks at "completed"
                # workflows.
                for wf in db.query(Workflow).filter(Workflow.id.in_(wf_ids)).all():
                    if wf.working_directory and ".worktrees/" in wf.working_directory:
                        lp = wf.launch_params if isinstance(wf.launch_params, dict) else {}
                        worktrees_to_clean.append((wf.working_directory, lp))

                # Delete tasks -- must happen before Phase/PhaseExecution
                # below: Task.phase_id is a FK to phases.id, so deleting
                # Phase rows first (as an earlier version of this fix did)
                # fails with the same FOREIGN KEY error, just one table over.
                db.query(Task).filter(Task.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

                # Delete phase executions -- PhaseExecution links to a
                # workflow via phase_id -> Phase.workflow_id, not the
                # workflow_execution_id column (an unused legacy field
                # that's never actually populated with a workflow id, so
                # filtering on it matched zero rows and left every
                # PhaseExecution -- and the Phase rows below -- behind).
                phase_ids = [p.id for p in db.query(Phase.id).filter(Phase.workflow_id.in_(wf_ids)).all()]
                if phase_ids:
                    db.query(PhaseExecution).filter(PhaseExecution.phase_id.in_(phase_ids)).delete(synchronize_session=False)

                # Delete phases -- Phase.workflow_id is a NOT NULL FK to
                # workflows.id, so leaving these behind (as this function
                # always did) made the Workflow delete below fail with a
                # FOREIGN KEY constraint error every time.
                db.query(Phase).filter(Phase.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

                # Delete workflows
                db.query(Workflow).filter(Workflow.id.in_(wf_ids)).delete(synchronize_session=False)

            # Delete features
            db.query(Feature).filter_by(design_id=d.id).delete(synchronize_session=False)

            # Delete the design itself
            db.delete(d)
            found = True

    # Best-effort worktree cleanup, now that the DB transaction above has
    # committed -- not fatal if any single one can't be resolved.
    for working_directory, launch_params in worktrees_to_clean:
        try:
            wt_path = Path(working_directory)
            if not (wt_path / ".git").exists():
                continue
            project_path_str = launch_params.get("project_path")
            if not project_path_str:
                logger.warning(
                    f"[DELETE-DESIGN] {wt_path} has no launch_params.project_path "
                    "to scope cleanup to -- left in place"
                )
                continue
            import git as _git

            from src.autopilot.orchestrator import _cleanup_worktree

            try:
                branch = _git.Repo(wt_path).active_branch.name
            except Exception:
                branch = ""
            _cleanup_worktree(wt_path, branch, Path(project_path_str), logger)
        except Exception as e:
            logger.warning(f"[DELETE-DESIGN] Failed to clean up worktree {working_directory}: {e}")

    design_dir = _get_design_queue_dir(base_dir)
    filepath = _safe_path(str(design_dir), filename)
    if filepath.exists():
        filepath.unlink()
        found = True

    if not found:
        raise HTTPException(404, f"Design '{filename}' not found")

    _invalidate("queue", "status", f"project_designs:{project_id}")

    # Also remove from the persisted processed-designs set so re-adding
    # triggers reprocessing
    try:
        import hashlib

        from src.autopilot.orchestrator import PersistentPipelineState

        # Compute hash of the design file to remove it
        if filepath.exists():
            content = filepath.read_bytes()
        else:
            # File already deleted, try to compute from remaining data
            content = filename.encode()
        h = hashlib.sha256(content).hexdigest()[:16]

        PersistentPipelineState(project_id=project_id).remove_processed_hash(h)
    except Exception:
        pass  # Non-critical

    return {"removed": filename}


@router.get("/projects/{project_id}/designs/{filename}/content")
async def get_project_design_content(project_id: str, filename: str):
    from src.core.database import AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir

    design_dir = _get_design_queue_dir(base_dir)
    # Validate filename doesn't contain path traversal
    if ".." in filename or "/" in filename:
        raise HTTPException(400, "Invalid filename")
    filepath = design_dir / filename
    if not filepath.exists():
        raise HTTPException(404, f"Design '{filename}' not found")
    return {"filename": filename, "content": filepath.read_text(errors="replace")}


@router.get("/projects/{project_id}/designs/{filename}/status")
async def get_project_design_status(project_id: str, filename: str):
    """Get full status for a design: workflow, tasks, branch, feature folder."""
    from src.core.database import (
        Agent,
        AgentBranch,
        AutopilotDesign,
        AutopilotProject,
        Feature,
        Phase,
        Task,
        Workflow,
        get_db,
    )

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir

    design_dir = _get_design_queue_dir(base_dir)
    if ".." in filename or "/" in filename:
        raise HTTPException(400, "Invalid filename")
    filepath = design_dir / filename
    if not filepath.exists():
        raise HTTPException(404, f"Design '{filename}' not found")

    design_content = filepath.read_text(errors="replace")
    design_name = filepath.stem.replace("_", " ").replace("-", " ")

    # Find all workflows that processed this design
    with get_db() as db:
        # Use LIKE query for efficiency instead of loading all workflows
        matching_workflows = (
            db.query(Workflow)
            .filter(
                Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS),
                Workflow.launch_params.like(f"%{filename}%"),
            )
            .order_by(Workflow.created_at.desc())
            .all()
        )

        # Self-heal each matched workflow's own status before using it below --
        # derive_workflow_status is the centralized "did every phase actually
        # finish" check (unlike the coarse task-status heuristics further
        # down this endpoint), so a workflow that got marked "completed"
        # prematurely (e.g. a goto-limit-exceeded forced "continue" that
        # skipped starting the next phase) gets corrected back to "active"
        # here on every poll, the same way Feature/Design status already
        # self-heal.
        from src.core.status_derivation import derive_workflow_status

        for wf in matching_workflows:
            derive_workflow_status(db, wf.id, write_back=True)

        # Get tasks and agents for all matching workflows
        all_tasks = []
        all_agents = []
        workflow_ids = [wf.id for wf in matching_workflows]

        # Build phase name lookup
        phase_map = {}
        if workflow_ids:
            phases = db.query(Phase).filter(Phase.workflow_id.in_(workflow_ids)).all()
            phase_map = {p.id: p.name for p in phases}

        if workflow_ids:
            tasks = db.query(Task).filter(Task.workflow_id.in_(workflow_ids)).order_by(Task.created_at).all()

            # Bulk-fetch agents to avoid N+1
            agent_ids = list(set(t.assigned_agent_id for t in tasks if t.assigned_agent_id))
            agents_map = {}
            if agent_ids:
                agents_list = db.query(Agent).filter(Agent.id.in_(agent_ids)).all()
                agents_map = {a.id: a for a in agents_list}

            for t in tasks:
                agent = agents_map.get(t.assigned_agent_id) if t.assigned_agent_id else None
                all_tasks.append(
                    {
                        "id": t.id,
                        "description": (t.enriched_description or t.raw_description or "")[:200],
                        "status": t.status,
                        "priority": t.priority,
                        "phase_id": t.phase_id,
                        "phase_name": phase_map.get(t.phase_id),
                        "workflow_id": t.workflow_id,
                        "created_at": t.created_at.isoformat() if t.created_at else None,
                        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                        "agent_id": t.assigned_agent_id,
                        "agent_status": agent.status if agent else None,
                        "cost_total_usd": t.cost_total_usd or 0.0,
                    }
                )

            # Get agent IDs for branch info - check both task.assigned_agent_id and agents.current_task_id
            agent_ids = list(set(t.assigned_agent_id for t in tasks if t.assigned_agent_id))
            # Also get agents assigned to these tasks via agents.current_task_id
            task_ids = [t.id for t in tasks]
            if task_ids:
                assigned_agents = db.query(Agent).filter(Agent.current_task_id.in_(task_ids)).all()
                for a in assigned_agents:
                    if a.id not in agent_ids:
                        agent_ids.append(a.id)

            if agent_ids:
                worktrees = db.query(AgentBranch).filter(AgentBranch.agent_id.in_(agent_ids)).all()
                for wt in worktrees:
                    all_agents.append(
                        {
                            "agent_id": wt.agent_id,
                            "branch_name": wt.branch_name,
                            "status": wt.merge_status,
                        }
                    )

            # Also include full agent details (not just branch info)
            for agent_id in agent_ids:
                agent = agents_map.get(agent_id)
                if agent:
                    # Avoid duplicates
                    if not any(a.get("agent_id") == agent.id for a in all_agents):
                        all_agents.append(
                            {
                                "agent_id": agent.id,
                                "status": agent.status,
                                "current_task_id": agent.current_task_id,
                                "last_activity": agent.last_activity.isoformat() if agent.last_activity else None,
                                "cli_model": agent.cli_model,
                                "agent_type": agent.agent_type,
                            }
                        )

        # Determine overall status — prefer the design-level status from
        # autopilot_designs (set by run_design_aggregate / continuous pipeline)
        # over workflow-level heuristics, because workflow statuses may include
        # retries, gotos, or partial failures that don't reflect final outcome.
        _design_id = None
        _design_raw_error = None
        with get_db() as _db:
            _design = _db.query(AutopilotDesign).filter_by(project_id=project_id, filename=filename).first()
            if _design:
                from src.core.status_derivation import derive_design_status

                # H-3: use the centralized, self-healing derivation (feature
                # rollup) instead of the raw column, which is only ever
                # written by run_design_aggregate at the very end of a run.
                design_status = derive_design_status(_db, _design.id, write_back=True)
                _design_id = _design.id
                _design_raw_error = _design.error
            else:
                design_status = None

        # A live 'active'/'paused' workflow signal must win over the coarser
        # design_status field — that field is only updated by run_design_aggregate
        # at the end of a full pipeline run, so it never reflects a workflow
        # being paused mid-run. Without this, design_status stays 'active'
        # forever after a pause, the pause/resume button never flips to
        # 'resume', and clicking pause looks like it did nothing.
        #
        # BUT matching_workflows is deliberately broad (LIKE-matched on the
        # bare design filename), so it also catches every OTHER feature's
        # workflow that happened to originate from the same design document
        # -- a design gets re-run once per decomposed feature, and each
        # feature's own workflow references the same design_document path
        # in its launch_params. A workflow whose OWN linked Feature has
        # already reached completed/skipped is not a live in-flight run no
        # matter what its own (potentially stale, never-cleaned-up)
        # Workflow.status says -- trusting it here made the WHOLE design
        # look permanently "Active". Observed live: BACKEND_DESIGN.md's
        # Credit Management System feature completed 2026-07-29 but its
        # workflow (f1b3c0e0) never got its status flipped from "active",
        # so every later feature's design-status view showed a permanent
        # spinner even after the design (and every feature) genuinely
        # finished.
        _feature_status_by_wf = {}
        _wf_ids_for_feature_check = [wf.id for wf in matching_workflows]
        if _wf_ids_for_feature_check:
            with get_db() as _db:
                for feat in _db.query(Feature).filter(Feature.workflow_id.in_(_wf_ids_for_feature_check)).all():
                    _feature_status_by_wf[feat.workflow_id] = feat.status
        _wf_statuses = [
            wf.status
            for wf in matching_workflows
            if _feature_status_by_wf.get(wf.id) not in ("completed", "skipped")
        ]
        if any(s == "active" for s in _wf_statuses):
            overall_status = "active"
        elif _wf_statuses and any(s == "paused" for s in _wf_statuses):
            overall_status = "paused"
        elif design_status and design_status not in ("pending", "unknown"):
            overall_status = design_status
        elif not matching_workflows:
            overall_status = "pending"
        else:
            if all(s == "completed" for s in _wf_statuses):
                overall_status = "completed"
            elif any(s == "failed" for s in _wf_statuses):
                overall_status = "failed"
            else:
                overall_status = _wf_statuses[0] if _wf_statuses else "unknown"

        # Only surface the stored error while the design is actually
        # failed -- _design.error isn't cleared when a design is re-run
        # successfully (or reset to pending), so showing it unconditionally
        # would leak a stale message from a previous failed attempt onto a
        # design that's since recovered.
        design_error = _design_raw_error if overall_status == "failed" else None

        # Surface *why* a paused workflow is paused -- "paused" alone is
        # ambiguous between a user-initiated pause and a budget-enforcement
        # pause, and the latter is the one users most need to notice.
        design_paused_by = None
        design_status_reason = None
        if overall_status == "paused":
            paused_wf = next((wf for wf in matching_workflows if wf.status == "paused" and wf.paused_by), None)
            if paused_wf:
                design_paused_by = paused_wf.paused_by
                design_status_reason = paused_wf.status_reason

        # Find feature folder
        feature_folder = None
        for wf in matching_workflows:
            if wf.working_directory:
                features_dir = Path(wf.working_directory) / CONTEXT_DIR_NAME / "features"
                if features_dir.exists():
                    for d in sorted(features_dir.iterdir(), reverse=True):
                        if d.is_dir() and filename.replace(".md", "").lower() in d.name.lower():
                            feature_folder = str(d)
                            break
                if feature_folder:
                    break

        # Get branch names
        branch_names = list(set(a["branch_name"] for a in all_agents if a.get("branch_name")))

        # Get features linked to this design's workflows
        workflow_ids = [wf.id for wf in matching_workflows]
        features = []

        # Query decomposed features from the DB (created by Phase 0)
        if _design_id:
            db_features = db.query(Feature).filter_by(design_id=_design_id).all()
        else:
            db_features = []

        for feat in db_features:
            # Get tasks for this feature's workflow
            feat_tasks = []
            feat_wf_id = feat.workflow_id

            # If no workflow_id, try to match by feature_key in launch_params
            if not feat_wf_id and matching_workflows:
                import json as _json

                for wf in matching_workflows:
                    try:
                        params = wf.launch_params if isinstance(wf.launch_params, dict) else _json.loads(wf.launch_params or "{}")
                    except Exception:
                        continue
                    if params.get("feature_id") == feat.feature_key:
                        feat_wf_id = wf.id
                        break

            if feat_wf_id:
                wf_tasks = db.query(Task).filter_by(workflow_id=feat_wf_id).all()
                phase_ids = set(t.phase_id for t in wf_tasks if t.phase_id)
                phases_q = db.query(Phase).filter(Phase.id.in_(phase_ids)).all() if phase_ids else []
                phase_map = {p.id: p.name for p in phases_q}
                # Phase.description is config-sourced (each phase YAML's own
                # `description:`) -- exposed per task so the UI can show what
                # the phase actually does without re-deriving it from the
                # task's own free-text description (which also carries the
                # "Execute {phase}: " label and, for goto/retry tasks, the
                # GOTO_REASON_PREFIX block below).
                phase_description_map = {p.id: p.description for p in phases_q}

                for t in wf_tasks:
                    agent_status = None
                    if t.assigned_agent_id:
                        agent = db.query(Agent).filter_by(id=t.assigned_agent_id).first()
                        agent_status = agent.status if agent else None
                    # The full (untruncated) text -- goto_reason is parsed
                    # out of this, not the 200-char-truncated `description`
                    # below, since a long phase description could otherwise
                    # push the reason past the truncation point.
                    full_description = t.enriched_description or t.raw_description or ""
                    goto_reason = None
                    if GOTO_REASON_PREFIX in full_description:
                        goto_reason = full_description.split(GOTO_REASON_PREFIX, 1)[1].split("\n", 1)[0].strip()
                    feat_tasks.append(
                        {
                            "id": t.id,
                            "description": full_description[:200],
                            "phase_description": phase_description_map.get(t.phase_id),
                            "goto_reason": goto_reason,
                            # Once the task is finished, its own outcome is more
                            # useful to show than goto_reason/phase_description
                            # (both describe why the task was dispatched, not
                            # what it actually did) -- the frontend prefers
                            # these when status is done/failed.
                            "completion_notes": t.completion_notes,
                            "failure_reason": t.failure_reason,
                            "status": t.status,
                            "action": t.action or "",
                            "action_target_phase": t.action_target_phase or None,
                            "phase_id": t.phase_id,
                            "phase_name": phase_map.get(t.phase_id),
                            "workflow_id": t.workflow_id,
                            "created_at": t.created_at.isoformat() if t.created_at else None,
                            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                            "agent_id": t.assigned_agent_id,
                            "agent_status": agent_status,
                            "cost_total_usd": t.cost_total_usd or 0.0,
                        }
                    )

            # Use centralized status derivation (H-3 fix)
            from src.core.status_derivation import derive_feature_status

            feat_status = derive_feature_status(db, feat.id, write_back=True)

            # doc_review.yaml's feature_report.html shows up here as soon as
            # that phase writes it -- PhaseManager._populate_feature_folder
            # only archives a copy to the features gallery at FULL workflow
            # completion (2 phases later), so checking the live worktree is
            # what lets the report surface right after doc_review finishes
            # instead of only once the whole 12-phase pipeline is done.
            has_report = False
            if feat_wf_id:
                feat_wf = next((wf for wf in matching_workflows if wf.id == feat_wf_id), None)
                # Only show report if doc_review phase has completed
                # (prevents showing stale reports from previous runs)
                from src.core.database import Phase as _Phase
                doc_review_phase = db.query(_Phase).filter_by(
                    workflow_id=feat_wf_id, name="doc_review"
                ).first()
                if doc_review_phase:
                    doc_review_done = db.query(Task).filter(
                        Task.phase_id == doc_review_phase.id,
                        Task.status == "done",
                    ).first()
                    if doc_review_done:
                        if feat_wf and feat_wf.working_directory:
                            has_report = (Path(feat_wf.working_directory) / CONTEXT_DIR_NAME / "feature_report.html").is_file() or \
                                         (Path(feat_wf.working_directory) / "docs" / "feature_report.html").is_file()
                        if not has_report:
                            # working_directory is null/gone once the feature's
                            # worktree is cleaned up on full completion (see
                            # _cleanup_worktree) -- the live-worktree checks
                            # above go permanently False at that point even
                            # though PhaseManager._populate_feature_folder
                            # already archived a durable copy to the features
                            # gallery first.
                            has_report = _find_archived_feature_report(base_dir, feat_wf_id) is not None

            features.append(
                {
                    "id": feat.id,
                    "name": feat.name,
                    "feature_key": feat.feature_key,
                    "workflow_id": feat.workflow_id,
                    "status": feat_status,
                    "scope": feat.scope or "",
                    "tasks": feat_tasks,
                    "depends_on": feat.depends_on or [],
                    "created_at": feat.created_at.isoformat() if feat.created_at else None,
                    "completed_at": feat.completed_at.isoformat() if feat.completed_at else None,
                    "has_report": has_report,
                    "cost_total_usd": feat.cost_total_usd or 0.0,
                    "pr_url": _extract_pr_url(db, feat_wf_id, phase_map) if feat_wf_id else None,
                    # Review mode fields
                    "review_pending": (
                        feat_wf_id is not None
                        and any(
                            wf.id == feat_wf_id and wf.paused_by == "review"
                            for wf in matching_workflows
                        )
                    ),
                    "review_status": getattr(feat, "review_status", None),
                    "review_feedback": getattr(feat, "review_feedback", None),
                }
            )

        # Feature Architect (Phase 0) pseudo-feature: it decomposes the design
        # into the Feature rows above, but is itself a separate Workflow (see
        # docs/LOOP_ENGINEERING_REVIEW.md -- a Feature:Workflow is 1:1, so
        # Phase 0 can't be phase order=0 within one of them; it must be its
        # own workflow that runs BEFORE those exist). That made it invisible
        # here: nothing surfaced its live task/agent while it was running, so
        # this list only ever showed a static "pending" placeholder or the
        # real decomposed features, with no way to watch Phase 0 itself.
        # Build a feature-shaped entry from its actual task/agent data (using
        # the same shape as real features above) so FeatureRow renders it
        # identically -- including the clickable agent-id link per task.
        phase0_workflows = [wf for wf in matching_workflows if wf.definition_id in PHASE0_DEFINITION_IDS]
        if phase0_workflows:
            phase0_wf = phase0_workflows[0]  # most recent (matching_workflows is desc-ordered)
            phase0_tasks = [t for t in all_tasks if t["workflow_id"] == phase0_wf.id]
            if phase0_tasks:
                # Paused-for-review wins over the task-derived status: every
                # task is genuinely "done" at this point (decomposition +
                # review both finished), which would otherwise read as
                # "completed" -- indistinguishable from a design that
                # skipped review entirely. Mirrors how a real Feature row's
                # status is set to "paused" by _pause_feature_for_review.
                if phase0_wf.paused_by == "review":
                    phase0_status = "paused"
                else:
                    phase0_status = (
                        "completed"
                        if all(t["status"] == "done" for t in phase0_tasks)
                        else "failed"
                        if any(t["status"] == "failed" for t in phase0_tasks)
                        else "active"
                        if any(t["status"] in ("assigned", "in_progress") for t in phase0_tasks)
                        else "pending"
                    )

                # has_report: the feature_review phase's HTML decomposition
                # synopsis. Check the live worktree first (still present
                # while paused for review -- Phase 0's own worktree isn't
                # cleaned up until AFTER the review gate clears), then the
                # design's durably-persisted designs_folder archive (see
                # run_phase0's synopsis_src copy) once it's gone.
                phase0_has_report = False
                if phase0_wf.working_directory:
                    phase0_has_report = (Path(phase0_wf.working_directory) / CONTEXT_DIR_NAME / "feature_report.html").is_file()
                if not phase0_has_report:
                    phase0_design = db.query(AutopilotDesign).filter_by(project_id=project_id, filename=filename).first()
                    if phase0_design and phase0_design.designs_folder:
                        phase0_has_report = (Path(phase0_design.designs_folder) / "feature_report.html").is_file()

                features.insert(
                    0,
                    {
                        "id": f"phase0-{phase0_wf.id}",
                        "name": "Feature Architect",
                        "feature_key": "phase-0-decomposition",
                        "workflow_id": phase0_wf.id,
                        "status": phase0_status,
                        "scope": "Decomposes the design into the feature(s) below",
                        "tasks": phase0_tasks,
                        "created_at": phase0_wf.created_at.isoformat() if phase0_wf.created_at else None,
                        "completed_at": None,
                        "cost_total_usd": phase0_wf.cost_total_usd or 0.0,
                        "has_report": phase0_has_report,
                        "review_pending": phase0_wf.paused_by == "review",
                        "review_status": None,
                        "review_feedback": None,
                    },
                )

        # Placeholder: if no DB features yet (and no Phase 0 activity to show
        # either), show a single pending feature so the UI has something to
        # display while waiting for Phase 0 to even start.
        if not features:
            features.append(
                {
                    "id": f"placeholder-{filename}",
                    "name": design_name or filename.replace(".md", ""),
                    "feature_key": "pending-decomposition",
                    "status": "pending",
                    "scope": "Awaiting Phase 0 decomposition",
                    "tasks": [],
                    "created_at": None,
                    "completed_at": None,
                    "cost_total_usd": 0.0,
                }
            )

        # Collect workflow-level errors for failed workflows
        workflow_errors = []
        for wf in matching_workflows:
            if wf.status == "failed":
                wf_tasks = [t for t in all_tasks if t.get("workflow_id") == wf.id]
                failed_tasks = [t for t in wf_tasks if t.get("status") == "failed"]
                diag_failed = [t for t in failed_tasks if t.get("description", "").startswith("DIAGNOSTIC:")]
                real_failed = [t for t in failed_tasks if not t.get("description", "").startswith("DIAGNOSTIC:")]
                if real_failed:
                    workflow_errors.append(f"Workflow {wf.id[:8]}: {len(real_failed)} task(s) failed")
                elif diag_failed:
                    workflow_errors.append(f"Workflow {wf.id[:8]}: diagnostic task failed (all feature work completed)")
                else:
                    workflow_errors.append(f"Workflow {wf.id[:8]}: marked failed")

        # Build warning message for completed designs with failed workflows
        warning = None
        if overall_status == "completed" and workflow_errors:
            warning = f"Design completed but {len(workflow_errors)} workflow(s) had issues. " + "; ".join(workflow_errors)

        return {
            "filename": filename,
            "name": design_name,
            "content": design_content,
            "status": overall_status,
            "error": design_error,
            "warning": warning,
            "paused_by": design_paused_by,
            "status_reason": design_status_reason,
            "workflows": [
                {
                    "id": wf.id,
                    "status": wf.status,
                    "created_at": wf.created_at.isoformat() if wf.created_at else None,
                    "error": next((e for e in workflow_errors if wf.id[:8] in e), None) if wf.status == "failed" else None,
                    "paused_by": wf.paused_by,
                    "status_reason": wf.status_reason,
                }
                for wf in matching_workflows
            ],
            "tasks": all_tasks,
            "agents": all_agents,
            "branches": branch_names,
            "feature_folder": feature_folder,
            "features": features,
            "cost_total_usd": sum(f["cost_total_usd"] for f in features),
        }


@router.get("/workflows/{workflow_id}/feature_report")
async def get_workflow_feature_report(workflow_id: str):
    """Serve doc_review's HTML feature report, preferring the workflow's
    live worktree and falling back to the archived features gallery copy
    once that worktree is gone.

    Checking the live worktree first is what lets the report show up on
    the feature row right after doc_review itself finishes -- before
    PhaseManager._populate_feature_folder archives a copy to the features
    gallery at FULL workflow completion (2 phases later). But
    _cleanup_worktree removes the worktree (and nulls
    Workflow.working_directory) once the feature is fully done, which is
    exactly when the archived copy becomes the only one left -- must fall
    back to it or a fully-completed feature's report 404s forever, same
    bug class as get_project_design_status's has_report flag, which this
    matches via the same _find_archived_feature_report helper.

    A Phase 0 (Feature Architect) workflow's report is the decomposition
    synopsis feature_review writes -- same filename, same live-worktree
    check above, but archived to the design's own designs_folder (via
    run_phase0's synopsis_src copy) instead of the per-feature features
    gallery, since Phase 0 predates any Feature row existing.
    """
    from src.core.database import AutopilotDesign, AutopilotProject, Workflow, get_db

    with get_db() as db:
        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        if not wf:
            raise HTTPException(404, "Workflow not found")
        working_directory = wf.working_directory
        project_base_dir = None
        if wf.project_id:
            proj = db.query(AutopilotProject).filter_by(id=wf.project_id).first()
            project_base_dir = proj.base_dir if proj else None
        phase0_designs_folder = None
        if wf.definition_id in PHASE0_DEFINITION_IDS and wf.design_id:
            design = db.query(AutopilotDesign).filter_by(id=wf.design_id).first()
            phase0_designs_folder = design.designs_folder if design else None

    report_path = None
    if working_directory:
        candidate = Path(working_directory) / CONTEXT_DIR_NAME / "feature_report.html"
        if not candidate.is_file():
            candidate = Path(working_directory) / "docs" / "feature_report.html"
        if candidate.is_file():
            report_path = candidate

    if report_path is None and phase0_designs_folder:
        candidate = Path(phase0_designs_folder) / "feature_report.html"
        if candidate.is_file():
            report_path = candidate

    if report_path is None and project_base_dir:
        report_path = _find_archived_feature_report(project_base_dir, workflow_id)

    if report_path is None:
        raise HTTPException(404, "Report not found")
    return HTMLResponse(content=report_path.read_text(errors="replace"))


@router.get("/workflows/{workflow_id}/decomposition_review")
async def get_workflow_decomposition_review(workflow_id: str):
    """Serve feature_review's adversarial review.md for a Phase 0 workflow.

    Same live-worktree-then-designs_folder fallback chain as
    get_workflow_feature_report, since review.md is copied to
    designs_folder by run_phase0 alongside feature_report.html.
    """
    from src.core.database import AutopilotDesign, Workflow, get_db

    with get_db() as db:
        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        if not wf:
            raise HTTPException(404, "Workflow not found")
        working_directory = wf.working_directory
        phase0_designs_folder = None
        if wf.definition_id in PHASE0_DEFINITION_IDS and wf.design_id:
            design = db.query(AutopilotDesign).filter_by(id=wf.design_id).first()
            phase0_designs_folder = design.designs_folder if design else None

    review_path = None
    if working_directory:
        candidate = Path(working_directory) / CONTEXT_DIR_NAME / "review.md"
        if candidate.is_file():
            review_path = candidate

    if review_path is None and phase0_designs_folder:
        candidate = Path(phase0_designs_folder) / "review.md"
        if candidate.is_file():
            review_path = candidate

    if review_path is None:
        raise HTTPException(404, "Review not found")
    return {"name": "review.md", "content": review_path.read_text(errors="replace")}


# ── Features Gallery ─────────────────────────────────────────────


def _scan_features() -> List[Dict[str, Any]]:
    cached = _cached("features", ttl=30.0)
    if cached is not None:
        return cached

    from src.core.status_derivation import derive_feature_status
    from src.core.database import DatabaseManager

    features = []
    try:
        db_manager = DatabaseManager()
        session = db_manager.get_session()
        try:
            from src.core.database import Feature, Workflow, AutopilotProject
            db_features = session.query(Feature).order_by(Feature.created_at.desc()).all()
            for f in db_features:
                status = f.status
                if f.workflow_id:
                    wf = session.query(Workflow).filter_by(id=f.workflow_id).first()
                    if wf:
                        derived = derive_feature_status(session, f.id, write_back=False)
                        if derived:
                            status = derived

                has_report = False
                if f.workflow_id:
                    wf = session.query(Workflow).filter_by(id=f.workflow_id).first()
                    if wf and wf.working_directory:
                        report = Path(wf.working_directory) / CONTEXT_DIR_NAME / "feature_report.html"
                        has_report = report.is_file()
                    if not has_report:
                        project_base = None
                        if wf and wf.project_id:
                            proj = session.query(AutopilotProject).filter_by(id=wf.project_id).first()
                            project_base = proj.base_dir if proj else None
                        if not project_base and wf:
                            lp = wf.launch_params or {}
                            if isinstance(lp, dict):
                                project_base = lp.get("project_path")
                        if project_base:
                            has_report = _find_archived_feature_report(project_base, f.workflow_id) is not None

                created_at = f.created_at.isoformat() if f.created_at else ""

                features.append({
                    "id": f.id,
                    "name": f.name or f.feature_key or f.id,
                    "status": status,
                    "iterations": 0,
                    "total_time_seconds": 0,
                    "stop_reason": "completed" if status == "completed" else "unknown",
                    "cost_total": f.cost_total_usd or 0,
                    "cost_currency": "USD",
                    "created_at": created_at,
                    "has_report": has_report,
                })
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Error scanning features from DB: {e}")

    return _store("features", features)


@router.get("/features", response_model=List[FeatureSummary])
async def list_features():
    return _scan_features()


@router.post("/features/{feature_id}/pause")
async def pause_feature(feature_id: str):
    """Pause a feature's workflow and block its in-flight child tasks."""
    from src.core.database import Agent, Feature, Task, Workflow, get_db

    with get_db() as db:
        feature = db.query(Feature).filter_by(id=feature_id).first()
        if not feature:
            raise HTTPException(status_code=404, detail="Feature not found")
        if not feature.workflow_id:
            raise HTTPException(status_code=400, detail="Feature has no linked workflow")

        wf = db.query(Workflow).filter_by(id=feature.workflow_id).first()
        if not wf:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if wf.status != "active":
            return {"success": True, "message": f"Workflow already {wf.status}"}

        # Terminate any agent actively working a task on this feature, and mark
        # every not-yet-done task 'blocked' so the UI reflects the pause and the
        # orchestrator will not advance them until resume.
        active_tasks = (
            db.query(Task)
            .filter(
                Task.workflow_id == feature.workflow_id,
                Task.status.in_(["pending", "queued", "assigned", "in_progress"]),
            )
            .all()
        )
        for task in active_tasks:
            if task.assigned_agent_id:
                agent = db.query(Agent).filter_by(id=task.assigned_agent_id).first()
                if agent and agent.status in ("working", "starting", "idle"):
                    agent.status = "terminated"
                    agent.current_task_id = None  # Clear stale reference
                    agent.terminated_at = datetime.utcnow()
            task.status = "blocked"

        wf.status = "paused"
        # Same marker /autopilot/stop sets -- without it, the self-heal
        # sweep's _try_auto_resume_paused_workflow silently un-pauses this
        # feature again within one sweep tick (~20-30s), the same bug the
        # pipeline-level pause button had.
        wf.paused_by = "user"
        feature.status = "paused"
        db.commit()
        return {
            "success": True,
            "message": f"Paused feature {feature.name} ({len(active_tasks)} task(s) blocked)",
        }


@router.post("/features/{feature_id}/resume")
async def resume_feature(feature_id: str):
    """Resume a paused or failed feature: recover blocked, failed, and errored tasks."""
    from src.core.database import Agent, Feature, Task, Workflow, get_db

    with get_db() as db:
        feature = db.query(Feature).filter_by(id=feature_id).first()
        if not feature:
            raise HTTPException(status_code=404, detail="Feature not found")
        if not feature.workflow_id:
            raise HTTPException(status_code=400, detail="Feature has no linked workflow")
        workflow_id = feature.workflow_id
        feature_name = feature.name

        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        if not wf:
            raise HTTPException(status_code=404, detail="Workflow not found")

        # Resume workflow if paused or failed
        if wf.status in ("paused", "failed"):
            wf.status = "active"
            wf.paused_by = None
            # Clear a stale arbitration/pause reason -- otherwise it lingers
            # and reads as an ongoing problem even after the user has
            # manually resolved it and resumed.
            wf.status_reason = None

        # Recover blocked/failed tasks, plus any task still marked
        # assigned/in_progress whose agent was terminated (errored/orphaned
        # rather than cleanly failed) — pressing resume should retry all of these.
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

        # Always set feature to active on resume
        feature.status = "active"
        db.commit()

    # Spawn a fresh agent for each restarted task. This runs in-process
    # (not a self-HTTP call) and is fired off in the background: agent
    # initialization can legitimately take 25s+, so awaiting it here would
    # block the response and (as a prior version did via a synchronous HTTP
    # call to this same server with a 30s timeout) time out before the agent
    # ever finished starting, leaving the task stuck at 'pending' forever.
    for task_id, phase_id in to_restart:
        asyncio.create_task(_spawn_agent_for_task(task_id, phase_id))

    return {
        "success": True,
        "message": f"Resumed feature {feature_name} — restarting {len(to_restart)} task(s)",
    }


# ── Review Mode ───────────────────────────────────────────────────────────────


class ReviewModeUpdate(BaseModel):
    review_mode: bool


class FeatureReviewRequest(BaseModel):
    action: str  # "approve" or "request_changes"
    feedback: Optional[str] = None


@router.patch("/projects/{project_id}/review-mode")
async def set_review_mode(project_id: str, req: ReviewModeUpdate):
    """Toggle review mode for a project. When enabled, the pipeline pauses
    after each feature's deploy phase and waits for explicit approval."""
    from src.core.database import AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found")
        proj.review_mode = req.review_mode
        db.commit()
    _invalidate("status")
    return {"review_mode": req.review_mode}


async def _review_phase0_decomposition(workflow_id: str, req: FeatureReviewRequest):
    """Approve or request changes for a Phase 0 (Feature Architect) decomposition.

    Mirrors review_feature's real-feature flow but operates on the Phase 0
    Workflow directly -- there's no Feature row yet at this point, Phase 0
    is what creates them. Approve clears the review pause the same way the
    "Feature Architect" row's existing Resume action already does (run_phase0's
    own wait loop, _wait_for_phase0_review_clearance, just polls paused_by).
    request_changes creates a new task on the feature_architect phase
    carrying the human feedback and spawns an agent for it directly, the
    same one-off-task pattern review_feature uses for a real feature's
    development phase, leaving the workflow paused for review so the redone
    decomposition gets a second look before it's approved.
    """
    from src.core.database import Phase, Task, TaskPromptOverride, Workflow, get_db

    with get_db() as db:
        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        if not wf:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if wf.paused_by != "review":
            return {"success": True, "message": "Decomposition was not awaiting review"}

        arch_phase = (
            db.query(Phase)
            .filter(Phase.workflow_id == workflow_id, Phase.name == "feature_architect")
            .first()
        )

        if req.action == "approve":
            # A prior request_changes may still have a redo agent working in
            # this same worktree. Approving now would let run_phase0's wait
            # loop return immediately, create Feature rows from a
            # possibly-half-written features.json, and then delete the
            # worktree out from under the still-running agent (run_phase0's
            # finally block cleans it up once it considers Phase 0 fully
            # succeeded). Block until the redo settles instead.
            if arch_phase:
                in_flight = (
                    db.query(Task)
                    .filter(
                        Task.workflow_id == workflow_id,
                        Task.phase_id == arch_phase.id,
                        Task.status.in_(["pending", "assigned", "in_progress"]),
                    )
                    .first()
                )
                if in_flight:
                    raise HTTPException(
                        status_code=409,
                        detail="A requested-changes redo is still in progress — wait for it to finish before approving.",
                    )
            wf.status = "active"
            wf.paused_by = None
            db.commit()
            _invalidate("status")

            # Normally run_phase0's own wait loop (still polling in-process)
            # notices this clearance and finishes the job itself. But if
            # this pause was set by the out-of-band completion hook
            # (PhaseManager._complete_workflow -> finalize_phase0_workflow,
            # e.g. after a backend restart left run_phase0 with no live
            # waiter), nothing else will ever create the Feature rows.
            # finalize_phase0_workflow and _create_feature_records are both
            # idempotent, so calling it here unconditionally is a safe
            # no-op in the run_phase0-is-still-waiting case.
            from src.autopilot.orchestrator import finalize_phase0_workflow
            finalize_phase0_workflow(workflow_id, logger, skip_review_gate=True)

            return {"success": True, "message": "Feature decomposition approved"}

        # request_changes — re-decompose with the human's feedback.
        if not arch_phase:
            raise HTTPException(status_code=500, detail="feature_architect phase not found")

        import uuid
        # This one-off task redoes both feature_architect's decomposition and
        # feature_review's adversarial pass in a single agent run -- there is
        # no orchestration engine left running to hand off between the two
        # phases the normal way (the workflow already reached "completed"
        # before pausing for review; run_single_workflow's own phase-by-phase
        # loop, which would otherwise sequence this, already returned).
        # Skipping the review.md/feature_report.html rewrite would leave the
        # review modal showing the pre-redo synopsis and findings forever.
        feedback_prompt = (
            f"## Human Review Feedback\n\n{req.feedback.strip()}\n\n"
            "Re-decompose the design taking the above feedback into account. "
            "Update .hephaestus/features.json and each feature's scope.md accordingly.\n\n"
            "Then, in this same task, perform the adversarial feature-review pass "
            "yourself: compare the revised decomposition against the design document "
            "the same way the feature_review phase does, and rewrite "
            ".hephaestus/review.md and .hephaestus/feature_report.html so both "
            "reflect the revised decomposition -- they are what the human reviewer "
            "sees next, and must not be left describing the old decomposition."
        )

        # Same restartable-task check as the real-feature request_changes
        # path below: if a prior redo is still blocked/failed/orphaned,
        # restart it instead of piling on a second concurrent agent in the
        # same worktree (which would race on features.json). Scoped to this
        # phase specifically, since the real-feature version's workflow-wide
        # scope has no analogous ambiguity (Phase 0 has two phases sharing
        # one workflow).
        candidates = (
            db.query(Task)
            .filter(
                Task.workflow_id == workflow_id,
                Task.phase_id == arch_phase.id,
                Task.status.in_(["blocked", "failed", "assigned", "in_progress", "pending"]),
            )
            .all()
        )
        restartable = []
        for t in candidates:
            if t.status in ("blocked", "failed", "pending"):
                restartable.append(t)
            elif t.assigned_agent_id:
                from src.core.database import Agent
                agent = db.query(Agent).filter_by(id=t.assigned_agent_id).first()
                if not agent or agent.status == "terminated":
                    restartable.append(t)

        if restartable:
            # Mirrors the real-feature restartable-task path below: leave
            # raw_description alone and prefix the new feedback onto the
            # existing override rather than replacing it, so an earlier
            # redo round's feedback isn't silently dropped if it wasn't
            # fully addressed yet.
            reuse_task = restartable[0]
            reuse_task.status = "pending"
            reuse_task.failure_reason = None
            reuse_task.assigned_agent_id = None
            override = db.query(TaskPromptOverride).filter_by(task_id=reuse_task.id).first()
            if override:
                override.user_prompt = feedback_prompt + "\n\n---\n\n" + (override.user_prompt or "")
                override.updated_by = "ui-user"
            else:
                db.add(TaskPromptOverride(
                    task_id=reuse_task.id,
                    user_prompt=feedback_prompt,
                    updated_by="ui-user",
                ))
            task_id, phase_id = reuse_task.id, reuse_task.phase_id
        else:
            new_task = Task(
                id=str(uuid.uuid4()),
                workflow_id=workflow_id,
                phase_id=arch_phase.id,
                raw_description=feedback_prompt,
                enriched_description=None,
                done_definition="Feature decomposition revised per human feedback, review.md and feature_report.html rewritten to match",
                status="pending",
                priority="high",
            )
            db.add(new_task)
            db.flush()
            db.add(TaskPromptOverride(
                task_id=new_task.id,
                user_prompt=feedback_prompt,
                updated_by="ui-user",
            ))
            task_id, phase_id = new_task.id, new_task.phase_id

        # Keep the workflow paused for review — the human must approve
        # again once the redone decomposition is ready.
        db.commit()

    logger.info(f"[REVIEW] Spawning agent for Phase 0 re-decomposition task {task_id}")
    asyncio.create_task(_spawn_agent_for_task(task_id, phase_id))

    _invalidate("status")
    return {"success": True, "message": "Changes requested — re-decomposition queued"}


@router.post("/features/{feature_id}/review")
async def review_feature(feature_id: str, req: FeatureReviewRequest):
    """Approve a feature or request changes.

    approve:          clears the review pause, pipeline advances.
    request_changes:  saves feedback, resumes iteration, pipeline advances.
    """
    if req.action not in ("approve", "request_changes"):
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'request_changes'")
    if req.action == "request_changes" and not (req.feedback or "").strip():
        raise HTTPException(status_code=400, detail="feedback is required when requesting changes")

    if feature_id.startswith("phase0-"):
        return await _review_phase0_decomposition(feature_id[len("phase0-"):], req)

    from src.core.database import Feature, Task, Workflow, get_db

    with get_db() as db:
        feature = db.query(Feature).filter_by(id=feature_id).first()
        if not feature:
            raise HTTPException(status_code=404, detail="Feature not found")
        if not feature.workflow_id:
            raise HTTPException(status_code=400, detail="Feature has no linked workflow")

        wf = db.query(Workflow).filter_by(id=feature.workflow_id).first()
        if not wf:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if wf.paused_by != "review":
            # Idempotent — already cleared (user double-clicked, or pipeline
            # advanced on its own). Return success rather than an error.
            return {"success": True, "message": "Feature was not awaiting review"}

        feature.review_status = "approved" if req.action == "approve" else "changes_requested"
        feature.reviewed_at = datetime.utcnow()
        feature.reviewed_by = "ui-user"

        if req.action == "approve":
            # Clear the review pause — orchestrator's _wait_for_review_clearance
            # polls paused_by; setting it to None unblocks the loop.
            wf.status = "active"
            wf.paused_by = None
            # Restore Feature.status to "active" so derive_feature_status
            # doesn't short-circuit on "paused" forever after approval.
            feature.status = "active"
            db.commit()

            # Create review_approved marker so the safe git wrapper
            # allows git merge. Without this, the agent-safe-bin/git
            # script blocks all merge commands.
            if wf.working_directory:
                from pathlib import Path
                marker_dir = Path(wf.working_directory) / ".hephaestus"
                marker_dir.mkdir(parents=True, exist_ok=True)
                marker = marker_dir / "review_approved"
                marker.write_text(f"Approved at {datetime.utcnow().isoformat()}\n")
                logger.info(f"[REVIEW] Created review_approved marker at {marker}")

            # In review mode, git_commit_push created a PR but didn't merge.
            # Merge it now that the feature is approved.
            pr_url = feature.pr_url or _extract_pr_url(db, wf.id, {})
            if pr_url:
                import subprocess
                try:
                    # Try gh pr merge first
                    result = subprocess.run(
                        ["gh", "pr", "merge", pr_url, "--merge"],
                        capture_output=True, text=True, timeout=30,
                    )
                    if result.returncode == 0:
                        logger.info(f"[REVIEW] Merged PR {pr_url} after approval")
                    else:
                        logger.warning(f"[REVIEW] gh pr merge failed: {result.stderr}")
                except Exception as e:
                    logger.warning(f"[REVIEW] Failed to merge PR: {e}")

            # Check if all tasks are done — if so, mark as completed
            from src.core.database import Task as _Task
            from src.autopilot.spec import DIAGNOSTIC_TASK_PREFIX
            from src.core.database import PhaseExecution as _PhaseExecution
            all_tasks = db.query(_Task).filter(
                _Task.workflow_id == wf.id,
                ~_Task.raw_description.like(f"{DIAGNOSTIC_TASK_PREFIX}%")
            ).all()
            # Check all tasks are done AND all phases are completed (or
            # legitimately skipped -- see derive_feature_status's matching
            # fix for why excluding "skipped" here disagreed with the
            # workflow-level derivation and caused this same feature to
            # flap back to "active" on the next self-heal poll).
            all_phases_done = db.query(_PhaseExecution).join(
                _Phase, _PhaseExecution.phase_id == _Phase.id
            ).filter(
                _Phase.workflow_id == wf.id,
                _PhaseExecution.status.notin_(["completed", "skipped"])
            ).count() == 0
            if all_tasks and all(t.status == "done" for t in all_tasks) and all_phases_done:
                wf.status = "completed"
                feature.status = "completed"
                db.commit()

            _invalidate("status")
            return {"success": True, "message": f"Feature {feature.name} approved"}

        # request_changes path
        feature.review_feedback = req.feedback
        workflow_id = feature.workflow_id
        feature_name = feature.name

        # Keep workflow paused for review - the feature stays yellow
        # until user approves after development fixes are done
        # Don't resume the workflow here, just create the task

        # Find restartable tasks, or create a new one if all are done
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
                from src.core.database import Agent
                agent = db.query(Agent).filter_by(id=t.assigned_agent_id).first()
                if not agent or agent.status == "terminated":
                    restartable.append(t)

        # If no restartable tasks, create a new development task
        # to address the feedback directly
        if not restartable:
            from src.core.database import Phase
            # Find the development phase
            dev_phase = (
                db.query(Phase)
                .filter(
                    Phase.workflow_id == workflow_id,
                    Phase.name == "development",
                )
                .first()
            )
            if dev_phase:
                import uuid
                # Load feedback prompt template from YAML
                feedback_prompt = f"## Human Review Feedback\n\n{req.feedback.strip()}\n\nRead the feature report for context: .hephaestus/feature_report.html\n\nAddress all feedback items and make the necessary code changes."
                try:
                    import yaml as _yaml
                    from pathlib import Path as _Path
                    prompt_file = _Path(__file__).parent.parent.parent / "config" / "prompts" / "review_feedback.yaml"
                    if prompt_file.exists():
                        with open(prompt_file) as f:
                            prompt_config = _yaml.safe_load(f)
                            feedback_prompt = prompt_config.get("review_feedback_prompt", feedback_prompt).format(feedback=req.feedback.strip())
                except Exception:
                    pass  # Use default prompt

                new_task = Task(
                    id=str(uuid.uuid4()),
                    workflow_id=workflow_id,
                    phase_id=dev_phase.id,
                    raw_description=feedback_prompt,
                    enriched_description=None,
                    done_definition="All review feedback addressed",
                    status="pending",
                    priority="high",
                )
                db.add(new_task)
                db.flush()
                restartable.append(new_task)
                logger.info(f"[REVIEW] Created new development task {new_task.id} for feedback")
            else:
                logger.warning(f"[REVIEW] No development phase found for workflow {workflow_id}")

        to_restart = [(t.id, t.phase_id) for t in restartable]
        for t in restartable:
            t.status = "pending"
            t.failure_reason = None
            t.assigned_agent_id = None

        # Inject feedback into each restarted task via TaskPromptOverride
        if req.feedback and to_restart:
            from src.core.database import TaskPromptOverride
            feedback_prefix = (
                f"## Human Review Feedback\n\n{req.feedback.strip()}\n\n"
                "Please address the above feedback in your implementation.\n\n---\n\n"
            )
            for task_id, _ in to_restart:
                existing = db.query(TaskPromptOverride).filter_by(task_id=task_id).first()
                if existing:
                    existing.user_prompt = feedback_prefix + (existing.user_prompt or "")
                else:
                    db.add(TaskPromptOverride(
                        task_id=task_id,
                        user_prompt=feedback_prefix,
                        updated_by="ui-user",
                    ))

        # Keep feature paused for review - user must approve after fixes
        # feature.status stays "paused" and wf.paused_by stays "review"
        db.commit()

    # Spawn agents for restarted tasks (out of DB session, same as resume_feature)
    for task_id, phase_id in to_restart:
        logger.info(f"[REVIEW] Spawning agent for task {task_id} (phase {phase_id})")
        asyncio.create_task(_spawn_agent_for_task(task_id, phase_id))

    _invalidate("status")
    return {
        "success": True,
        "message": f"Changes requested for {feature_name} — restarting {len(to_restart)} task(s)",
    }


@router.delete("/features/{feature_id}")
async def delete_feature(feature_id: str):
    """Permanently delete a feature: terminate any agent still working its
    tasks, remove its worktree (if any), and delete the feature, its
    workflow, and every dependent record. For an old/stuck feature run
    that has no path back to "done" and just clutters the queue -- mirrors
    rerun_design's own cleanup (Step 2b above), scoped to one feature
    instead of an entire design.
    """
    from sqlalchemy.exc import IntegrityError

    from src.core.app_context import get_app_state
    from src.core.database import (
        AgentResult,
        BoardConfig,
        CostEntry,
        DiagnosticRun,
        Feature,
        Memory,
        PhaseExecution,
        Task,
        TaskPromptOverride,
        Ticket,
        ValidationReview,
        Workflow,
        WorkflowResult,
        get_db,
    )

    with get_db() as db:
        feature = db.query(Feature).filter_by(id=feature_id).first()
        if not feature:
            raise HTTPException(status_code=404, detail="Feature not found")

        workflow_id = feature.workflow_id
        working_directory = None
        launch_params: dict = {}
        agent_ids_to_terminate: List[str] = []
        if workflow_id:
            wf = db.query(Workflow).filter_by(id=workflow_id).first()
            if wf:
                working_directory = wf.working_directory
                launch_params = wf.launch_params if isinstance(wf.launch_params, dict) else {}
            agent_ids_to_terminate = [
                t.assigned_agent_id
                for t in db.query(Task).filter(
                    Task.workflow_id == workflow_id,
                    Task.assigned_agent_id.isnot(None),
                )
                if t.assigned_agent_id
            ]

    # Terminate before deleting: Agent.current_task_id is a foreign key
    # (foreign_keys=ON) and terminate_agent is what clears it, same
    # reasoning as the single-task DELETE endpoint (server.py).
    if agent_ids_to_terminate:
        server_state = get_app_state()
        for agent_id in agent_ids_to_terminate:
            await server_state.agent_manager.terminate_agent(agent_id)

    try:
        with get_db() as db:
            feature = db.query(Feature).filter_by(id=feature_id).first()
            if not feature:
                raise HTTPException(status_code=404, detail="Feature not found")

            if workflow_id:
                task_ids = [t.id for t in db.query(Task).filter(Task.workflow_id == workflow_id).all()]
                if task_ids:
                    db.query(TaskPromptOverride).filter(TaskPromptOverride.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(ValidationReview).filter(ValidationReview.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(AgentResult).filter(AgentResult.task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(Memory).filter(Memory.related_task_id.in_(task_ids)).delete(synchronize_session=False)
                    db.query(Ticket).filter(Ticket.task_id.in_(task_ids)).delete(synchronize_session=False)
                    # CostEntry.task_id/workflow_id are also enforced FKs -- a
                    # feature that ever recorded real LLM cost (the common
                    # case, not the exception) would otherwise fail to delete.
                    db.query(CostEntry).filter(CostEntry.task_id.in_(task_ids)).delete(synchronize_session=False)

                db.query(DiagnosticRun).filter(DiagnosticRun.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(WorkflowResult).filter(WorkflowResult.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(BoardConfig).filter(BoardConfig.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(Ticket).filter(Ticket.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(CostEntry).filter(CostEntry.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(PhaseExecution).filter(PhaseExecution.workflow_execution_id == workflow_id).delete(synchronize_session=False)
                db.query(Task).filter(Task.workflow_id == workflow_id).delete(synchronize_session=False)
                db.query(Workflow).filter_by(id=workflow_id).delete(synchronize_session=False)

            db.delete(feature)
    except IntegrityError as e:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete feature {feature_id}: other records still reference it -- {e}",
        )

    # Best-effort worktree cleanup. Not fatal if it can't be resolved --
    # the startup sweep (sweep_completed_workflow_worktrees) only catches
    # "completed" workflows, and this Workflow row is now gone entirely,
    # so this is the one chance to reclaim the directory.
    if working_directory and ".worktrees/" in working_directory:
        try:
            wt_path = Path(working_directory)
            if (wt_path / ".git").exists():
                project_path_str = launch_params.get("project_path")
                if project_path_str:
                    import git as _git

                    from src.autopilot.orchestrator import _cleanup_worktree

                    try:
                        branch = _git.Repo(wt_path).active_branch.name
                    except Exception:
                        branch = ""
                    # _cleanup_worktree only calls .info/.warning -- this
                    # module's own logger satisfies that without needing
                    # OrchestratorLogger's real log-file machinery here.
                    _cleanup_worktree(wt_path, branch, Path(project_path_str), logger)
                else:
                    logger.warning(
                        f"[DELETE-FEATURE] {feature_id}'s worktree {wt_path} has no "
                        "launch_params.project_path to scope cleanup to -- left in place"
                    )
        except Exception as e:
            logger.warning(f"[DELETE-FEATURE] Failed to clean up worktree for {feature_id}: {e}")

    _invalidate("queue", "features", "status")
    return {"success": True, "feature_id": feature_id}


async def _spawn_agent_for_task(task_id: str, phase_id: Optional[str]) -> None:
    """Create an agent for a task, mirroring /api/create_agent_for_task in server.py."""
    from src.core.app_context import get_app_state
    from src.core.database import Task

    server_state = get_app_state()

    session = server_state.db_manager.get_session()
    try:
        task = session.query(Task).filter_by(id=task_id).first()
        if not task:
            logger.warning(f"[RESUME] Task {task_id} not found, cannot restart")
            return

        enriched_data = {}
        if task.enriched_description:
            enriched_data["enriched_description"] = task.enriched_description

        agent = await server_state.agent_manager.create_agent_for_task(
            task=task,
            enriched_data=enriched_data,
            memories=[],
            project_context="",
            agent_type="phase",
            use_existing_worktree=True,
            # Assign the task in the same commit as the Agent row itself,
            # before the slow worktree/tmux/prompt work -- otherwise a crash
            # in that window (e.g. a backend restart) leaves Agent.current_task_id
            # set but Task.assigned_agent_id permanently null. See
            # create_agent_for_task's assign_to_task docstring for the incident.
            assign_to_task=True,
        )
        logger.info(f"[RESUME] Restarted task {task_id[:8]} with agent {agent.id[:8]}")
    except Exception as e:
        logger.error(f"[RESUME] Failed to restart task {task_id[:8]}: {e}", exc_info=True)
        session.rollback()
        task = session.query(Task).filter_by(id=task_id).first()
        if task:
            task.status = "failed"
            task.failure_reason = f"Resume failed to spawn agent: {e}"
            session.commit()
    finally:
        session.close()


@router.get("/features/{feature_id}", response_model=FeatureDetail)
async def get_feature_detail(feature_id: str):
    cache_key = f"feature:{feature_id}"
    cached = _cached(cache_key, ttl=30.0)
    if cached is not None:
        return cached

    feature_dir = _safe_path(FEATURES_DIR, feature_id)
    if not feature_dir.exists() or not feature_dir.is_dir():
        raise HTTPException(404, f"Feature '{feature_id}' not found")

    report_path = feature_dir / "feature_report.html"
    metrics = _read_json(feature_dir / "docs" / "pipeline_metrics.json") or {}

    docs_dir = feature_dir / "docs"
    docs = []
    if docs_dir.exists():
        for f in sorted(docs_dir.iterdir()):
            if f.is_file():
                stat = f.stat()
                docs.append(
                    {
                        "name": f.name,
                        "size_bytes": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                        "type": "markdown" if f.suffix == ".md" else "json" if f.suffix == ".json" else "text" if f.suffix == ".txt" else "other",
                    }
                )

    summaries = {}
    summary_files = {
        "requirements_summary": "requirements.md",
        "architecture_summary": "architecture.md",
        "security_summary": "security.md",
        "qa_summary": "qa.md",
        "product_validation_summary": "validation.md",
        "forensics_summary": "forensics.md",
    }
    for key, fname in summary_files.items():
        fpath = docs_dir / fname
        if fpath.exists():
            content = fpath.read_text(errors="replace")
            summaries[key] = content[:500] + ("..." if len(content) > 500 else "")

    dir_name = feature_dir.name
    name = dir_name.split("_", 1)[1].replace("_", " ").replace("-", " ").title() if "_" in dir_name else dir_name

    created_at = datetime.fromtimestamp(feature_dir.stat().st_mtime, tz=timezone.utc).isoformat()

    result = FeatureDetail(
        id=feature_dir.name,
        name=name,
        status=_feature_status(metrics),
        iterations=metrics.get("iterations", 0),
        total_time_seconds=metrics.get("total_time_seconds", 0),
        stop_reason=metrics.get("stop_reason", "unknown"),
        qa_passed=metrics.get("qa_passed", False),
        product_validated=metrics.get("product_validated", False),
        has_report=report_path.exists(),
        design_name=metrics.get("design_name", name),
        project_path=metrics.get("project_path", ""),
        feature_folder=metrics.get("feature_folder", str(feature_dir)),
        requirements_summary=summaries.get("requirements_summary", ""),
        architecture_summary=summaries.get("architecture_summary", ""),
        security_summary=summaries.get("security_summary", ""),
        qa_summary=summaries.get("qa_summary", ""),
        product_validation_summary=summaries.get("product_validation_summary", ""),
        forensics_summary=summaries.get("forensics_summary", ""),
        files_created=metrics.get("files_created", []),
        issues_resolved=metrics.get("issues_resolved", []),
        outstanding_issues=metrics.get("outstanding_issues", []),
        cost_total=metrics.get("cost_total", 0),
        cost_breakdown=metrics.get("cost_breakdown", {}),
        cost_currency=metrics.get("cost_currency", "USD"),
        created_at=created_at,
        docs=docs,
    )
    return _store(cache_key, result)


def _resolve_feature_docs_base(wf) -> Optional[str]:
    """Best-known directory to look for a feature's generated docs in.

    working_directory is cleared once a feature's worktree is cleaned up
    after a successful merge (see _cleanup_worktree in orchestrator.py) --
    that's correct, the worktree is genuinely gone, but it means a
    *completed* feature's docs are no longer reachable there. They were
    merged into the project's main repo, so fall back to launch_params'
    project_path (observed live: core-infrastructure showed an empty Docs
    tab despite being done, purely because this fallback was missing).
    """
    if wf.working_directory:
        return wf.working_directory
    launch_params = wf.launch_params or {}
    if isinstance(launch_params, dict):
        return launch_params.get("project_path")
    return None


@router.get("/feature-records/{feature_id}/docs")
async def list_feature_record_docs(feature_id: str):
    """List generated docs for a Feature Model row (Feature DB table).

    Distinct from /features/{feature_id}/docs above -- that endpoint reads
    from FEATURES_DIR (a scanned-directory feature id, legacy single-feature
    pipeline). This one reads from a Feature row's own workflow's
    working_directory/docs -- the storage location every current multi-
    feature design pipeline actually writes to (architecture.md,
    qa.md, etc., same files task_completion_service verifies).
    """
    from src.core.database import AutopilotDesign, Feature, Workflow, get_db

    with get_db() as db:
        feat = db.query(Feature).filter_by(id=feature_id).first()
        if not feat:
            raise HTTPException(404, f"Feature '{feature_id}' not found")

        docs: List[Dict[str, Any]] = []

        # The Feature Architect (Phase 0) writes one scope.md per feature
        # under the design's own storage folder, before the feature's own
        # workflow/worktree even exists -- distinct from (and predates) the
        # docs the feature's own pipeline phases write later. Surfaced here
        # as "architect-scope.md" so it's not confused with -- or clobbered
        # by -- a same-named file the feature's own phases might produce.
        design = db.query(AutopilotDesign).filter_by(id=feat.design_id).first() if feat.design_id else None
        if design and design.designs_folder:
            scope_path = Path(design.designs_folder) / "features" / feat.feature_key / "scope.md"
            if scope_path.is_file():
                stat = scope_path.stat()
                docs.append(
                    {
                        "name": "architect-scope.md",
                        "size_bytes": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                        "type": "markdown",
                    }
                )

        if not feat.workflow_id:
            return {"docs": docs}
        wf = db.query(Workflow).filter_by(id=feat.workflow_id).first()
        if not wf:
            return {"docs": docs}
        base_dir = _resolve_feature_docs_base(wf)
        if not base_dir:
            return {"docs": docs}
        docs_dir = Path(base_dir) / "docs"

    if not docs_dir.exists():
        return {"docs": docs}

    for f in sorted(docs_dir.iterdir()):
        if f.is_file():
            stat = f.stat()
            docs.append(
                {
                    "name": f.name,
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "type": "markdown" if f.suffix == ".md" else "json" if f.suffix == ".json" else "text" if f.suffix == ".txt" else "other",
                }
            )
    return {"docs": docs}


@router.get("/feature-records/{feature_id}/docs/{doc_name}")
async def get_feature_record_doc(feature_id: str, doc_name: str):
    """Read one generated doc's content for a Feature Model row."""
    from src.core.database import AutopilotDesign, Feature, Workflow, get_db

    with get_db() as db:
        feat = db.query(Feature).filter_by(id=feature_id).first()
        if not feat:
            raise HTTPException(404, f"Feature '{feature_id}' not found")

        if doc_name == "architect-scope.md":
            design = db.query(AutopilotDesign).filter_by(id=feat.design_id).first() if feat.design_id else None
            if not design or not design.designs_folder:
                raise HTTPException(404, "Document 'architect-scope.md' not found")
            scope_dir = str(Path(design.designs_folder) / "features" / feat.feature_key)
            doc_path = _safe_path(scope_dir, "scope.md")
            if not doc_path.exists():
                raise HTTPException(404, "Document 'architect-scope.md' not found")
            return {"name": doc_name, "content": doc_path.read_text(errors="replace")}

        if not feat.workflow_id:
            raise HTTPException(404, f"Document '{doc_name}' not found")
        wf = db.query(Workflow).filter_by(id=feat.workflow_id).first()
        base_dir = _resolve_feature_docs_base(wf) if wf else None
        if not base_dir:
            raise HTTPException(404, "Feature's workflow has no known working directory")
        docs_dir = str(Path(base_dir) / "docs")

    doc_path = _safe_path(docs_dir, doc_name)
    if not doc_path.exists():
        raise HTTPException(404, f"Document '{doc_name}' not found")
    return {"name": doc_name, "content": doc_path.read_text(errors="replace")}


@router.get("/feature-records/{feature_id}/report")
async def get_feature_record_report(feature_id: str):
    """Serve feature_report.html as a real HTML response (not the {name,
    content} JSON shape /docs/{doc_name} above returns) for direct browser
    navigation -- the modal's header "Download Report" link needs raw
    content, not a JSON wrapper. Same live-worktree source as the other
    feature-records endpoints; same underlying file the report icon on
    the feature row (workflow-scoped) also serves, just reachable by the
    Feature DB row's own id instead of needing its workflow_id threaded
    through as a separate prop.
    """
    from src.core.database import Feature, Workflow, get_db

    with get_db() as db:
        feat = db.query(Feature).filter_by(id=feature_id).first()
        if not feat or not feat.workflow_id:
            raise HTTPException(404, f"Feature '{feature_id}' not found")
        wf = db.query(Workflow).filter_by(id=feat.workflow_id).first()
        base_dir = _resolve_feature_docs_base(wf) if wf else None

    report_path = None
    if base_dir:
        candidate = Path(base_dir) / CONTEXT_DIR_NAME / "feature_report.html"
        if candidate.is_file():
            report_path = candidate
        else:
            candidate = Path(base_dir) / "docs" / "feature_report.html"
            if candidate.is_file():
                report_path = candidate
    if report_path is None:
        # Worktree may have been cleaned up after completion — check the
        # archived features gallery (copied there by PhaseManager before
        # _cleanup_worktree runs).
        project_base = None
        if wf and wf.project_id:
            from src.core.database import AutopilotProject
            with get_db() as _db2:
                proj = _db2.query(AutopilotProject).filter_by(id=wf.project_id).first()
                project_base = proj.base_dir if proj else None
        if not project_base and wf:
            lp = wf.launch_params or {}
            if isinstance(lp, dict):
                project_base = lp.get("project_path")
        if project_base:
            archived = _find_archived_feature_report(project_base, feat.workflow_id)
            if archived:
                report_path = archived
    if report_path is None or not report_path.is_file():
        raise HTTPException(404, "Report not found")
    return HTMLResponse(content=report_path.read_text(errors="replace"))


@router.get("/features/{feature_id}/report")
async def get_feature_report(feature_id: str, project_id: Optional[str] = None):
    try:
        effective_dir = _get_effective_features_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    report_path = _safe_path(effective_dir, feature_id, "feature_report.html")
    if not report_path.exists():
        raise HTTPException(404, "Report not found")
    return HTMLResponse(content=report_path.read_text(errors="replace"))


@router.get("/features/{feature_id}/docs/{doc_name}")
async def get_feature_doc(feature_id: str, doc_name: str, project_id: Optional[str] = None):
    # feature_id is globally unique (UUID), so this cache key is already
    # collision-safe across projects without needing project_id in it too.
    cache_key = f"doc:{feature_id}:{doc_name}"
    cached = _cached(cache_key, ttl=60.0)
    if cached is not None:
        return cached

    try:
        effective_dir = _get_effective_features_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    doc_path = _safe_path(effective_dir, feature_id, "docs", doc_name)
    if not doc_path.exists():
        raise HTTPException(404, f"Document '{doc_name}' not found")
    return _store(cache_key, {"name": doc_name, "content": doc_path.read_text(errors="replace")})


@router.get("/features/{feature_id}/download")
async def download_feature_report(feature_id: str, project_id: Optional[str] = None):
    try:
        effective_dir = _get_effective_features_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    report_path = _safe_path(effective_dir, feature_id, "feature_report.html")
    if not report_path.exists():
        raise HTTPException(404, "Report not found")
    return FileResponse(
        path=str(report_path),
        media_type="text/html",
        filename=f"{feature_id}_report.html",
    )


@router.get("/features/{feature_id}/logs")
async def list_feature_logs(feature_id: str, project_id: Optional[str] = None):
    """List available tmux phase logs for a feature run."""
    try:
        effective_dir = _get_effective_features_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    tmux_dir = _safe_path(effective_dir, feature_id, "tmux")
    if not tmux_dir.exists():
        return {"logs": []}
    logs = []
    for f in sorted(tmux_dir.glob("*.log")):
        stat = f.stat()
        logs.append(
            {
                "name": f.name,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return {"logs": logs}


@router.get("/features/{feature_id}/logs/{log_name}")
async def get_feature_log(feature_id: str, log_name: str, project_id: Optional[str] = None):
    """Return the content of a single tmux phase log."""
    try:
        effective_dir = _get_effective_features_dir(project_id)
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    log_path = _safe_path(effective_dir, feature_id, "tmux", log_name)
    if not log_path.exists() or log_path.suffix != ".log":
        raise HTTPException(404, f"Log '{log_name}' not found")
    return {"name": log_name, "content": log_path.read_text(errors="replace")}


# ── Message Center ───────────────────────────────────────────────


@router.get("/messages", response_model=List[MessageItem])
async def get_messages(limit: int = Query(50, ge=1, le=500)):
    cache_key = f"messages:{limit}"
    cached = _cached(cache_key, ttl=5.0)
    if cached is not None:
        return cached

    run_dir = _get_latest_run_dir()
    if not run_dir:
        return _store(cache_key, [])

    events = _read_jsonl_tail(run_dir / "events.jsonl", limit=limit)
    result = [
        MessageItem(
            timestamp=e.get("timestamp", ""),
            type=e.get("type", "unknown"),
            data={k: v for k, v in e.items() if k not in ("timestamp", "type")},
        )
        for e in events
    ]
    return _store(cache_key, result)


@router.get("/messages/archived")
async def get_archived_messages():
    """Get archived message IDs."""
    from sqlalchemy import text

    from src.core.database import get_db

    with get_db() as db:
        try:
            db.execute(text("SELECT 1 FROM archived_events LIMIT 1"))
        except Exception:
            db.execute(
                text("""CREATE TABLE IF NOT EXISTS archived_events (
                id TEXT PRIMARY KEY,
                message_type TEXT,
                timestamp TEXT,
                archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")
            )
            db.commit()

        result = db.execute(text("SELECT id FROM archived_events")).fetchall()
        return {"archived_ids": [r[0] for r in result]}


@router.post("/messages/archive")
async def archive_message(request: dict):
    """Archive a message by its ID."""
    from sqlalchemy import text

    from src.core.database import get_db

    msg_id = request.get("message_id")
    msg_type = request.get("message_type", "unknown")
    timestamp = request.get("timestamp", "")

    if not msg_id:
        raise HTTPException(400, "message_id is required")

    with get_db() as db:
        try:
            db.execute(text("SELECT 1 FROM archived_events LIMIT 1"))
        except Exception:
            db.execute(
                text("""CREATE TABLE IF NOT EXISTS archived_events (
                id TEXT PRIMARY KEY,
                message_type TEXT,
                timestamp TEXT,
                archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")
            )
            db.commit()

        db.execute(
            text("INSERT OR IGNORE INTO archived_events (id, message_type, timestamp) VALUES (:id, :type, :ts)"),
            {"id": msg_id, "type": msg_type, "ts": timestamp},
        )
        db.commit()
    return {"archived": True}


@router.post("/messages/unarchive")
async def unarchive_message(request: dict):
    """Unarchive a message by its ID."""
    from sqlalchemy import text

    from src.core.database import get_db

    msg_id = request.get("message_id")
    if not msg_id:
        raise HTTPException(400, "message_id is required")

    with get_db() as db:
        try:
            db.execute(text("DELETE FROM archived_events WHERE id = :id"), {"id": msg_id})
            db.commit()
        except Exception:
            pass
    return {"unarchived": True}


@router.post("/messages/unarchive-all")
async def unarchive_all_messages():
    """Unarchive all messages."""
    from sqlalchemy import text

    from src.core.database import get_db

    with get_db() as db:
        try:
            db.execute(text("DELETE FROM archived_events"))
            db.commit()
        except Exception:
            pass
    return {"unarchived": True}


@router.post("/messages/cleanup-archives")
async def cleanup_old_archives():
    """Remove archived messages older than 30 days."""
    from sqlalchemy import text

    from src.core.database import get_db

    with get_db() as db:
        try:
            db.execute(text("DELETE FROM archived_events WHERE archived_at < datetime('now', '-30 days')"))
            db.commit()
        except Exception:
            pass
    return {"cleaned": True}


@router.get("/logs")
async def get_logs(lines: int = Query(100, ge=1, le=2000)):
    cache_key = f"logs:{lines}"
    cached = _cached(cache_key, ttl=5.0)
    if cached is not None:
        return cached

    run_dir = _get_latest_run_dir()
    if not run_dir:
        return _store(cache_key, {"lines": []})

    log_path = run_dir / "orchestrator.log"
    if not log_path.exists():
        return _store(cache_key, {"lines": []})

    try:
        all_lines = log_path.read_text(errors="replace").splitlines()
        return _store(cache_key, {"lines": all_lines[-lines:]})
    except Exception:
        return _store(cache_key, {"lines": []})


# ── Human Input ─────────────────────────────────────────────────

STALE_INPUT_SECONDS = 3600  # 1 hour


class HumanInputRequest(BaseModel):
    id: str
    reason: str
    timestamp: str
    options: List[str]
    labels: Dict[str, str]


class HumanInputResponse(BaseModel):
    request_id: str
    choice: str
    message: Optional[str] = None


def _find_pending_input() -> Optional[Path]:
    """Find the first non-stale input request file."""
    input_dir = Path(AUTOPILOT_STATE_DIR)
    if not input_dir.exists():
        return None
    for f in sorted(input_dir.glob("input_request_*.json")):
        try:
            data = json.loads(f.read_text())
            ts = datetime.fromisoformat(data["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > STALE_INPUT_SECONDS:
                f.unlink(missing_ok=True)
                # Also clean up any orphaned response
                rid = data.get("id", "")
                resp = input_dir / f"input_response_{rid}.json"
                resp.unlink(missing_ok=True)
                continue
            return f
        except Exception:
            continue
    return None


@router.get("/input", response_model=Optional[HumanInputRequest])
async def get_human_input_request():
    """Check if the orchestrator is waiting for human input."""
    request_file = _find_pending_input()
    if not request_file:
        return None
    try:
        data = json.loads(request_file.read_text())
        return HumanInputRequest(**data)
    except Exception:
        return None


@router.post("/input")
async def submit_human_input(resp: HumanInputResponse):
    """Submit a human input response to the orchestrator."""
    if resp.choice not in ("c", "s", "q", "m"):
        raise HTTPException(400, "Invalid choice. Must be 'c', 's', 'q', or 'm'.")

    # Verify the request still exists
    request_file = Path(AUTOPILOT_STATE_DIR) / f"input_request_{resp.request_id}.json"
    if not request_file.exists():
        raise HTTPException(404, "Input request not found or already answered.")

    response_file = Path(AUTOPILOT_STATE_DIR) / f"input_response_{resp.request_id}.json"
    response_file.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write via temp+rename
    payload = {
        "request_id": resp.request_id,
        "choice": resp.choice,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if resp.message:
        payload["message"] = resp.message
    payload = json.dumps(payload)
    tmp = response_file.with_suffix(".tmp")
    tmp.write_text(payload)
    os.rename(tmp, response_file)

    _invalidate("status")
    return {"submitted": resp.choice, "request_id": resp.request_id}


@router.delete("/input/{request_id}")
async def dismiss_human_input(request_id: str):
    """Dismiss a pending human input request without responding."""
    request_file = Path(AUTOPILOT_STATE_DIR) / f"input_request_{request_id}.json"
    if not request_file.exists():
        raise HTTPException(404, "Request not found.")
    # Only delete the request — orchestrator will see it's gone and stop polling
    request_file.unlink(missing_ok=True)
    _invalidate("status")
    return {"dismissed": request_id}


# ── Pipeline Start/Stop ──────────────────────────────────────────


@router.post("/start")
async def start_pipeline(project_path: str, design_queue: str = "", max_iterations: int = 3):
    """Start the autopilot pipeline."""
    from src.autopilot.orchestrator import _get_or_create_project_id
    from src.autopilot.service import get_registry

    project_id = _get_or_create_project_id(project_path)

    # Concurrency-cap check, before anything else touches the (possibly
    # already-running) service for this project -- a genuinely new project
    # over the cap should be rejected before the zombie-detection block
    # below does any mutating work (stop()) on a service we're about to
    # refuse anyway. Restarting a project already occupying a slot is
    # always allowed (try_reserve never counts that as a new slot).
    # try_reserve (not can_start) atomically reserves the slot too, closing
    # the race window between two concurrent /start calls both checking the
    # cap before either has actually started -- the reservation MUST be
    # released below once service.start() has resolved either way.
    can_start, cap_message = get_registry().try_reserve(project_id)
    if not can_start:
        raise HTTPException(409, cap_message)

    try:
        return await _start_pipeline_reserved(project_id, project_path, design_queue, max_iterations)
    finally:
        get_registry().release_reservation(project_id)


async def _start_pipeline_reserved(project_id: str, project_path: str, design_queue: str, max_iterations: int):
    """Body of start_pipeline() that runs after the concurrency-cap slot for
    project_id has been reserved -- split out so the reservation can be
    released in a finally regardless of which of the several early-return/
    raise paths below is taken."""
    from src.autopilot.service import get_autopilot_service

    service = get_autopilot_service(project_id)
    # Give a freshly-(re)started pipeline time to actually reach its first
    # workflow check before second-guessing it. Without this, a zombie
    # verdict landing seconds after start cancels run_continuous_pipeline's
    # task -- which resets its in-memory recovery-attempt counter -- before
    # it ever gets a chance to hand off to the per-feature resume path.
    # Observed live: zombie-detected and stopped 8s after auto-resume,
    # trapping a genuinely in-progress workflow in a stop/restart loop that
    # could never escalate past its own recovery counter.
    zombie_check_grace_seconds = 45
    time_since_start = time.time() - service._start_time if service._start_time else None
    if service.running and (time_since_start is None or time_since_start >= zombie_check_grace_seconds):
        # Check for zombie state: service says running but no active agents/workflows.
        # This happens when the pipeline task gets stuck. Auto-stop and restart.
        # BUT: if the queue is legitimately empty (all designs done), the pipeline
        # is correctly idle — not a zombie.
        # Scoped to THIS project's own workflows/agents/designs -- a busy
        # OTHER project must never mask (or falsely trigger) this check.
        try:
            from src.core.database import Agent, AutopilotDesign, Task, Workflow, get_db

            with get_db() as db:
                project_wf_ids = [w.id for w in db.query(Workflow).filter(Workflow.project_id == project_id).all()]
                active_agents = (
                    db.query(Agent)
                    .join(Task, Agent.current_task_id == Task.id)
                    .filter(
                        Task.workflow_id.in_(project_wf_ids),
                        Agent.status.in_(["working", "starting", "idle"]),
                    )
                    .count()
                    if project_wf_ids
                    else 0
                )
                active_wfs = (
                    db.query(Workflow)
                    .filter(
                        Workflow.project_id == project_id,
                        Workflow.status == "active",
                    )
                    .count()
                )

                # Only zombie-detect if there are pending designs that
                # should be getting processed. Empty queue = legitimate idle.
                pending_designs = (
                    db.query(AutopilotDesign)
                    .filter(
                        AutopilotDesign.project_id == project_id,
                        AutopilotDesign.status.in_(["pending", "active"]),
                    )
                    .count()
                )

            if active_agents == 0 and active_wfs == 0 and pending_designs > 0:
                logger.warning(f"[START] Zombie pipeline detected (running=True but {pending_designs} pending/active designs and no agents/workflows) — auto-stopping")
                await service.stop()
            elif active_agents == 0 and active_wfs == 0 and pending_designs == 0:
                logger.info("[START] Pipeline is running but all designs are done — stopping cleanly and restarting")
                await service.stop()
            else:
                raise HTTPException(409, "Pipeline is already running.")
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"[START] Zombie check failed, proceeding with start: {e}")
            await service.stop()

    try:
        result = await service.start(
            project_path=project_path,
            design_queue=design_queue,
            max_iterations=max_iterations,
        )
        _invalidate("status")
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        logger.error(f"Failed to start pipeline: {e}")
        raise HTTPException(500, str(e))


@router.post("/stop")
async def stop_pipeline(clear_state: bool = False, project_id: Optional[str] = None):
    """Stop the autopilot pipeline and all its agents.

    Args:
        clear_state: If True, clear persistent pipeline state (fresh start next time)
        project_id: If provided, only stop workflows for this project
    """
    from src.autopilot.service import get_autopilot_service, get_registry
    from src.core.database import AutopilotProject, get_db

    # Stop the service(s) (this stops the pipeline task). With project_id,
    # stop just that project's service; without one, preserve the old
    # "stop whatever's running" behavior by stopping every running service
    # (there's no longer a single global service to fall back to).
    # stopped_project_ids feeds the clear_state block below -- it must be
    # captured here, not re-derived from get_registry().running() after the
    # fact, since every service in it is no longer "running" once stopped.
    if project_id:
        result = await get_autopilot_service(project_id).stop()
        stopped_project_ids = [project_id]
    else:
        stopped_any = False
        stopped_project_ids = []
        aggregate = {"designs_processed": 0, "designs_succeeded": 0, "designs_failed": 0}
        for running_service in get_registry().running():
            r = await running_service.stop()
            stopped_any = True
            stopped_project_ids.append(running_service.project_id)
            for key in aggregate:
                aggregate[key] += r.get(key, 0)
        result = {"stopped": stopped_any, **aggregate} if stopped_any else {"stopped": True, "message": "Pipeline was not running"}

    # Terminate autopilot agents and pause workflows
    # Uses shared pause_project_workflows which includes Phase 0 workflows
    # (definition_id in ["autopilot", "autopilot-phase0"]).
    terminated_count = 0
    try:
        from src.autopilot.orchestrator import pause_project_workflows

        with get_db() as db:
            for pid in stopped_project_ids:
                paused = pause_project_workflows(db, pid, paused_by="user")
                terminated_count += paused
                # Deactivate the project so UI no longer shows it as Active
                proj = db.query(AutopilotProject).filter_by(id=pid).first()
                if proj:
                    proj.is_active = False
            db.commit()
    except Exception as e:
        logger.error(f"Error cleaning up autopilot agents: {e}")

    # Clear persistent state if requested -- scoped to whichever project(s)
    # this call actually stopped, not the old bare global key, so stopping
    # project A can't wipe project B's still-running pipeline state.
    if clear_state:
        from src.autopilot.orchestrator import PersistentPipelineState

        for stopped_project_id in stopped_project_ids:
            PersistentPipelineState(project_id=stopped_project_id).clear()
        logger.info(f"Cleared persistent pipeline state for {stopped_project_ids}")

    _invalidate("status")
    return {
        "stopped": True,
        "agents_terminated": terminated_count,
        "state_cleared": clear_state,
        **result,
    }


@router.post("/cleanup-branches")
async def cleanup_branches(project_path: Optional[str] = None):
    """Clean up all stale agent branches.

    project_path: which project's repo to sweep. Defaults to the active
    project -- WorktreeManager otherwise operates on whatever project
    happens to be config.main_repo_path's current global default, which is
    wrong as soon as more than one project exists (same bug already fixed
    for the other WorktreeManager(...) call sites -- see orchestrator.py).
    """
    from src.core.database import AutopilotProject, DatabaseManager, get_db
    from src.core.worktree_manager import WorktreeManager

    try:
        if not project_path:
            with get_db() as db:
                active_id = _get_active_project_id()
                proj = (
                    db.query(AutopilotProject).filter_by(id=active_id).first()
                    if active_id
                    else None
                )
                if not proj:
                    raise HTTPException(
                        400,
                        "project_path is required (no active project to default to)",
                    )
                project_path = proj.base_dir

        db_manager = DatabaseManager()
        branch_manager = WorktreeManager(db_manager)
        branch_manager.reload(project_path)
        result = branch_manager.cleanup_all_stale_branches()
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cleanup branches: {e}")
        raise HTTPException(500, str(e))


@router.get("/health")
async def get_system_health():
    """Get system health audit results."""
    return run_health_audit()


def run_health_audit(db_manager=None):
    """Shared health audit logic used by both Monitor and API endpoint.

    Returns:
        dict with 'findings', 'workflows', 'summary' keys
    """
    from src.core.database import Agent, DatabaseManager, Task, Workflow, get_db

    if db_manager is None:
        db_manager = DatabaseManager()

    findings = []

    # 1. Orphaned processes
    try:
        result = subprocess.run(
            ["pgrep", "-la", "opencode|claude|pi"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            pids = []
            for line in result.stdout.strip().split("\n"):
                if line:
                    parts = line.split()
                    if len(parts) >= 1:
                        pids.append(parts[0])

            tmux_result = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{session_name}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            tmux_pids = set()
            if tmux_result.returncode == 0:
                for line in tmux_result.stdout.strip().split("\n"):
                    if line:
                        parts = line.split()
                        if len(parts) >= 1:
                            tmux_pids.add(parts[0])

            orphaned = [p for p in pids if p not in tmux_pids]
            if orphaned:
                findings.append(
                    {
                        "type": "orphaned_processes",
                        "severity": "warning",
                        "message": f"{len(orphaned)} orphaned process(es) not in tmux",
                        "pids": orphaned[:10],
                        "action": f"kill -9 {' '.join(orphaned[:5])}",
                    }
                )
    except Exception:
        pass

    # 2. Unmerged branches
    try:
        # Get project path from active autopilot project
        with get_db() as _db:
            from src.core.database import AutopilotProject

            _proj = _db.query(AutopilotProject).filter_by(is_active=True).first()
            project_path = _proj.base_dir if _proj else os.getenv("PROJECT_PATH")
        if not project_path:
            return findings  # Can't check without a project path
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
                findings.append(
                    {
                        "type": "unmerged_branches",
                        "severity": "info",
                        "message": f"{len(branches)} unmerged agent branch(es)",
                        "branches": branches[:10],
                        "action": "heph cleanup branches",
                    }
                )
    except Exception:
        pass

    # 3. Workflow progress + stuck/failed
    workflows_summary = []
    session = db_manager.get_session()
    try:
        autopilot_wfs = (
            session.query(Workflow)
            .filter(
                Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS),
                Workflow.status.in_(["active", "running", "paused"]),
            )
            .all()
        )

        for wf in autopilot_wfs:
            design_name = "unknown"
            if wf.launch_params:
                try:
                    params = json.loads(wf.launch_params) if isinstance(wf.launch_params, str) else wf.launch_params
                    doc = params.get("design_document", "")
                    design_name = Path(doc).stem.replace("_", " ").replace("-", " ") if doc else "unknown"
                except Exception:
                    pass

            tasks = session.query(Task).filter(Task.workflow_id == wf.id).all()
            status_counts = {}
            for t in tasks:
                status_counts[t.status] = status_counts.get(t.status, 0) + 1

            total = len(tasks)
            done = status_counts.get("done", 0)
            failed = status_counts.get("failed", 0)
            in_progress = status_counts.get("in_progress", 0)
            pending = status_counts.get("pending", 0) + status_counts.get("queued", 0)

            progress = {
                "design": design_name,
                "workflow_id": wf.id[:8],
                "status": wf.status,
                "total_tasks": total,
                "done": done,
                "failed": failed,
                "in_progress": in_progress,
                "pending": pending,
                "progress_pct": round(done / total * 100) if total > 0 else 0,
            }

            if in_progress == 0 and pending > 0 and done < total and wf.status == "active":
                progress["stuck"] = True
                findings.append(
                    {
                        "type": "stuck_design",
                        "severity": "warning",
                        "message": f"Design '{design_name}' stuck: {pending} pending, 0 active",
                        "workflow_id": wf.id[:8],
                        "action": "Relaunch agents or pause workflow",
                    }
                )

            for t in tasks:
                if t.status == "failed":
                    findings.append(
                        {
                            "type": "failed_task",
                            "severity": "error",
                            "message": f"Failed in '{design_name}': {(t.enriched_description or t.raw_description or '')[:80]}",
                            "task_id": t.id[:8],
                            "action": "Review and rerun",
                        }
                    )

            workflows_summary.append(progress)
    finally:
        session.close()

    # 4. Active agents
    try:
        with get_db() as db:
            active = db.query(Agent).filter(Agent.status.in_(["working", "starting", "idle"])).count()
            terminated = db.query(Agent).filter(Agent.status == "terminated").count()
    except Exception:
        active = 0
        terminated = 0

    return {
        "findings": findings,
        "workflows": workflows_summary,
        "active_agents": active,
        "terminated_agents": terminated,
        "summary": {
            "total_findings": len(findings),
            "errors": len([f for f in findings if f["severity"] == "error"]),
            "warnings": len([f for f in findings if f["severity"] == "warning"]),
            "info": len([f for f in findings if f["severity"] == "info"]),
        },
    }


# ── Config ───────────────────────────────────────────────────────


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
