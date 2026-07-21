"""API endpoints for the Autopilot dashboard."""

import asyncio
import collections
import hashlib
import json
import logging
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypeVar

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, field_validator, model_validator

from src.core.constants import (
    AUTOPILOT_STATE_DIR,
    CONTEXT_DIR_NAME,
    DESIGN_CONTEXT_SUBDIR,
    GOTO_REASON_PREFIX,
)

# Import authentication function from server module
from src.mcp.server import verify_agent_authentication

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/autopilot", tags=["Autopilot"])

DESIGN_QUEUE_DIR = ""
FEATURES_DIR = ""
_active_project_id_cache: Optional[str] = None  # Track which project the cached dirs belong to

ALLOWED_EXTENSIONS = {".md", ".txt"}

# Workflow.definition_id values that identify a design's workflows.
# "autopilot-phase0" is the pre-rename Phase 0 definition_id (see
# config/workflows/feature_architect/, renamed from autopilot-phase0/) --
# kept here for historical DB rows created before the rename, not because
# new workflows still use it. One shared constant instead of repeating this
# pair inline at every call site, so a future retirement of the old value
# only has to happen in one place.
PHASE0_DEFINITION_IDS = ("autopilot-phase0", "feature_architect")
DESIGN_WORKFLOW_DEFINITION_IDS = ("autopilot",) + PHASE0_DEFINITION_IDS


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
    _invalidate("queue", "features", "status")


def _get_effective_queue_dir() -> str:
    """Get the effective design queue directory.

    Automatically invalidates the cache when the active project changes.

    Raises:
        FileNotFoundError: If queue directory doesn't exist
        RuntimeError: If no active project configured
    """
    global DESIGN_QUEUE_DIR, _active_project_id_cache

    # Check if the active project has changed since we last cached
    current_project_id = _get_active_project_id()
    if current_project_id != _active_project_id_cache:
        # Project changed — invalidate cached dirs
        DESIGN_QUEUE_DIR = ""
        _active_project_id_cache = current_project_id

    if DESIGN_QUEUE_DIR:
        if not Path(DESIGN_QUEUE_DIR).exists():
            raise FileNotFoundError(f"Design queue directory does not exist: {DESIGN_QUEUE_DIR}")
        return DESIGN_QUEUE_DIR

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


def _get_effective_features_dir() -> str:
    """Get the effective features directory.

    Automatically invalidates the cache when the active project changes.

    Raises:
        FileNotFoundError: If features directory doesn't exist
        RuntimeError: If no active project configured
    """
    global FEATURES_DIR, _active_project_id_cache

    # Check if the active project has changed since we last cached
    current_project_id = _get_active_project_id()
    if current_project_id != _active_project_id_cache:
        # Project changed — invalidate cached dirs
        FEATURES_DIR = ""
        _active_project_id_cache = current_project_id

    if FEATURES_DIR:
        if not Path(FEATURES_DIR).exists():
            raise FileNotFoundError(f"Features directory does not exist: {FEATURES_DIR}")
        return FEATURES_DIR

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
    running_project_path: Optional[str] = None
    running_project_name: Optional[str] = None
    # True when the running project matches the requested project (after
    # realpath resolution, so /tmp == /private/tmp on macOS).
    is_self_conflict: bool = False


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
    if project_id:
        service_status = get_autopilot_service(project_id).status()
    else:
        # PipelineStatus is a single-object shape (running_project_path/
        # running_project_name are singular fields, by the same pre-multi-
        # project assumption noted on those fields above) -- it can't
        # represent "N projects running" without a schema change. Sum what
        # CAN be honestly aggregated (counts) across every running project
        # instead of arbitrarily reporting only the first one's numbers;
        # current_design/elapsed_seconds/error still reflect just one
        # project (the first), since those genuinely have no multi-project
        # representation in this response shape.
        running_services = get_registry().running()
        if running_services:
            service_status = dict(running_services[0].status())
            for extra in running_services[1:]:
                extra_status = extra.status()
                for key in ("designs_processed", "designs_succeeded", "designs_failed"):
                    service_status[key] = service_status.get(key, 0) + extra_status.get(key, 0)
        else:
            service_status = {}

    run_dir = _get_latest_run_dir()
    running = service_status.get("running", False)

    # When project_id is provided, check if THIS project has active
    # workflows OR active agents (the service.running flag is global,
    # not per-project).
    if project_id:
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
        # symlink resolution (/tmp -> /private/tmp on macOS).
        is_self_conflict=(running_project_path is not None and project_path is not None and os.path.realpath(running_project_path) == os.path.realpath(project_path)),
    )
    return _store(cache_key, result)


