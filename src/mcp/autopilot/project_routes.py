"""Project routes: CRUD, cost tracking, design management, file browsing. — extracted from src/mcp/autopilot_api.py (backend_module_decomposition.md §3.2)."""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, field_validator, model_validator, validator
from sqlalchemy import func as sqlfunc

from src.core.agent_identity import is_known_system_identity
from src.core.constants import (
    CONTEXT_DIR_NAME,
    DESIGN_CONTEXT_SUBDIR,
    DESIGN_WORKFLOW_DEFINITION_IDS,
)
from src.mcp.autopilot._shared import ALLOWED_EXTENSIONS, _cached, _invalidate, _safe_path, _store

# Import authentication function from server module
from src.mcp.server._shared import verify_agent_authentication
from src.mcp.server.oauth_routes import _check_rate_limit
from src.services.design_status_service import get_design_status

logger = logging.getLogger(__name__)

router = APIRouter()

_ORDINAL_RE = re.compile(r"^(\d+)[-_]")


def _apply_active_project(proj):
    """Apply project path to runtime config.

    Updates config immediately (fast). The WorktreeManager will
    reload lazily when next used, not during the API call.
    """
    from src.core.simple_config import get_config

    config = get_config()
    new_path = Path(proj.base_dir)

    # Validate path exists and is a git repo (fast check — just look for .git)
    if not new_path.exists() or not new_path.is_dir():
        raise ValueError(
            f"Cannot activate project — path does not exist: {new_path}"
        )
    if not (new_path / ".git").exists():
        raise ValueError(
            f"Cannot activate project — not a git repository: {new_path}"
        )

    # Update config immediately — no git reload here
    config.git.main_repo_path = new_path
    config.paths.project_root = new_path


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
    # "queue" (default): .hephaestus/designs/, not git-tracked -- used by
    # "Load from Remote" (the file already lives somewhere in the project,
    # nothing new is being introduced). "docs": a locally-uploaded file is
    # new content coming from outside the repo, so it's persisted as a
    # real, git-tracked file under docs/ instead of the hidden staging dir.
    destination: str = "queue"

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

def _init_codegraph_index(resolved: str, project_name: str) -> None:
    """`codegraph init` in a freshly-created project's directory, if
    codegraph is installed and no index exists yet -- real subprocess work
    (up to a 120s timeout) plus filesystem I/O, called via run_in_executor
    by create_project below."""
    try:
        codegraph_dir = Path(resolved) / ".codegraph"
        if not codegraph_dir.exists():
            result = subprocess.run(
                ["codegraph", "init", "."],
                cwd=resolved,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                logger.info(f"CodeGraph initialized for project {project_name} at {resolved}")
                # Hide .codegraph from git without modifying .gitignore
                exclude_file = Path(resolved) / ".git" / "info" / "exclude"
                if exclude_file.exists():
                    exclude_content = exclude_file.read_text()
                    if ".codegraph/" not in exclude_content:
                        exclude_file.write_text(exclude_content.rstrip() + "\n.codegraph/\n")
            else:
                logger.debug(f"CodeGraph init skipped/failed for {project_name}: {result.stderr[:200]}")
    except FileNotFoundError:
        logger.debug("codegraph not installed, skipping index initialization")
    except Exception as e:
        logger.debug(f"CodeGraph init skipped for {project_name}: {e}")

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

        from src.services.system_settings import get_default_cost_limit

        # Apply the system default spend cap (settings:default_cost_limit_usd).
        # Passed the in-flight session deliberately: opening a nested get_db()
        # mid-flush is how SQLite deadlocks.
        proj = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:12]}",
            name=req.name,
            base_dir=resolved,
            is_default=req.is_default,
            cost_limit_usd=get_default_cost_limit(db),
        )
        db.add(proj)
        db.flush()

        # Sync designs in the SAME session — no nested get_db()
        designs = _sync_project_designs(proj.id, resolved, db)

        _invalidate("queue", "status")

    # Initialize codegraph index if codegraph is installed and .codegraph
    # doesn't already exist in the project directory. Offloaded -- the
    # subprocess call alone has a 120s timeout, blocking the whole event
    # loop (every other in-flight request) for up to that long on a
    # single POST /projects call.
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _init_codegraph_index, resolved, proj.name)

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


@router.get("/projects/active", response_model=List[ProjectItem])
async def get_active_projects():
    """List every currently-active project (0 to max_concurrent_projects).

    Was Optional[ProjectItem] (a single project via .first()) before
    multi-project concurrency -- now that more than one project can be
    active at once, returning only one would silently hide the rest.
    """
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        projects = db.query(AutopilotProject).filter_by(is_active=True).all()
        result = []
        for proj in projects:
            count = db.query(AutopilotDesign).filter_by(project_id=proj.id).count()
            result.append(
                ProjectItem(
                    id=proj.id,
                    name=proj.name,
                    base_dir=proj.base_dir,
                    is_default=proj.is_default,
                    is_active=True,
                    design_count=count,
                    created_at=proj.created_at.isoformat() if proj.created_at else "",
                    updated_at=proj.updated_at.isoformat() if proj.updated_at else "",
                )
            )
        return result


