"""API endpoints for the Autopilot dashboard."""

import asyncio
import collections
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from src.core.constants import AUTOPILOT_STATE_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/autopilot", tags=["Autopilot"])

DESIGN_QUEUE_DIR = ""
FEATURES_DIR = ""

ALLOWED_EXTENSIONS = {".md", ".txt"}

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
    if not base:
        raise HTTPException(500, "Directory not configured")
    resolved = (Path(base) / Path(*parts)).resolve()
    base_resolved = Path(base).resolve()
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


async def _is_orchestrator_running() -> bool:
    cached = _cached("orchestrator_running", ttl=5.0)
    if cached is not None:
        return cached

    pid_file = Path(AUTOPILOT_STATE_DIR) / "orchestrator.pid"
    if not pid_file.exists():
        return _store("orchestrator_running", False)
    try:
        pid = int(pid_file.read_text().strip())
        proc = await asyncio.create_subprocess_exec(
            "ps", "-p", str(pid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        return _store("orchestrator_running", proc.returncode == 0)
    except Exception:
        return _store("orchestrator_running", False)


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
    artifacts: List[Dict[str, Any]]


class PipelineStatus(BaseModel):
    running: bool
    current_design: Optional[str] = None
    designs_processed: int = 0
    designs_succeeded: int = 0
    designs_failed: int = 0
    total_elapsed: int = 0
    queue_depth: int = 0
    last_event: Optional[Dict[str, Any]] = None


class MessageItem(BaseModel):
    timestamp: str
    type: str
    data: Dict[str, Any]


# ── Pipeline Status ───────────────────────────────────────────────

@router.get("/status", response_model=PipelineStatus)
async def get_pipeline_status():
    cached = _cached("status", ttl=5.0)
    if cached is not None:
        return cached

    run_dir = _get_latest_run_dir()
    running = await _is_orchestrator_running()

    state = _cached("state", ttl=5.0)
    if state is None:
        if run_dir:
            state = _store("state", _read_json(run_dir / "state.json") or {})
        else:
            state = _store("state", {})

    queue_depth = 0
    if DESIGN_QUEUE_DIR and Path(DESIGN_QUEUE_DIR).exists():
        for ext in ALLOWED_EXTENSIONS:
            queue_depth += len(list(Path(DESIGN_QUEUE_DIR).glob(f"*{ext}")))

    last_event = _cached("last_event", ttl=5.0)
    if last_event is None:
        if run_dir:
            events = _read_jsonl_tail(run_dir / "events.jsonl", limit=1)
            last_event = _store("last_event", events[-1] if events else None)
        else:
            last_event = _store("last_event", None)

    result = PipelineStatus(
        running=running,
        current_design=state.get("current_design"),
        designs_processed=state.get("designs_processed", 0),
        designs_succeeded=state.get("designs_succeeded", 0),
        designs_failed=state.get("designs_failed", 0),
        total_elapsed=state.get("total_elapsed", 0),
        queue_depth=queue_depth,
        last_event=last_event,
    )
    return _store("status", result)


# ── Design Queue ─────────────────────────────────────────────────

def _get_queue_order_path() -> Optional[Path]:
    if not DESIGN_QUEUE_DIR:
        return None
    return Path(DESIGN_QUEUE_DIR) / ".queue_order.json"


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

    if not DESIGN_QUEUE_DIR or not Path(DESIGN_QUEUE_DIR).exists():
        return _store("queue", [])

    queue_path = Path(DESIGN_QUEUE_DIR)
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
        items.append(DesignQueueItem(
            filename=f.name,
            name=name,
            size_bytes=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            extension=f.suffix,
        ))

    return _store("queue", items)


class QueueReorderRequest(BaseModel):
    filenames: List[str]


@router.post("/queue/reorder")
async def reorder_queue(req: QueueReorderRequest):
    if not DESIGN_QUEUE_DIR or not Path(DESIGN_QUEUE_DIR).exists():
        raise HTTPException(500, "DESIGN_QUEUE_DIR not configured")

    queue_path = Path(DESIGN_QUEUE_DIR)
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


@router.post("/queue", response_model=DesignQueueItem)
async def add_to_queue(item: DesignQueueAdd):
    if not DESIGN_QUEUE_DIR:
        raise HTTPException(500, "DESIGN_QUEUE_DIR not configured")

    queue_path = Path(DESIGN_QUEUE_DIR)
    queue_path.mkdir(parents=True, exist_ok=True)

    ext = item.extension if item.extension in ALLOWED_EXTENSIONS else ".md"
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in item.name)
    safe_name = safe_name.strip().replace(" ", "_")
    if not safe_name:
        raise HTTPException(400, "Invalid design name")
    filename = f"{safe_name}{ext}"
    filepath = _safe_path(DESIGN_QUEUE_DIR, filename)

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
    filepath = _safe_path(DESIGN_QUEUE_DIR, filename)
    if not filepath.exists():
        raise HTTPException(404, f"Design '{filename}' not found")
    filepath.unlink()
    _invalidate("queue", "status")
    return {"removed": filename}


@router.get("/queue/{filename}/content")
async def get_queue_item_content(filename: str):
    filepath = _safe_path(DESIGN_QUEUE_DIR, filename)
    if not filepath.exists():
        raise HTTPException(404, f"Design '{filename}' not found")
    return {"filename": filename, "content": filepath.read_text(errors="replace")}


# ── Projects ────────────────────────────────────────────────────

import re
import uuid
import asyncio as _asyncio

DESIGN_SUBDIR = "docs/design-queue"
_ORDINAL_RE = re.compile(r"^(\d+)[-_]")


class ProjectItem(BaseModel):
    id: str
    name: str
    base_dir: str
    is_default: bool
    is_active: bool = False
    design_count: int
    created_at: str
    updated_at: str


class ProjectCreate(BaseModel):
    name: str
    base_dir: str
    is_default: bool = False


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    base_dir: Optional[str] = None
    is_default: Optional[bool] = None


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


_project_sync_locks: Dict[str, _asyncio.Lock] = {}
_project_lock_guard = _asyncio.Lock()


async def _get_project_lock(project_id: str) -> _asyncio.Lock:
    async with _project_lock_guard:
        if project_id not in _project_sync_locks:
            _project_sync_locks[project_id] = _asyncio.Lock()
        return _project_sync_locks[project_id]


def _get_design_queue_dir(project_base: str) -> Path:
    return Path(project_base) / DESIGN_SUBDIR


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

    design_dir = _get_design_queue_dir(project_base)
    design_dir.mkdir(parents=True, exist_ok=True)

    fs_files: Dict[str, Path] = {}
    for ext in ALLOWED_EXTENSIONS:
        for f in design_dir.glob(f"*{ext}"):
            fs_files[f.name] = f

    existing = {
        d.filename: d
        for d in db.query(AutopilotDesign).filter_by(project_id=project_id).all()
    }

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
            d.ordinal = ordinal
        else:
            d = AutopilotDesign(
                id=f"des-{uuid.uuid4().hex[:12]}",
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
                id=f"des-{uuid.uuid4().hex[:12]}",
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
    designs = (
        db.query(AutopilotDesign)
        .filter_by(project_id=project_id)
        .order_by(AutopilotDesign.ordinal)
        .all()
    )
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
    from src.core.database import AutopilotProject, AutopilotDesign, get_db

    with get_db() as db:
        projects = db.query(AutopilotProject).order_by(AutopilotProject.name).all()
        result = []
        for p in projects:
            count = db.query(AutopilotDesign).filter_by(project_id=p.id).count()
            result.append(ProjectItem(
                id=p.id,
                name=p.name,
                base_dir=p.base_dir,
                is_default=p.is_default,
                is_active=getattr(p, 'is_active', False),
                design_count=count,
                created_at=p.created_at.isoformat() if p.created_at else "",
                updated_at=p.updated_at.isoformat() if p.updated_at else "",
            ))
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
            is_active=getattr(proj, 'is_active', False),
            design_count=len(designs),
            created_at=proj.created_at.isoformat() if proj.created_at else "",
            updated_at=proj.updated_at.isoformat() if proj.updated_at else "",
        )


@router.get("/projects/{project_id}", response_model=ProjectItem)
async def get_project(project_id: str):
    from src.core.database import AutopilotProject, AutopilotDesign, get_db

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
            is_active=getattr(proj, 'is_active', False),
            design_count=count,
            created_at=proj.created_at.isoformat() if proj.created_at else "",
            updated_at=proj.updated_at.isoformat() if proj.updated_at else "",
        )


