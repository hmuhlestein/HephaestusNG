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

    # Shared with POST /projects, POST /autopilot/start, AutopilotService.
    # start() and both CLI commands, so every door states the same rule --
    # including its multi-repo exemption, which each used to re-implement.
    from src.core.repo_resolution import git_repo_error

    repo_problem = git_repo_error(new_path, project_id=getattr(proj, "id", None))
    if repo_problem:
        raise ValueError(f"Cannot activate project — {repo_problem}")

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
    # Omitted entirely from every construction site below until this fix --
    # every response silently read back False regardless of the actual DB
    # value, indistinguishable from a PATCH (feature_review_routes.py's
    # set_review_mode) that genuinely failed. review_mode itself is still
    # only ever written through that dedicated endpoint, not this model's
    # own PUT handler.
    review_mode: bool = False

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
    # Only rows with no file_path live in design_dir -- add_project_design
    # sets file_path for every OTHER destination (docs/spec, docs/bugfix,
    # or an arbitrary browsed folder; see its own comment), so a design
    # added there is invisible to the glob above by construction. Scoping
    # the deletion sweep to file_path-less rows only is what makes this
    # sync a no-op for those -- unscoped, every one of them looked
    # "deleted" on every sync (this endpoint, and DesignQueuePanel's own
    # 30s auto-reload timer), and got dropped from the DB despite the file
    # still sitting on disk right where it was added.
    queue_scoped = {fname: d for fname, d in existing.items() if not d.file_path}
    db_filenames = set(queue_scoped.keys())

    # Remove DB records for deleted files
    for fname in db_filenames - fs_filenames:
        removed = queue_scoped[fname]
        logger.info(
            f"[SYNC] Removing design {removed.id} ({fname!r}) for project "
            f"{project_id}: not found in {design_dir}"
        )
        db.delete(removed)

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
            "spec_key": d.spec_key,
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
    # Rejected here rather than at first use: activation already refuses a
    # non-repo (_apply_active_project), so a project created on one could
    # never be activated anyway -- it just failed later, and less clearly.
    # allow_workspace_root: a multi-repo project's repos cannot be registered
    # until the project itself exists, so at this point a child repository is
    # the only evidence that root is a workspace rather than a mistake.
    from src.core.repo_resolution import git_repo_error

    repo_problem = git_repo_error(base, allow_workspace_root=True)
    if repo_problem:
        raise HTTPException(400, repo_problem)
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
                    review_mode=getattr(p, "review_mode", False),
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
            review_mode=getattr(proj, "review_mode", False),
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
                    review_mode=getattr(proj, "review_mode", False),
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
        try:
            _apply_active_project(SimpleNamespace(base_dir=proj.base_dir, id=proj.id))
        except ValueError as e:
            # A directory that cannot back a project is the caller's problem,
            # not a server fault -- this reached the UI as an opaque 500.
            raise HTTPException(400, str(e))

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
            review_mode=getattr(proj, "review_mode", False),
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
            review_mode=getattr(proj, "review_mode", False),
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
            review_mode=getattr(proj, "review_mode", False),
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
                    resume_workflow(wf.id, force=True, session=db)
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
            review_mode=getattr(proj, "review_mode", False),
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

    from sqlalchemy.exc import IntegrityError

    from src.core.database import (
        Agent,
        AgentResult,
        AgentWorktree,
        AutopilotDesign,
        AutopilotProject,
        BoardConfig,
        CostEntry,
        DiagnosticRun,
        Feature,
        Memory,
        Phase,
        PhaseExecution,
        PhasePromptVersion,
        ProjectRepo,
        Task,
        TaskPromptOverride,
        Ticket,
        TicketCommit,
        ValidationReview,
        Workflow,
        WorkflowResult,
        get_db,
    )

    replacement_base_dir = None
    replacement_project_id = None

    # The ORM cascades on AutopilotProject.designs/.repos and
    # AutopilotDesign.features only reach AutopilotDesign/Feature/
    # ProjectRepo -- they never touch Workflow or its whole dependent
    # subtree (Task/Phase/Ticket/etc). features.workflow_id and
    # autopilot_designs.phase0_workflow_id both FK to workflows.id
    # (NO ACTION, no cascade), so deleting a project that has ever
    # actually run a design left every Workflow row behind and the
    # AutopilotDesign delete failed with a FOREIGN KEY violation --
    # caught below, but effectively making this endpoint dead for any
    # used project. Mirrors delete_feature/rerun_design/
    # remove_project_design's own cascade (same bug class, this is the
    # project-wide version of it), scoped by every workflow reachable
    # from this project's designs.
    agent_ids_to_terminate: List[str] = []
    with get_db() as db:
        design_ids = [d.id for d in db.query(AutopilotDesign.id).filter_by(project_id=project_id).all()]
        wf_ids = [w.id for w in db.query(Workflow.id).filter_by(project_id=project_id).all()]
        if design_ids:
            for w in db.query(Workflow.id).filter(Workflow.design_id.in_(design_ids)).all():
                if w.id not in wf_ids:
                    wf_ids.append(w.id)
        if wf_ids:
            agent_ids_to_terminate = [
                t.assigned_agent_id
                for t in db.query(Task).filter(
                    Task.workflow_id.in_(wf_ids),
                    Task.assigned_agent_id.isnot(None),
                )
                if t.assigned_agent_id
            ]

    # Terminate before deleting: Agent.current_task_id is a foreign key
    # (foreign_keys=ON) and terminate_agent is what clears it, same
    # reasoning as delete_feature.
    if agent_ids_to_terminate:
        from src.core.app_context import get_app_state

        server_state = get_app_state()
        for agent_id in agent_ids_to_terminate:
            await server_state.agent_manager.terminate_agent(agent_id)

    with get_db() as db:
        proj = db.query(AutopilotProject).get(project_id)
        if not proj:
            raise HTTPException(404, "Project not found")

        was_active = getattr(proj, "is_active", False)

        if wf_ids:
            task_ids = [t.id for t in db.query(Task).filter(Task.workflow_id.in_(wf_ids)).all()]
            if task_ids:
                db.query(TaskPromptOverride).filter(TaskPromptOverride.task_id.in_(task_ids)).delete(synchronize_session=False)
                db.query(ValidationReview).filter(ValidationReview.task_id.in_(task_ids)).delete(synchronize_session=False)
                db.query(AgentResult).filter(AgentResult.task_id.in_(task_ids)).delete(synchronize_session=False)
                db.query(Memory).filter(Memory.related_task_id.in_(task_ids)).delete(synchronize_session=False)
                db.query(Ticket).filter(Ticket.task_id.in_(task_ids)).delete(synchronize_session=False)
                db.query(CostEntry).filter(CostEntry.task_id.in_(task_ids)).delete(synchronize_session=False)
                # Agent.current_task_id -> tasks.id is also an enforced FK --
                # a belt-and-suspenders null-out alongside the termination
                # above (repair_service.py's rerun does the same): an agent
                # that crashed/was killed without going through the normal
                # terminate path (which clears this) can leave it dangling
                # at one of these tasks, failing the Task delete below.
                db.query(Agent).filter(Agent.current_task_id.in_(task_ids)).update(
                    {"current_task_id": None}, synchronize_session=False
                )

            db.query(DiagnosticRun).filter(DiagnosticRun.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
            db.query(WorkflowResult).filter(WorkflowResult.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
            db.query(BoardConfig).filter(BoardConfig.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
            db.query(Ticket).filter(Ticket.workflow_id.in_(wf_ids)).delete(synchronize_session=False)
            db.query(CostEntry).filter(CostEntry.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

            # tasks.phase_id and tickets.phase_id both FK to phases.id --
            # Task (and Ticket, already deleted above) must be gone before
            # Phase, not after.
            db.query(Task).filter(Task.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

            phase_ids = [p.id for p in db.query(Phase.id).filter(Phase.workflow_id.in_(wf_ids)).all()]
            if phase_ids:
                db.query(PhaseExecution).filter(PhaseExecution.phase_id.in_(phase_ids)).delete(synchronize_session=False)
                db.query(PhasePromptVersion).filter(PhasePromptVersion.phase_id.in_(phase_ids)).delete(synchronize_session=False)
            db.query(Phase).filter(Phase.workflow_id.in_(wf_ids)).delete(synchronize_session=False)

            # autopilot_designs.phase0_workflow_id also FKs to workflows.id.
            if design_ids:
                db.query(AutopilotDesign).filter(
                    AutopilotDesign.id.in_(design_ids), AutopilotDesign.phase0_workflow_id.in_(wf_ids)
                ).update({"phase0_workflow_id": None}, synchronize_session=False)

            # features.workflow_id also FKs to workflows.id -- must be gone
            # before DELETE FROM workflows runs, same reasoning as
            # delete_feature.
            if design_ids:
                db.query(Feature).filter(Feature.design_id.in_(design_ids)).delete(synchronize_session=False)

            db.query(Workflow).filter(Workflow.id.in_(wf_ids)).delete(synchronize_session=False)

        # BLOCKER fix (adversarial review): repo_id FKs on these five tables
        # have no ondelete clause, so SQLite's FK enforcement rejects the
        # cascade delete of ProjectRepo rows (AutopilotProject.repos,
        # cascade="all, delete-orphan") if any row still references one.
        # Null the FK first, in the same transaction, so the cascade delete
        # below always succeeds — matches repo_id=None's existing meaning
        # ("use the primary repo"). Only whatever the cleanup above didn't
        # already delete outright (e.g. AgentWorktree, TicketCommit, or a
        # Feature/Task with no linked workflow) can still be here.
        repo_ids = [r.id for r in db.query(ProjectRepo.id).filter_by(project_id=project_id).all()]
        if repo_ids:
            for model in (Task, Ticket, TicketCommit, AgentWorktree, Feature):
                db.query(model).filter(model.repo_id.in_(repo_ids)).update({"repo_id": None}, synchronize_session=False)

        try:
            db.delete(proj)
            db.flush()
        except IntegrityError as e:
            db.rollback()
            logger.error(f"Failed to delete project {project_id}: {e}")
            raise HTTPException(
                status_code=409,
                detail="Project cannot be deleted: it still has references that block deletion.",
            ) from e

        if was_active:
            next_proj = db.query(AutopilotProject).order_by(AutopilotProject.name).first()
            if next_proj:
                next_proj.is_active = True
                replacement_base_dir = next_proj.base_dir
                replacement_project_id = next_proj.id

    if replacement_base_dir:
        try:
            from types import SimpleNamespace

            # Pass a plain object, not the ORM instance — its session is
            # already closed here, so touching an attribute on it would
            # raise DetachedInstanceError.
            _apply_active_project(SimpleNamespace(base_dir=replacement_base_dir, id=replacement_project_id))
        except Exception as e:
            logger.error(f"Failed to activate replacement project: {e}")

    _invalidate("queue", "status", f"project_designs:{project_id}")
    return {"deleted": project_id}






