# ── Design Queue ─────────────────────────────────────────────────


def _get_queue_order_path() -> Optional[Path]:
    try:
        # Write alongside other server state under .hephaestus/, not inside
        # the tracked docs/design/ directory (which would pollute git status).
        effective_dir = _get_effective_queue_dir()
        hephaestus_dir = Path(effective_dir).parent.parent / CONTEXT_DIR_NAME
        hephaestus_dir.mkdir(parents=True, exist_ok=True)
        return hephaestus_dir / ".queue_order.json"
    except (FileNotFoundError, RuntimeError):
        return None


def _load_queue_order() -> List[str]:
    path = _get_queue_order_path()
    if path and path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return []


def _save_queue_order(order: List[str]):
    path = _get_queue_order_path()
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(order))


@router.get("/queue", response_model=List[DesignQueueItem])
async def list_design_queue():
    cached = _cached("queue")
    if cached is not None:
        return cached

    try:
        effective_dir = _get_effective_queue_dir()
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))

    queue_path = Path(effective_dir)
    saved_order = _load_queue_order()

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

    return _store("queue", items)


class QueueReorderRequest(BaseModel):
    filenames: List[str]


@router.post("/queue/reorder")
async def reorder_queue(req: QueueReorderRequest):
    try:
        effective_dir = _get_effective_queue_dir()
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

    _save_queue_order(req.filenames)
    _invalidate("queue")
    return {"order": req.filenames}


