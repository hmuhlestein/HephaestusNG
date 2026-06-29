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


@router.get("/active", response_model=Optional[ProjectItem])
async def get_active_project():
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        proj = db.query(AutopilotProject).filter_by(is_active=True).first()
        if not proj:
            return None
        count = db.query(AutopilotDesign).filter_by(project_id=proj.id).count()
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


@router.post("", response_model=ProjectItem)
async def create_project(req: ProjectCreate):
    from src.core.database import AutopilotProject, get_db

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

        proj = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:12]}",
            name=req.name,
            base_dir=resolved,
            is_default=req.is_default or is_first,
            is_active=is_first,
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

    # Apply active project OUTSIDE the DB session
    if is_first:
        try:
            _apply_active_project(proj)
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

    with get_db() as db:
        proj = db.query(AutopilotProject).filter_by(id=project_id).first()
        if not proj:
            raise HTTPException(404, f"Project not found: {project_id}")

        # Clear all active flags
        db.query(AutopilotProject).update({"is_active": False})

        # Set target active
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


def _apply_active_project(proj):
    """Apply project path to runtime config and reinitialize WorktreeManager.

    Validates the path is a valid git repo BEFORE mutating config.
    If reload fails, config is not mutated.
    """
    from src.core.simple_config import get_config
    from src.mcp.server import server_state

    config = get_config()
    new_path = Path(proj.base_dir)

    # Validate first — don't mutate config if path is invalid
    if server_state.branch_manager:
        try:
            server_state.branch_manager.reload(new_path)
        except Exception as e:
            logger.error(f"Failed to reload WorktreeManager for {new_path}: {e}")
            raise ValueError(
                f"Cannot activate project — not a valid git repository: {new_path}"
            )

    # Only mutate config after successful reload
    config.main_repo_path = new_path
    config.project_root = new_path
