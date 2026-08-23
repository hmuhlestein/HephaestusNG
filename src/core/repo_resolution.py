"""Resolve ProjectRepo by repo_id or project_id.

Central module for all repo resolution — every other component imports
these functions instead of reimplementing the repo_id-or-primary-fallback
logic locally.  Thread-safe: pure functions of (session, ids), no shared
mutable state.
"""

import logging
from typing import List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def ensure_primary_repo(
    session: Session,
    project: "AutopilotProject",  # noqa: F821 — forward ref
) -> "ProjectRepo":  # noqa: F821
    """Create a primary ProjectRepo for *project* if one doesn't exist yet.

    Called from (a) the migration backfill for pre-existing projects and
    (b) create_project immediately after flushing the new AutopilotProject.

    Idempotent: if the project already has a primary repo, returns it.

    Raises SQLAlchemyError on DB failure (caller's transaction rolls back).
    """
    from src.core.database import ProjectRepo

    existing = session.query(ProjectRepo).filter_by(
        project_id=project.id, is_primary=True
    ).first()
    if existing is not None:
        return existing

    repo = ProjectRepo(
        id=f"repo-{__import__('uuid').uuid4().hex[:12]}",
        project_id=project.id,
        label="main",
        path=project.base_dir,
        is_primary=True,
    )
    session.add(repo)
    session.flush()
    logger.info(
        "Created primary ProjectRepo id=%s for project=%s path=%s",
        repo.id,
        project.id,
        project.base_dir,
    )
    return repo


def resolve_primary_repo(
    session: Session,
    project_id: str,
) -> Optional["ProjectRepo"]:  # noqa: F821
    """Return the project's is_primary=True ProjectRepo, or None."""
    from src.core.database import ProjectRepo

    return (
        session.query(ProjectRepo)
        .filter_by(project_id=project_id, is_primary=True)
        .first()
    )


def resolve_repo(
    session: Session,
    project_id: str,
    repo_id: Optional[str],
) -> Optional["ProjectRepo"]:  # noqa: F821
    """Resolve a repo_id to a ProjectRepo, falling back to primary.

    REQ-06: if repo_id is None or does not match any ProjectRepo for
    this project_id, returns the primary repo.  When repo_id is non-None
    but stale, logs a WARNING before falling back.
    """
    from src.core.database import ProjectRepo

    if repo_id is not None:
        repo = (
            session.query(ProjectRepo)
            .filter_by(id=repo_id, project_id=project_id)
            .first()
        )
        if repo is not None:
            return repo
        logger.warning(
            "repo_id=%s not found for project=%s, falling back to primary repo",
            repo_id,
            project_id,
        )

    return resolve_primary_repo(session, project_id)


def list_repos(
    session: Session,
    project_id: str,
) -> List["ProjectRepo"]:  # noqa: F821
    """All ProjectRepos for a project, primary first then alphabetical."""
    from src.core.database import ProjectRepo

    return (
        session.query(ProjectRepo)
        .filter_by(project_id=project_id)
        .order_by(ProjectRepo.is_primary.desc(), ProjectRepo.label.asc())
        .all()
    )