@router.put("/projects/{project_id}", response_model=ProjectItem)
async def update_project(project_id: str, req: ProjectUpdate):
    from src.core.database import AutopilotProject, AutopilotDesign, get_db

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

        db.flush()

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
            is_active=getattr(proj, 'is_active', False),
            design_count=count,
            created_at=proj.created_at.isoformat() if proj.created_at else "",
            updated_at=proj.updated_at.isoformat() if proj.updated_at else "",
        )


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    from src.core.database import AutopilotProject, get_db

    replacement_proj = None

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")

        was_active = getattr(proj, 'is_active', False)
        db.delete(proj)
        db.flush()

        if was_active:
            next_proj = db.query(AutopilotProject).order_by(AutopilotProject.name).first()
            if next_proj:
                next_proj.is_active = True
                replacement_proj = next_proj

    if replacement_proj:
        try:
            from src.mcp.projects_api import _apply_active_project
            _apply_active_project(replacement_proj)
        except Exception as e:
            logger.error(f"Failed to activate replacement project: {e}")

    _invalidate("queue", "status", f"project_designs:{project_id}")
    return {"deleted": project_id}


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


@router.get("/projects/{project_id}/designs", response_model=List[DesignItem])
async def list_project_designs(project_id: str):
    from src.core.database import AutopilotProject, AutopilotDesign, get_db

    cache_key = f"project_designs:{project_id}"
    cached = _cached(cache_key)
    if cached is not None:
        return cached

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")

        designs = (
            db.query(AutopilotDesign)
            .filter_by(project_id=project_id)
            .order_by(AutopilotDesign.ordinal)
            .all()
        )
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
    from src.core.database import AutopilotProject, AutopilotDesign, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir

    design_dir = _get_design_queue_dir(base_dir)
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

    design_id = f"des-{uuid.uuid4().hex[:12]}"

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
    from src.core.database import AutopilotDesign, get_db

    with get_db() as db:
        designs = db.query(AutopilotDesign).filter_by(project_id=project_id).all()
        by_id = {d.id: d for d in designs}

        for i, design_id in enumerate(req.design_ids):
            if design_id not in by_id:
                raise HTTPException(400, f"Unknown design id: {design_id}")
            by_id[design_id].ordinal = i + 1

    _invalidate("queue", f"project_designs:{project_id}")
    return {"order": req.design_ids}


