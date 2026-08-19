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

from src.core.constants import (
    CONTEXT_DIR_NAME,
    DESIGN_CONTEXT_SUBDIR,
    DESIGN_WORKFLOW_DEFINITION_IDS,
    GOTO_REASON_PREFIX,
    PHASE0_DEFINITION_IDS,
)
from src.mcp.autopilot._shared import ALLOWED_EXTENSIONS, _cached, _extract_pr_url, _invalidate, _safe_path, _store
from src.mcp.autopilot.feature_routes import _find_archived_feature_report

# Import authentication function from server module
from src.mcp.server._shared import KNOWN_SYSTEM_AGENTS, verify_agent_authentication
from src.mcp.server.oauth_routes import _check_rate_limit

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
    config.main_repo_path = new_path
    config.project_root = new_path


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

    # Initialize codegraph index if codegraph is installed and .codegraph
    # doesn't already exist in the project directory.
    try:
        import subprocess as _sp
        codegraph_dir = Path(resolved) / ".codegraph"
        if not codegraph_dir.exists():
            result = _sp.run(
                ["codegraph", "init", "."],
                cwd=resolved,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                logger.info(f"CodeGraph initialized for project {proj.name} at {resolved}")
            else:
                logger.debug(f"CodeGraph init skipped/failed for {proj.name}: {result.stderr[:200]}")
    except FileNotFoundError:
        logger.debug("codegraph not installed, skipping index initialization")
    except Exception as e:
        logger.debug(f"CodeGraph init skipped for {proj.name}: {e}")

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
            max_concurrent = get_config().max_concurrent_projects
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
                        # "Z" suffix required: these are naive datetimes that
                        # ARE utc (see the utc-only invariant), but plain
                        # .isoformat() on a naive datetime carries no
                        # timezone marker at all -- the frontend's
                        # `new Date(iso_string)` then parses it as LOCAL
                        # time, not UTC. On a host whose local timezone
                        # trails UTC, that makes the parsed timestamp look
                        # HOURS in the future relative to real now(),
                        # producing a large negative "elapsed" display.
                        "created_at": t.created_at.isoformat() + "Z" if t.created_at else None,
                        "completed_at": t.completed_at.isoformat() + "Z" if t.completed_at else None,
                        "agent_id": t.assigned_agent_id,
                        "agent_status": agent.status if agent else None,
                        "cli_type": agent.cli_type if agent else None,
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
                    agent_cli_type = None
                    if t.assigned_agent_id:
                        agent = db.query(Agent).filter_by(id=t.assigned_agent_id).first()
                        agent_status = agent.status if agent else None
                        agent_cli_type = agent.cli_type if agent else None
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
                            # "Z" suffix required -- see the sibling task-list
                            # builder above for why: a naive-but-UTC datetime
                            # serialized without a timezone marker gets
                            # misparsed as local time by the frontend's
                            # `new Date(...)`, producing a large negative
                            # "elapsed" display on hosts behind UTC.
                            "created_at": t.created_at.isoformat() + "Z" if t.created_at else None,
                            "completed_at": t.completed_at.isoformat() + "Z" if t.completed_at else None,
                            "agent_id": t.assigned_agent_id,
                            "agent_status": agent_status,
                            "cli_type": agent_cli_type,
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
                            has_report = (Path(feat_wf.working_directory) / CONTEXT_DIR_NAME / "doc_review" / "feature_report.html").is_file() or \
                                         (Path(feat_wf.working_directory) / CONTEXT_DIR_NAME / "feature_report.html").is_file() or \
                                         (Path(feat_wf.working_directory) / "docs" / "doc_review" / "feature_report.html").is_file() or \
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
                    phase0_has_report = (Path(phase0_wf.working_directory) / CONTEXT_DIR_NAME / "doc_review" / "feature_report.html").is_file() or \
                                        (Path(phase0_wf.working_directory) / CONTEXT_DIR_NAME / "feature_report.html").is_file()
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



