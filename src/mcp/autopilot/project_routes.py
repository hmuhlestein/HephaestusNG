"""Project routes: CRUD, cost tracking, design management, file browsing. — extracted from src/mcp/autopilot_api.py (backend_module_decomposition.md §3.2)."""

import asyncio
import hashlib
import logging
import math
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, field_validator

from src.core.constants import (
    DESIGN_CONTEXT_SUBDIR,
)
from src.mcp.autopilot._shared import ALLOWED_EXTENSIONS, _invalidate

# Import authentication function from server module
from src.mcp.server._shared import verify_agent_authentication

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
        raise ValueError(f"Cannot activate project — path does not exist: {new_path}")
    if not (new_path / ".git").exists():
        raise ValueError(f"Cannot activate project — not a git repository: {new_path}")

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


_project_sync_locks: Dict[str, asyncio.Lock] = {}

_project_lock_guard = asyncio.Lock()


async def _get_project_lock(project_id: str) -> asyncio.Lock:
    async with _project_lock_guard:
        if project_id not in _project_sync_locks:
            _project_sync_locks[project_id] = asyncio.Lock()
        return _project_sync_locks[project_id]


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
                workflow_type=_detect_workflow_type_for_file(name, fpath),
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
                workflow_type=_detect_workflow_type_for_file(name, fpath),
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
            "workflow_type": d.workflow_type,
        }
        for d in designs
    ]


def _detect_workflow_type_for_file(name: str, fpath: Path) -> str:
    """detect_workflow_type() needs the file's content, not just its name --
    a design discovered by filesystem sync (as opposed to the add-design API,
    which already has req.content in memory) has to read it back first. Falls
    back to "feature" if the file can't be read rather than failing the sync
    over a detection nicety."""
    from src.services.workflow_type_detection import detect_workflow_type

    try:
        content = fpath.read_text()
    except Exception:
        content = ""
    return detect_workflow_type(name, content)


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

        # Create primary ProjectRepo for multi-repo support (REQ-04)
        from src.core.repo_resolution import ensure_primary_repo

        ensure_primary_repo(db, proj)

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
            active_projects = db.query(AutopilotProject).filter_by(is_active=True).all()
            max_concurrent = get_config().autopilot.max_concurrent_projects
            if len(active_projects) >= max_concurrent:
                names = ", ".join(p.name for p in active_projects)
                raise HTTPException(
                    409,
                    f"Max concurrent projects ({max_concurrent}) reached: {names}. Stop one before starting another.",
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


# ── C10: Project Repo CRUD endpoints (REQ-24) ────────────────────────


class ProjectRepoItem(BaseModel):
    id: str
    project_id: str
    label: str
    path: str
    is_primary: bool
    created_at: str


class AddProjectRepoRequest(BaseModel):
    label: str
    path: str

    @field_validator("label")
    @classmethod
    def validate_label(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Label must not be empty")
        return v

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        if not os.path.isabs(v):
            raise ValueError("Path must be absolute")
        return v


@router.get("/projects/{project_id}/repos", response_model=List[ProjectRepoItem])
async def get_project_repos(project_id: str):
    """List all repos for a project, primary first."""
    from src.core.database import get_db
    from src.core.repo_resolution import list_repos

    with get_db() as db:
        repos = list_repos(db, project_id)
        return [
            ProjectRepoItem(
                id=r.id,
                project_id=r.project_id,
                label=r.label,
                path=r.path,
                is_primary=r.is_primary,
                created_at=r.created_at.isoformat() if r.created_at else "",
            )
            for r in repos
        ]


@router.post("/projects/{project_id}/repos", response_model=ProjectRepoItem)
async def add_project_repo(
    project_id: str,
    req: AddProjectRepoRequest,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    """Add a child repo to a project."""
    from src.core.database import ProjectRepo, get_db

    # Validate label
    label = req.label.strip()
    if not label:
        raise HTTPException(400, "Label must not be empty")

    # Validate path is absolute
    if not os.path.isabs(req.path):
        raise HTTPException(400, "Path must be absolute")

    # Validate path is a git repo
    try:
        import git as gitpython

        gitpython.Repo(req.path)
    except Exception:
        raise HTTPException(400, f"Path is not a valid git repository: {req.path}")

    with get_db() as db:
        # Verify project exists
        from src.core.database import AutopilotProject

        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")

        # Pre-check uniqueness for specific error messages
        existing_path = db.query(ProjectRepo).filter_by(project_id=project_id, path=req.path).first()
        if existing_path:
            raise HTTPException(409, f"Repo already exists for path: {req.path}")

        existing_label = db.query(ProjectRepo).filter_by(project_id=project_id, label=label).first()
        if existing_label:
            raise HTTPException(409, f"Repo already exists with label: {label}")

        repo = ProjectRepo(
            id=f"repo-{uuid.uuid4().hex[:12]}",
            project_id=project_id,
            label=label,
            path=req.path,
            is_primary=False,
        )
        db.add(repo)

        try:
            db.commit()
        except Exception as e:
            db.rollback()
            if "UNIQUE" in str(e):
                raise HTTPException(409, "Repo already exists (race condition)")
            raise

        return ProjectRepoItem(
            id=repo.id,
            project_id=repo.project_id,
            label=repo.label,
            path=repo.path,
            is_primary=repo.is_primary,
            created_at=repo.created_at.isoformat() if repo.created_at else "",
        )