@router.delete("/projects/{project_id}/designs/{filename}")
async def remove_project_design(project_id: str, filename: str):
    from src.core.database import AutopilotProject, AutopilotDesign, get_db

    # Delete DB record first, then file (atomic rollback if file delete fails)
    found = False
    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")
        base_dir = proj.base_dir

        d = db.query(AutopilotDesign).filter_by(
            project_id=project_id, filename=filename
        ).first()
        if d:
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
    filepath = _safe_path(str(design_dir), filename)
    if not filepath.exists():
        raise HTTPException(404, f"Design '{filename}' not found")
    return {"filename": filename, "content": filepath.read_text(errors="replace")}


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

        metrics_path = feature_dir / "artifacts" / "pipeline_metrics.json"
        metrics = _read_json(metrics_path) or {}

        report_path = feature_dir / "feature_report.html"
        created_at = datetime.fromtimestamp(
            feature_dir.stat().st_mtime, tz=timezone.utc
        ).isoformat()

        dir_name = feature_dir.name
        if "_" in dir_name:
            name = dir_name.split("_", 1)[1].replace("_", " ").replace("-", " ").title()
        else:
            name = dir_name

        features.append({
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
        })

    return _store("features", features)


@router.get("/features", response_model=List[FeatureSummary])
async def list_features():
    return _scan_features()


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
    metrics = _read_json(feature_dir / "artifacts" / "pipeline_metrics.json") or {}

    artifacts_dir = feature_dir / "artifacts"
    artifacts = []
    if artifacts_dir.exists():
        for f in sorted(artifacts_dir.iterdir()):
            if f.is_file():
                stat = f.stat()
                artifacts.append({
                    "name": f.name,
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "type": "markdown" if f.suffix == ".md" else
                            "json" if f.suffix == ".json" else
                            "text" if f.suffix == ".txt" else
                            "other",
                })

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
        fpath = artifacts_dir / fname
        if fpath.exists():
            content = fpath.read_text(errors="replace")
            summaries[key] = content[:500] + ("..." if len(content) > 500 else "")

    dir_name = feature_dir.name
    name = dir_name.split("_", 1)[1].replace("_", " ").replace("-", " ").title() if "_" in dir_name else dir_name

    created_at = datetime.fromtimestamp(
        feature_dir.stat().st_mtime, tz=timezone.utc
    ).isoformat()

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
        artifacts=artifacts,
    )
    return _store(cache_key, result)


