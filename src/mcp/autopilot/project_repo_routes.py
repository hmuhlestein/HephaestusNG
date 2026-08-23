"""Project repo routes: add/list ProjectRepo (no update/delete in v1) --
the one new UI surface REQ-24 requires (adding/labeling child repos on a
project can't be inferred from existing data, unlike everything else in
the multi-repo design)."""

import logging
import os
import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from src.mcp.server._shared import verify_agent_authentication

logger = logging.getLogger(__name__)

router = APIRouter()


class ProjectRepoItem(BaseModel):
    id: str
    label: str
    path: str
    is_primary: bool
    created_at: str


class ProjectRepoCreate(BaseModel):
    label: str
    path: str


def _validate_repo_path(path: str) -> str:
    """Resolve + validate a child repo path is an existing git repository.
    Path.is_dir()/.git-exists check runs BEFORE any DB row is created, so a
    typo'd/nonexistent path is caught immediately rather than surfacing
    opaquely at first WorktreeManager.reload()."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise HTTPException(400, f"path is not a directory: {resolved}")
    if not (resolved / ".git").exists():
        raise HTTPException(400, "path is not a git repository")
    if not os.access(resolved, os.R_OK | os.W_OK):
        raise HTTPException(403, f"Insufficient permissions: {resolved}")
    return str(resolved)


@router.get("/projects/{project_id}/repos", response_model=List[ProjectRepoItem])
async def list_project_repos(project_id: str):
    from src.core.database import AutopilotProject, get_db
    from src.core.repo_resolution import get_project_repos

    with get_db() as db:
        if not db.query(AutopilotProject).filter_by(id=project_id).first():
            raise HTTPException(404, "Project not found")
        repos = get_project_repos(db, project_id)
        return [
            ProjectRepoItem(
                id=r.id,
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
    req: ProjectRepoCreate,
    agent_id: str = Header("ui-user", alias="X-Agent-ID"),
):
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    from src.core.database import AutopilotProject, ProjectRepo, get_db

    resolved_path = _validate_repo_path(req.path)

    with get_db() as db:
        project = db.query(AutopilotProject).filter_by(id=project_id).first()
        if not project:
            raise HTTPException(404, "Project not found")

        is_first_repo = db.query(ProjectRepo).filter_by(project_id=project_id).count() == 0
        repo = ProjectRepo(
            id=f"repo-{uuid.uuid4()}",
            project_id=project_id,
            label=req.label,
            path=resolved_path,
            is_primary=is_first_repo,
        )
        db.add(repo)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                409, f"A repo with label {req.label!r} or path {resolved_path!r} already exists on this project"
            )

        return ProjectRepoItem(
            id=repo.id,
            label=repo.label,
            path=repo.path,
            is_primary=repo.is_primary,
            created_at=repo.created_at.isoformat() if repo.created_at else "",
        )
