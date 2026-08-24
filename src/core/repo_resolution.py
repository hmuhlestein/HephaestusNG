"""Single choke point for "given a project and maybe a repo_id, what path."

Every call site that used to walk AutopilotProject.base_dir directly (or
its own copy of that logic) imports resolve_repo_path/get_project_repos/
repo_id_for_path from here instead, so repo_id=None -> primary repo is
decided in exactly one place (REQ-06).
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from src.core.database import ProjectRepo

logger = logging.getLogger(__name__)


class RepoNotFoundError(Exception):
    """repo_id was set on a row but no matching ProjectRepo exists for that
    project. Raised by resolve_repo_path -- NEVER silently falls back to
    primary in this case, unlike the repo_id=None case, because silently
    substituting a different repo's path would make git operations target
    the wrong filesystem tree with no visible error."""

    def __init__(self, repo_id: str, project_id: str):
        self.repo_id = repo_id
        self.project_id = project_id
        super().__init__(f"repo_id={repo_id!r} not found for project {project_id!r}")


def resolve_repo_path(db: Session, project_id: str, repo_id: Optional[str]) -> Path:
    """repo_id set -> that ProjectRepo's path (raises RepoNotFoundError if it
    doesn't belong to project_id). repo_id None -> the project's primary
    ProjectRepo's path. No primary row and no ProjectRepo rows at all
    (pre-migration edge case, should not happen post-migration) -> falls
    back to AutopilotProject.base_dir directly, logged at WARNING. Raises
    ValueError if project_id itself doesn't resolve to a project.
    """
    from src.core.database import AutopilotProject, ProjectRepo

    project = db.query(AutopilotProject).filter_by(id=project_id).first()
    if project is None:
        raise ValueError(f"project_id={project_id!r} does not resolve to a project")

    if repo_id is not None:
        repo = db.query(ProjectRepo).filter_by(id=repo_id, project_id=project_id).first()
        if repo is None:
            raise RepoNotFoundError(repo_id, project_id)
        return Path(repo.path)

    primary = db.query(ProjectRepo).filter_by(project_id=project_id, is_primary=True).first()
    if primary is not None:
        return Path(primary.path)

    logger.warning(f"[REPO-RESOLUTION] project {project_id!r} has no ProjectRepo rows (migration not run?) -- falling back to AutopilotProject.base_dir")
    return Path(project.base_dir)


def get_project_repos(db: Session, project_id: str) -> List["ProjectRepo"]:
    """All ProjectRepo rows for a project, primary first. Empty list for
    malformed/missing project_id (never raises) -- callers use this for
    display (prompt injection, frontend), where a defensive empty list is
    correct; resolve_repo_path is the strict variant for write paths."""
    from src.core.database import ProjectRepo

    return db.query(ProjectRepo).filter_by(project_id=project_id).order_by(ProjectRepo.is_primary.desc(), ProjectRepo.label.asc()).all()


def repo_id_for_path(db: Session, project_id: str, file_path: str) -> Optional[str]:
    """Which ProjectRepo (if any) a given absolute path falls under --
    longest-prefix match against each ProjectRepo.path for the project.
    Returns None if no repo's path is a prefix of file_path."""
    resolved = Path(file_path).resolve()
    repos = get_project_repos(db, project_id)

    matches = []
    for repo in repos:
        repo_path = Path(repo.path).resolve()
        if resolved == repo_path or resolved.is_relative_to(repo_path):
            matches.append(repo)

    if not matches:
        return None
    # Tie-break: longest path first, then prefer primary, then alphabetical label
    return str(max(matches, key=lambda r: (len(r.path), r.is_primary, r.label)).id)
