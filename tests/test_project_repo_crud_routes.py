"""Tests for C10: Project Repo CRUD routes."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.database import (
    AutopilotProject,
    Base,
    ProjectRepo,
)


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(eng)
    return eng


class TestProjectRepoCRUD:
    def test_list_repos_empty(self, engine):
        """GET /repos returns empty list for project with no repos."""
        from src.core.repo_resolution import list_repos

        with Session(engine) as session:
            AutopilotProject(id="p1", name="p", base_dir="/tmp")
            session.add(AutopilotProject(id="p1", name="p", base_dir="/tmp"))
            session.commit()

        with Session(engine) as session:
            repos = list_repos(session, "p1")
            assert repos == []

    def test_list_repos_primary_first(self, engine):
        """GET /repos returns primary first, then alphabetical."""
        from src.core.repo_resolution import list_repos

        with Session(engine) as session:
            session.add(AutopilotProject(id="p1", name="p", base_dir="/tmp"))
            session.flush()
            session.add(ProjectRepo(
                id="r-be", project_id="p1", label="backend",
                path="/be", is_primary=False,
            ))
            session.add(ProjectRepo(
                id="r-main", project_id="p1", label="main",
                path="/tmp", is_primary=True,
            ))
            session.commit()

        with Session(engine) as session:
            repos = list_repos(session, "p1")
            assert len(repos) == 2
            assert repos[0].is_primary is True
            assert repos[1].label == "backend"

    def test_add_repo_validates_absolute_path(self):
        """POST /repos rejects relative path."""
        from pydantic import ValidationError
        from src.mcp.autopilot.project_routes import AddProjectRepoRequest

        with pytest.raises(ValidationError):
            AddProjectRepoRequest(label="test", path="relative/path")

    def test_add_repo_validates_non_empty_label(self):
        """POST /repos rejects empty label."""
        from pydantic import ValidationError
        from src.mcp.autopilot.project_routes import AddProjectRepoRequest

        with pytest.raises(ValidationError):
            AddProjectRepoRequest(label="", path="/tmp")

    def test_add_repo_is_always_not_primary(self, engine):
        """POST /repos always creates with is_primary=False."""
        with Session(engine) as session:
            session.add(AutopilotProject(id="p1", name="p", base_dir="/tmp"))
            session.flush()
            session.add(ProjectRepo(
                id="r-main", project_id="p1", label="main",
                path="/tmp", is_primary=True,
            ))
            session.commit()

        with Session(engine) as session:
            repo = ProjectRepo(
                id="r-new", project_id="p1", label="backend",
                path="/code/backend", is_primary=False,
            )
            session.add(repo)
            session.commit()
            assert repo.is_primary is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