@router.post("/queue/requeue")
async def requeue_design(request: dict):
    """Move a design to the front of the queue and pause its active workflow."""
    from src.core.database import Agent, Task, Workflow, get_db

    filename = request.get("filename")
    if not filename:
        raise HTTPException(400, "filename is required")

    # Get the queue order
    order = _load_queue_order()

    # Move to front
    if filename in order:
        order.remove(filename)
    order.insert(0, filename)
    _save_queue_order(order)
    _invalidate("queue")

    # Pause any active workflow processing this design
    paused_count = 0
    try:
        with get_db() as db:
            # Find autopilot workflows that are active
            active_workflows = (
                db.query(Workflow)
                .filter(
                    Workflow.definition_id == "autopilot",
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
            DiagnosticRun,
            Memory,
            PhaseExecution,
            TaskPromptOverride,
            Ticket,
            ValidationReview,
            WorkflowResult,
        )

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

                # Delete workflow-level dependents
                db.query(DiagnosticRun).filter(DiagnosticRun.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(WorkflowResult).filter(WorkflowResult.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(BoardConfig).filter(BoardConfig.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(Ticket).filter(Ticket.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

                # Delete phase executions
                db.query(PhaseExecution).filter(PhaseExecution.workflow_execution_id.in_(wf_ids)).delete(synchronize_session=False)

                # Delete tasks
                db.query(Task).filter(Task.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

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
    except Exception as e:
        logger.error(f"Error cleaning up design state for rerun: {e}")

    # Step 3: Clean up branches (non-blocking)
    try:
        from src.core.database import DatabaseManager
        from src.core.worktree_manager import WorktreeManager

        db_manager = DatabaseManager()
        bm = WorktreeManager(db_manager)
        # Run cleanup in background thread to not block pipeline start
        import threading

        thread = threading.Thread(target=lambda: bm.cleanup_all_stale_branches(), daemon=True)
        thread.start()
    except Exception as e:
        logger.error(f"Error starting branch cleanup: {e}")

    # Step 4: Move design to front of queue
    order = _load_queue_order()
    if filename in order:
        order.remove(filename)
    order.insert(0, filename)
    _save_queue_order(order)
    _invalidate("queue")

    # Resolved once and reused below -- clearing pipeline state (Step 5) and
    # starting the pipeline (Step 6) must scope to the SAME project, not two
    # independently-resolved ids.
    from src.autopilot.orchestrator import _get_or_create_project_id

    rerun_start_project_id = _get_or_create_project_id(str(project))

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
                            Workflow.definition_id == "autopilot",
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

        review_instructions = f"""REPAIR AGENT: Design '{filename}' needs systematic repair.

CRITICAL RULE: The design document is the SOURCE OF TRUTH. Do NOT modify it.
If implementation differs from design, fix the implementation to match the design.
If you cannot resolve a discrepancy or need to deviate from the design,
send an inbox message to the human for approval using the message tool.
Only deviate from the design with explicit human approval.

Workflow {wf_id[:8]} status: {reason}
Completed: {len(done_tasks)} | Failed: {len(failed_tasks)} | Pending: {len(pending_tasks)} | In Progress: {len(in_progress_tasks)}

Tasks:
{chr(10).join(task_summary) if task_summary else "No tasks found"}

YOUR JOB:
1. Read the design doc at {project / DESIGN_CONTEXT_SUBDIR / filename} (READ ONLY - do not modify)
2. Check what has been completed so far in the feature folder
3. Identify what's blocking progress
4. You have FULL AUTHORITY to:
   - Create tasks and spawn agents via create_task + create_agent_for_task
   - Merge branches via MCP tools
   - Fix code to match design (NOT the other way around)
5. For EACH failed task:
   a. Read the error and understand why it failed
   b. Determine: can it be retried? does it need rework? is it blocked?
   c. If retryable: reset to pending, spawn agent to relaunch
   d. If needs rework: create new task with corrected instructions, spawn agent
   e. If blocked: document the blocker and move on
   f. MONITOR: after spawning, check get_task_status until done or failed
6. For EACH pending task:
   a. Check if dependencies are met (depends_on tasks are done)
   b. If dependencies met: spawn agent via create_agent_for_task
   c. If not met: skip and come back later
   d. MONITOR: check status after spawning
7. For EACH in_progress task:
   a. Check agent output via get_agent_output
   b. If stuck (no progress): nudge agent or terminate and respawn
   c. If progressing: let it continue
8. MERGE: after all tasks complete, merge branches to main
9. WRITE repair_report.md summarizing actions taken
10. Mark your task done when ALL tasks are resolved"""

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
            workflows = db.query(Workflow).filter(Workflow.definition_id == "autopilot").all()

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
            # Create new project
            project = AutopilotProject(
                id=f"proj-{uuid.uuid4().hex[:12]}",
                name=project_path.name,
                base_dir=str(project_path),
                is_active=True,
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
        effective_dir = _get_effective_queue_dir()
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

    _invalidate("queue", "status")

    return DesignQueueItem(
        filename=filename,
        name=item.name,
        size_bytes=stat.st_size,
        modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        extension=ext,
    )


@router.delete("/queue/{filename}")
async def remove_from_queue(filename: str):
    try:
        effective_dir = _get_effective_queue_dir()
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    filepath = _safe_path(effective_dir, filename)
    if not filepath.exists():
        raise HTTPException(404, f"Design '{filename}' not found")
    filepath.unlink()
    _invalidate("queue", "status")
    return {"removed": filename}


@router.get("/queue/{filename}/content")
async def get_queue_item_content(filename: str):
    try:
        effective_dir = _get_effective_queue_dir()
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
async def create_project(req: ProjectCreate):
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
async def update_project(project_id: str, req: ProjectUpdate):
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
        if req.cost_limit_usd is not None:
            proj.cost_limit_usd = req.cost_limit_usd
        elif hasattr(req, "cost_limit_usd") and req.cost_limit_usd is None:
            # Explicitly clearing the limit
            proj.cost_limit_usd = None

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
async def delete_project(project_id: str):
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

    # Delete DB record first, then file (atomic rollback if file delete fails)
    found = False
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

                # Delete workflow-level dependents
                db.query(DiagnosticRun).filter(DiagnosticRun.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(WorkflowResult).filter(WorkflowResult.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(BoardConfig).filter(BoardConfig.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
                db.query(Ticket).filter(Ticket.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

                # Delete phase executions
                db.query(PhaseExecution).filter(PhaseExecution.workflow_execution_id.in_(wf_ids)).delete(synchronize_session=False)

                # Delete tasks
                db.query(Task).filter(Task.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

                # Delete workflows
                db.query(Workflow).filter(Workflow.id.in_(wf_ids)).delete(synchronize_session=False)

            # Delete features
            db.query(Feature).filter_by(design_id=d.id).delete(synchronize_session=False)

            # Delete the design itself
            db.delete(d)
            found = True

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
        _wf_statuses = [wf.status for wf in matching_workflows]
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
                if feat_wf and feat_wf.working_directory:
                    has_report = (Path(feat_wf.working_directory) / "docs" / "feature_report.html").is_file()

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
                phase0_status = (
                    "completed"
                    if all(t["status"] == "done" for t in phase0_tasks)
                    else "failed"
                    if any(t["status"] == "failed" for t in phase0_tasks)
                    else "active"
                    if any(t["status"] in ("assigned", "in_progress") for t in phase0_tasks)
                    else "pending"
                )
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
            "workflows": [
                {
                    "id": wf.id,
                    "status": wf.status,
                    "created_at": wf.created_at.isoformat() if wf.created_at else None,
                    "error": next((e for e in workflow_errors if wf.id[:8] in e), None) if wf.status == "failed" else None,
                }
                for wf in matching_workflows
            ],
            "tasks": all_tasks,
            "agents": all_agents,
            "branches": branch_names,
            "feature_folder": feature_folder,
            "features": features,
        }


@router.get("/workflows/{workflow_id}/feature_report")
async def get_workflow_feature_report(workflow_id: str):
    """Serve doc_review's HTML feature report straight from the workflow's
    live worktree.

    The features gallery's /features/{feature_id}/report only has a copy
    once PhaseManager._populate_feature_folder archives it at FULL
    workflow completion (2 phases after doc_review) -- this is what lets
    the report show up on the feature row right after doc_review itself
    finishes, matching the has_report flag computed in
    get_project_design_status above.
    """
    from src.core.database import Workflow, get_db

    with get_db() as db:
        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        if not wf or not wf.working_directory:
            raise HTTPException(404, "Workflow not found or has no working directory")
        working_directory = wf.working_directory

    report_path = Path(working_directory) / "docs" / "feature_report.html"
    if not report_path.is_file():
        raise HTTPException(404, "Report not found")
    return HTMLResponse(content=report_path.read_text(errors="replace"))


# ── Features Gallery ─────────────────────────────────────────────


def _scan_features() -> List[Dict[str, Any]]:
    cached = _cached("features", ttl=30.0)
    if cached is not None:
        return cached

    if not FEATURES_DIR or not Path(FEATURES_DIR).exists():
        return _store("features", [])

    features = []
    features_path = Path(FEATURES_DIR)

    for feature_dir in sorted(features_path.iterdir(), reverse=True):
        if not feature_dir.is_dir():
            continue

        metrics_path = feature_dir / "docs" / "pipeline_metrics.json"
        metrics = _read_json(metrics_path) or {}

        report_path = feature_dir / "feature_report.html"
        created_at = datetime.fromtimestamp(feature_dir.stat().st_mtime, tz=timezone.utc).isoformat()

        dir_name = feature_dir.name
        if "_" in dir_name:
            name = dir_name.split("_", 1)[1].replace("_", " ").replace("-", " ").title()
        else:
            name = dir_name

        features.append(
            {
                "id": feature_dir.name,
                "name": name,
                "status": _feature_status(metrics),
                "iterations": metrics.get("iterations", 0),
                "total_time_seconds": metrics.get("total_time_seconds", 0),
                "stop_reason": metrics.get("stop_reason", "unknown"),
                "cost_total": metrics.get("cost_total", 0),
                "cost_currency": metrics.get("cost_currency", "USD"),
                "created_at": created_at,
                "has_report": report_path.exists(),
            }
        )

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
        )

        task.assigned_agent_id = agent.id
        task.status = "in_progress"
        task.started_at = datetime.utcnow()
        session.commit()
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
        "requirements_summary": "requirements_analysis.md",
        "architecture_summary": "architecture.md",
        "security_summary": "security_report.md",
        "qa_summary": "qa_report.md",
        "product_validation_summary": "product_validation.md",
        "forensics_summary": "forensics_report.md",
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
    qa_result.json, etc., same files task_completion_service verifies).
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
        if not base_dir:
            raise HTTPException(404, "Feature's workflow has no known working directory")

    report_path = Path(base_dir) / "docs" / "feature_report.html"
    if not report_path.is_file():
        raise HTTPException(404, "Report not found")
    return HTMLResponse(content=report_path.read_text(errors="replace"))


@router.get("/features/{feature_id}/report")
async def get_feature_report(feature_id: str):
    try:
        effective_dir = _get_effective_features_dir()
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    report_path = _safe_path(effective_dir, feature_id, "feature_report.html")
    if not report_path.exists():
        raise HTTPException(404, "Report not found")
    return HTMLResponse(content=report_path.read_text(errors="replace"))


@router.get("/features/{feature_id}/docs/{doc_name}")
async def get_feature_doc(feature_id: str, doc_name: str):
    cache_key = f"doc:{feature_id}:{doc_name}"
    cached = _cached(cache_key, ttl=60.0)
    if cached is not None:
        return cached

    try:
        effective_dir = _get_effective_features_dir()
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    doc_path = _safe_path(effective_dir, feature_id, "docs", doc_name)
    if not doc_path.exists():
        raise HTTPException(404, f"Document '{doc_name}' not found")
    return _store(cache_key, {"name": doc_name, "content": doc_path.read_text(errors="replace")})


@router.get("/features/{feature_id}/download")
async def download_feature_report(feature_id: str):
    try:
        effective_dir = _get_effective_features_dir()
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
async def list_feature_logs(feature_id: str):
    """List available tmux phase logs for a feature run."""
    try:
        effective_dir = _get_effective_features_dir()
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
async def get_feature_log(feature_id: str, log_name: str):
    """Return the content of a single tmux phase log."""
    try:
        effective_dir = _get_effective_features_dir()
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
    from src.core.database import get_db

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
    # Uses shared _pause_project_workflows which includes Phase 0 workflows
    # (definition_id in ["autopilot", "autopilot-phase0"]).
    terminated_count = 0
    try:
        from src.core.cost_derivation import _pause_project_workflows

        with get_db() as db:
            for pid in stopped_project_ids:
                paused = _pause_project_workflows(db, pid, paused_by="user")
                terminated_count += paused
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
async def cleanup_branches():
    """Clean up all stale agent branches."""
    from src.core.database import DatabaseManager
    from src.core.worktree_manager import WorktreeManager

    try:
        db_manager = DatabaseManager()
        branch_manager = WorktreeManager(db_manager)
        result = branch_manager.cleanup_all_stale_branches()
        return result
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
                Workflow.definition_id == "autopilot",
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