@router.post("/projects/{project_id}/activate", response_model=ProjectItem)
async def activate_project(project_id: str):
    from src.core.database import AutopilotDesign, AutopilotProject, get_db
    from src.core.simple_config import get_config

    with get_db() as db:
        proj = db.query(AutopilotProject).filter_by(id=project_id).first()
        if not proj:
            raise HTTPException(404, f"Project not found: {project_id}")

        # Cap the number of simultaneously-active projects at
        # max_concurrent_projects instead of exclusively clearing every
        # other project -- mirrors AutopilotServiceRegistry.can_start's
        # "already occupies a slot" exemption (src/autopilot/service.py)
        # for re-activating an already-active project.
        if not proj.is_active:
            active_projects = (
                db.query(AutopilotProject).filter_by(is_active=True).all()
            )
            max_concurrent = get_config().autopilot.max_concurrent_projects
            if len(active_projects) >= max_concurrent:
                names = ", ".join(p.name for p in active_projects)
                raise HTTPException(
                    409,
                    f"Max concurrent projects ({max_concurrent}) reached: "
                    f"{names}. Stop one before starting another.",
                )
            proj.is_active = True
        db.flush()

        # Apply to runtime config
        from types import SimpleNamespace
        _apply_active_project(SimpleNamespace(base_dir=proj.base_dir))

        count = db.query(AutopilotDesign).filter_by(project_id=proj.id).count()

        logger.info(f"Activated project: {proj.name} ({proj.base_dir})")

        return ProjectItem(
            id=proj.id,
            name=proj.name,
            base_dir=proj.base_dir,
            is_default=proj.is_default,
            is_active=True,
            design_count=count,
            created_at=proj.created_at.isoformat() if proj.created_at else "",
            updated_at=proj.updated_at.isoformat() if proj.updated_at else "",
        )


@router.post("/projects/{project_id}/deactivate", response_model=ProjectItem)
async def deactivate_project(project_id: str):
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).filter_by(id=project_id).first()
        if not proj:
            raise HTTPException(404, f"Project not found: {project_id}")

        proj.is_active = False
        db.flush()

        count = db.query(AutopilotDesign).filter_by(project_id=proj.id).count()

        logger.info(f"Deactivated project: {proj.name} ({proj.base_dir})")

        return ProjectItem(
            id=proj.id,
            name=proj.name,
            base_dir=proj.base_dir,
            is_default=proj.is_default,
            is_active=False,
            design_count=count,
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
            if budget_paused:
                from src.autopilot.orchestrator.engine_client import resume_workflow
                for wf in budget_paused:
                    # force=True: raising/clearing the cost limit is an
                    # explicit override of the budget pause, same as this
                    # endpoint's pre-existing unconditional clear.
                    # cascade_to_feature=False preserves this endpoint's
                    # existing behavior exactly -- it has never touched
                    # Feature.status here.
                    resume_workflow(wf.id, force=True, cascade_to_feature=False, session=db)
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

            # Pass a plain object, not the ORM instance — its session is
            # already closed here, so touching an attribute on it would
            # raise DetachedInstanceError.
            _apply_active_project(SimpleNamespace(base_dir=replacement_base_dir))
        except Exception as e:
            logger.error(f"Failed to activate replacement project: {e}")

    _invalidate("queue", "status", f"project_designs:{project_id}")
    return {"deleted": project_id}

class DefaultBudgetUpdate(BaseModel):
    """None clears the default; a positive number sets it."""

    default_cost_limit_usd: Optional[float] = None


@router.get("/settings/default-budget")
async def get_default_budget():
    """The system-wide default spend cap applied to newly created projects."""
    from src.services.system_settings import get_default_cost_limit

    loop = asyncio.get_running_loop()
    value = await loop.run_in_executor(None, get_default_cost_limit)
    return {"default_cost_limit_usd": value}


@router.put("/settings/default-budget")
async def put_default_budget(req: DefaultBudgetUpdate):
    """Set or clear the default. Existing projects are untouched -- this only
    seeds projects created afterwards, so raising it does not silently widen
    the cap on a project someone deliberately constrained."""
    from src.services.system_settings import set_default_cost_limit

    loop = asyncio.get_running_loop()
    try:
        value = await loop.run_in_executor(
            None, set_default_cost_limit, req.default_cost_limit_usd
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    _invalidate("status")
    return {"default_cost_limit_usd": value}


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
    if not is_known_system_identity(agent_id):
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

    if req.destination == "docs":
        # Locally-uploaded content is new to the repo -- persist it as a
        # real, git-tracked file instead of the hidden staging dir below.
        design_dir = Path(base_dir) / "docs"
    else:
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
            # Set for destination="docs" so pick_next_design (queue.py)
            # resolves the design from docs/ instead of falling back to
            # its DESIGN_CONTEXT_SUBDIR-based reconstruction, which would
            # look in the wrong directory for a docs/-stored design. Left
            # unset for destination="queue", unchanged from before.
            file_path=str(filepath) if req.destination == "docs" else None,
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
                    from src.autopilot.orchestrator.engine_client import terminate_agent

                    agents = db.query(Agent).filter(Agent.current_task_id.in_(task_ids)).filter(Agent.status.in_(["working", "starting", "idle"])).all()
                    loop = asyncio.get_event_loop()
                    for agent in agents:
                        try:
                            import functools

                            await loop.run_in_executor(
                                None,
                                functools.partial(
                                    subprocess.run,
                                    ["tmux", "kill-session", "-t", agent.tmux_session_name],
                                    capture_output=True,
                                    timeout=3,
                                ),
                            )
                        except Exception:
                            pass
                        terminate_agent(agent.id, session=db)

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

            from src.autopilot.orchestrator.worktree_integration import _cleanup_worktree

            try:
                branch = _git.Repo(wt_path).active_branch.name
            except Exception:
                branch = ""
            # _cleanup_worktree does real git/filesystem work
            # (git worktree remove, dirty-check, archiving) -- offloaded
            # so it doesn't block the event loop.
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, _cleanup_worktree, wt_path, branch, Path(project_path_str), logger
            )
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

        from src.autopilot.orchestrator.state import PersistentPipelineState

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
    from src.core.database import AutopilotProject, get_db

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

    return await get_design_status(project_id, filename, base_dir, design_content, design_name)