@router.get("/features/{feature_id}/report")
async def get_feature_report(feature_id: str):
    report_path = _safe_path(FEATURES_DIR, feature_id, "feature_report.html")
    if not report_path.exists():
        raise HTTPException(404, "Report not found")
    return HTMLResponse(content=report_path.read_text(errors="replace"))


@router.get("/features/{feature_id}/artifacts/{artifact_name}")
async def get_feature_artifact(feature_id: str, artifact_name: str):
    cache_key = f"artifact:{feature_id}:{artifact_name}"
    cached = _cached(cache_key, ttl=60.0)
    if cached is not None:
        return cached

    artifact_path = _safe_path(FEATURES_DIR, feature_id, "artifacts", artifact_name)
    if not artifact_path.exists():
        raise HTTPException(404, f"Artifact '{artifact_name}' not found")
    return _store(cache_key, {"name": artifact_name, "content": artifact_path.read_text(errors="replace")})


@router.get("/features/{feature_id}/download")
async def download_feature_report(feature_id: str):
    report_path = _safe_path(FEATURES_DIR, feature_id, "feature_report.html")
    if not report_path.exists():
        raise HTTPException(404, "Report not found")
    return FileResponse(
        path=str(report_path),
        media_type="text/html",
        filename=f"{feature_id}_report.html",
    )


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
    if resp.choice not in ("c", "s", "q"):
        raise HTTPException(400, "Invalid choice. Must be 'c', 's', or 'q'.")

    # Verify the request still exists
    request_file = Path(AUTOPILOT_STATE_DIR) / f"input_request_{resp.request_id}.json"
    if not request_file.exists():
        raise HTTPException(404, "Input request not found or already answered.")

    response_file = Path(AUTOPILOT_STATE_DIR) / f"input_response_{resp.request_id}.json"
    response_file.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write via temp+rename
    payload = json.dumps({
        "request_id": resp.request_id,
        "choice": resp.choice,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
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
    import subprocess
    import signal

    running = await _is_orchestrator_running()
    if running:
        raise HTTPException(409, "Pipeline is already running.")

    project = Path(project_path).resolve()
    if not project.exists():
        raise HTTPException(400, f"Project path does not exist: {project}")

    dq = design_queue or str(project / "docs" / "design-queue")
    os.makedirs(dq, exist_ok=True)

    # Find python
    venv_python = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else "python"

    cmd = [
        python, "-m", "src.autopilot.orchestrator",
        "--project-path", str(project),
        "--design-queue", dq,
        "--max-iterations", str(max_iterations),
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=str(Path(__file__).parent.parent.parent.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    pid_dir = Path.home() / ".hephaestus" / "pids"
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "orchestrator.pid").write_text(str(proc.pid))

    _invalidate("status")
    return {"started": True, "pid": proc.pid}


@router.post("/stop")
async def stop_pipeline():
    """Stop the autopilot pipeline."""
    import signal

    pid_file = Path(AUTOPILOT_STATE_DIR) / "orchestrator.pid"
    if not pid_file.exists():
        raise HTTPException(404, "No pipeline PID file found.")

    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        raise HTTPException(500, "Invalid PID file.")

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        raise HTTPException(404, "Pipeline process not found.")

    # Wait for graceful shutdown
    for _ in range(50):
        await asyncio.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    else:
        # Force kill
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    pid_file.unlink(missing_ok=True)
    _invalidate("status")
    return {"stopped": True, "pid": pid}


# ── Config ───────────────────────────────────────────────────────

def configure_autopilot_api(
    design_queue_dir: str = "",
    features_dir: str = "",
):
    global DESIGN_QUEUE_DIR, FEATURES_DIR
    DESIGN_QUEUE_DIR = design_queue_dir or os.getenv("DESIGN_QUEUE_DIR", "")
    FEATURES_DIR = features_dir or os.getenv("FEATURES_DIR", "")
    _invalidate("queue", "features", "status")
    logger.info(f"Autopilot API configured: queue={DESIGN_QUEUE_DIR}, features={FEATURES_DIR}")
