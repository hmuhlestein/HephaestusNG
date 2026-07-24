"""Project management API — unified project endpoints."""

import logging
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectItem(BaseModel):
    id: str
    name: str
    base_dir: str
    is_default: bool
    is_active: bool
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


def _validate_base_dir(base_dir: str) -> str:
    p = Path(base_dir).expanduser().resolve()
    if not p.exists():
        raise HTTPException(400, f"Directory does not exist: {p}")
    if not p.is_dir():
        raise HTTPException(400, f"Not a directory: {p}")
    return str(p)


@router.get("", response_model=List[ProjectItem])
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
                )
            )
        return result


@router.get("/active", response_model=List[ProjectItem])
async def get_active_project():
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


@router.post("", response_model=ProjectItem)
async def create_project(req: ProjectCreate):
    from src.core.database import AutopilotProject, get_db
    from src.core.simple_config import get_config

    resolved = _validate_base_dir(req.base_dir)

    with get_db() as db:
        existing = db.query(AutopilotProject).filter_by(base_dir=resolved).first()
        if existing:
            raise HTTPException(
                409, f"Project already exists for directory: {resolved}"
            )

        if req.is_default:
            db.query(AutopilotProject).update({"is_default": False})

        # First project is automatically active
        is_first = db.query(AutopilotProject).count() == 0
        want_active = is_first
        if want_active:
            active_count = db.query(AutopilotProject).filter_by(is_active=True).count()
            max_concurrent = get_config().max_concurrent_projects
            if active_count >= max_concurrent:
                # Activation here is a side effect of project creation, not
                # an explicit user "activate" request -- create it inactive
                # rather than reject the whole creation like
                # activate_project's 409 does.
                logger.warning(
                    f"Not auto-activating new project {req.name!r}: "
                    f"max_concurrent_projects ({max_concurrent}) already reached"
                )
                want_active = False

        proj = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:12]}",
            name=req.name,
            base_dir=resolved,
            is_default=req.is_default or is_first,
            is_active=want_active,
        )
        db.add(proj)
        db.flush()

        result = ProjectItem(
            id=proj.id,
            name=proj.name,
            base_dir=proj.base_dir,
            is_default=proj.is_default,
            is_active=proj.is_active,
            design_count=0,
            created_at=proj.created_at.isoformat() if proj.created_at else "",
            updated_at=proj.updated_at.isoformat() if proj.updated_at else "",
        )

    # Apply active project OUTSIDE the DB session — proj is detached once the
    # session above closes, so pass a plain object instead of touching the
    # ORM instance (accessing proj.base_dir here would raise
    # DetachedInstanceError, silently swallowed by the except below).
    if want_active:
        try:
            from types import SimpleNamespace

            _apply_active_project(SimpleNamespace(base_dir=result.base_dir))
        except Exception as e:
            logger.warning(f"Created project but failed to activate at runtime: {e}")

    return result


@router.put("/{project_id}", response_model=ProjectItem)
async def update_project(project_id: str, req: ProjectUpdate):
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).filter_by(id=project_id).first()
        if not proj:
            raise HTTPException(404, f"Project not found: {project_id}")

        if req.name is not None:
            proj.name = req.name
        if req.base_dir is not None:
            resolved = _validate_base_dir(req.base_dir)
            existing = (
                db.query(AutopilotProject)
                .filter(
                    AutopilotProject.base_dir == resolved,
                    AutopilotProject.id != project_id,
                )
                .first()
            )
            if existing:
                raise HTTPException(
                    409, f"Another project already uses directory: {resolved}"
                )
            proj.base_dir = resolved

        db.flush()
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
        )


@router.delete("/{project_id}")
async def delete_project(project_id: str):
    from src.core.database import AutopilotProject, get_db

    replacement_proj = None

    with get_db() as db:
        proj = db.query(AutopilotProject).filter_by(id=project_id).first()
        if not proj:
            raise HTTPException(404, f"Project not found: {project_id}")

        was_active = getattr(proj, "is_active", False)
        db.delete(proj)
        db.flush()

        # If deleted project was active, find a replacement
        if was_active:
            next_proj = (
                db.query(AutopilotProject).order_by(AutopilotProject.name).first()
            )
            if next_proj:
                next_proj.is_active = True
                replacement_proj = {
                    "id": next_proj.id,
                    "name": next_proj.name,
                    "base_dir": next_proj.base_dir,
                }

    # Apply replacement OUTSIDE the DB session
    if replacement_proj:
        try:
            from types import SimpleNamespace

            _apply_active_project(SimpleNamespace(**replacement_proj))
        except Exception as e:
            logger.error(f"Failed to activate replacement project: {e}")

        return {"status": "deleted", "id": project_id}


@router.post("/{project_id}/activate", response_model=ProjectItem)
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
        _apply_active_project(proj)

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


@router.post("/{project_id}/deactivate", response_model=ProjectItem)
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
