"""Tests for C4: Agent Prompt Context (get_project_context multi-repo)."""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.database import (
    Agent,
    AutopilotProject,
    Base,
    ProjectRepo,
    Task,
    Workflow,
)


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(eng)
    return eng


def _seed_multi_repo(session):
    """Create a multi-repo project with workflows and tasks."""
    project = AutopilotProject(
        id="proj-mr", name="multi", base_dir="/workspace"
    )
    session.add(project)
    session.flush()

    repo_main = ProjectRepo(
        id="repo-main", project_id="proj-mr", label="main",
        path="/workspace", is_primary=True,
    )
    repo_be = ProjectRepo(
        id="repo-be", project_id="proj-mr", label="backend",
        path="/code/backend", is_primary=False,
    )
    session.add_all([repo_main, repo_be])
    session.flush()

    wf = Workflow(
        id="wf-1", name="wf", status="active",
        project_id="proj-mr", phases_folder_path="/tmp",
    )
    session.add(wf)
    session.flush()

    return project, wf, repo_main, repo_be


class TestBuildRepoContext:
    def test_single_repo_returns_none(self, engine):
        """REQ-21: single-repo project emits no additional text."""
        from src.agents.manager import AgentManager

        with Session(engine) as session:
            p = AutopilotProject(id="p1", name="p", base_dir="/tmp")
            session.add(p)
            session.flush()
            ProjectRepo(
                id="r1", project_id="p1", label="main",
                path="/tmp", is_primary=True,
            )
            session.add(session.get(ProjectRepo, "r1") or ProjectRepo(
                id="r1", project_id="p1", label="main",
                path="/tmp", is_primary=True,
            ))
            session.flush()

            result = AgentManager._build_repo_context(session, "p1", None)
            assert result is None

    def test_multi_repo_architect_mode(self, engine):
        """REQ-17: architect mode lists repos plainly, no writable/read-only."""
        from src.agents.manager import AgentManager

        with Session(engine) as session:
            _seed_multi_repo(session)
            session.commit()

            result = AgentManager._build_repo_context(session, "proj-mr", None)
            assert result is not None
            assert "## PROJECT REPOS" in result
            assert "main:" in result
            assert "backend:" in result
            assert "WRITABLE" not in result
            assert "read-only" not in result

    def test_multi_repo_implementation_mode(self, engine):
        """REQ-18: implementation mode marks writable vs read-only."""
        from src.agents.manager import AgentManager

        with Session(engine) as session:
            _seed_multi_repo(session)
            session.commit()

            result = AgentManager._build_repo_context(session, "proj-mr", "repo-be")
            assert result is not None
            assert "backend (WRITABLE)" in result
            assert "main (read-only reference)" in result

    def test_no_project_id_returns_none(self, engine):
        """project_id=None returns None."""
        from src.agents.manager import AgentManager

        with Session(engine) as session:
            result = AgentManager._build_repo_context(session, None, None)
            assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
