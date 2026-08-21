"""Repo resolution helpers for multi-repo project support.

Single place for the REQ-06 fallback rule (repo_id unset -> project's primary
ProjectRepo). Reused identically by commit resolution, dispatch-context
building, feature-pipeline path resolution, and commit-link validation.
"""

import logging
from typing import List, Optional

from sqlalchemy.orm import Session

from src.core.database import ProjectRepo

logger = logging.getLogger(__name__)


def resolve_primary_repo(session: Session, project_id: str) -> Optional[ProjectRepo]:
    """The project's is_primary=True ProjectRepo, or None if the project
    has no repos yet (should not happen post-migration; None only for a
    project created and never migrated, e.g. in a test fixture)."""
    return (
        session.query(ProjectRepo)
        .filter_by(project_id=project_id, is_primary=True)
        .first()
    )


def resolve_repo(
    session: Session, project_id: str, repo_id: Optional[str]
) -> Optional[ProjectRepo]:
    """REQ-06: repo_id if set and valid, else the project's primary repo.

    repo_id is looked up scoped to project_id -- a repo_id belonging to a
    different project must not resolve (cross-project repo lookup bug)."""
    if repo_id:
        repo = (
            session.query(ProjectRepo)
            .filter_by(id=repo_id, project_id=project_id)
            .first()
        )
        if repo:
            return repo
    return resolve_primary_repo(session, project_id)


def list_repos(session: Session, project_id: str) -> List[ProjectRepo]:
    """All ProjectRepos for a project, primary first, then by label."""
    return (
        session.query(ProjectRepo)
        .filter_by(project_id=project_id)
        .order_by(ProjectRepo.is_primary.desc(), ProjectRepo.label)
        .all()
    )
