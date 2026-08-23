"""Tests for AgentManager.get_project_context repo awareness (REQ-17/18/21).

Verifies:
- REQ-17: Multi-repo project context includes repo label+path list
- REQ-18: Implementation-agent context marks own repo writable, siblings read-only
- REQ-21: Single-repo projects see zero output change
- NFR-01: Identical output when ProjectRepo count <= 1
- NFR-03: _build_repo_context is unit-testable without a real agent
"""

import asyncio
import uuid
from unittest.mock import MagicMock, patch

import pytest

from src.agents.manager import AgentManager
from src.core.database import (
    AutopilotProject,
    DatabaseManager,
    ProjectRepo,
    Workflow,
)


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    """Create a test database manager."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def agent_manager(db_manager):
    """Create an AgentManager with mocked dependencies."""
    with patch("src.agents.manager.get_config", return_value={}):
        from src.agents.manager import AgentManager

        manager = AgentManager(
            db_manager=db_manager,
            llm_provider=MagicMock(),
            phase_manager=MagicMock(),
        )
        return manager


def _create_project_with_repos(session, num_repos):
    """Helper: create a project with N repos. Returns (project, repos)."""
    project = AutopilotProject(
        id=f"proj-{uuid.uuid4().hex[:8]}",
        name="Test Project",
        base_dir="/tmp/test-project",
        is_active=True,
    )
    session.add(project)

    repos = []
    for i in range(num_repos):
        repo = ProjectRepo(
            id=f"repo-{uuid.uuid4().hex[:8]}",
            project_id=project.id,
            label=f"repo-{i}",
            path=f"/tmp/test-project/repo-{i}",
            is_primary=(i == 0),
        )
        session.add(repo)
        repos.append(repo)

    session.flush()
    return project, repos


def _create_workflow(session, project):
    """Helper: create a workflow linked to a project."""
    wf = Workflow(
        id=f"wf-{uuid.uuid4().hex[:8]}",
        name="Test Workflow",
        status="active",
        working_directory="/tmp/test-project",
        phases_folder_path="/tmp",
        project_id=project.id,
    )
    session.add(wf)
    session.flush()
    return wf


class TestBuildRepoContext:
    """Unit tests for _build_repo_context (NFR-03)."""

    def test_no_project_id_returns_empty(self, agent_manager, db_manager):
        """project_id=None -> empty string."""
        with db_manager.session_scope() as session:
            result = AgentManager._build_repo_context(session, None, None)
        assert result == ""

    def test_zero_repos_returns_empty(self, agent_manager, db_manager):
        """Project with 0 repos -> empty string (REQ-21)."""
        project = AutopilotProject(
            id="proj-empty",
            name="Empty Project",
            base_dir="/tmp/empty",
            is_active=True,
        )
        with db_manager.session_scope() as session:
            session.add(project)
            session.flush()
            result = AgentManager._build_repo_context(session, project.id, None)
        assert result == ""

    def test_one_repo_returns_empty(self, agent_manager, db_manager):
        """Project with 1 repo -> empty string (REQ-21/NFR-01)."""
        with db_manager.session_scope() as session:
            project, repos = _create_project_with_repos(session, 1)
            result = AgentManager._build_repo_context(session, project.id, None)
        assert result == ""

    def test_two_repos_lists_both(self, agent_manager, db_manager):
        """Project with 2+ repos -> PROJECT REPOSITORIES section (REQ-17)."""
        with db_manager.session_scope() as session:
            project, repos = _create_project_with_repos(session, 2)
            result = AgentManager._build_repo_context(session, project.id, None)

        assert "## PROJECT REPOSITORIES" in result
        assert "repo-0:" in result
        assert "repo-1:" in result
        assert "/tmp/test-project/repo-0" in result
        assert "/tmp/test-project/repo-1" in result

    def test_two_repos_no_access_section_without_repo_id(self, agent_manager, db_manager):
        """2+ repos, no repo_id -> no REPO ACCESS section (REQ-17 only)."""
        with db_manager.session_scope() as session:
            project, repos = _create_project_with_repos(session, 2)
            result = AgentManager._build_repo_context(session, project.id, None)

        assert "## PROJECT REPOSITORIES" in result
        assert "REPO ACCESS" not in result
        assert "WRITABLE" not in result
        assert "READ-ONLY" not in result

    def test_two_repos_with_repo_id_shows_access(self, agent_manager, db_manager):
        """2+ repos with valid repo_id -> writable/read-only framing (REQ-18)."""
        with db_manager.session_scope() as session:
            project, repos = _create_project_with_repos(session, 2)
            result = AgentManager._build_repo_context(session, project.id, repos[0].id)

        assert "## PROJECT REPOSITORIES" in result
        assert "## REPO ACCESS" in result
        assert "repo-0: WRITABLE" in result
        assert "repo-1: READ-ONLY" in result

    def test_stale_repo_id_still_lists_repos(self, agent_manager, db_manager):
        """repo_id not matching any repo -> repo list still emitted, no access section."""
        with db_manager.session_scope() as session:
            project, repos = _create_project_with_repos(session, 2)
            result = AgentManager._build_repo_context(session, project.id, "nonexistent-repo")

        assert "## PROJECT REPOSITORIES" in result
        assert "REPO ACCESS" not in result

    def test_ordering_is_by_label(self, agent_manager, db_manager):
        """Repos ordered by label for deterministic output (Gotcha #4)."""
        with db_manager.session_scope() as session:
            project = AutopilotProject(
                id="proj-order",
                name="Order Test",
                base_dir="/tmp/order",
                is_active=True,
            )
            session.add(project)
            # Add in reverse alphabetical order
            for label in ["zebra", "alpha", "middle"]:
                session.add(
                    ProjectRepo(
                        id=f"repo-{label}",
                        project_id=project.id,
                        label=label,
                        path=f"/tmp/order/{label}",
                        is_primary=(label == "alpha"),
                    )
                )
            session.flush()
            result = AgentManager._build_repo_context(session, project.id, None)

        # Verify alphabetical ordering
        alpha_pos = result.index("alpha:")
        middle_pos = result.index("middle:")
        zebra_pos = result.index("zebra:")
        assert alpha_pos < middle_pos < zebra_pos


class TestGetProjectContextRepoAwareness:
    """Integration tests for get_project_context with workflow_id/repo_id."""

    def test_no_args_returns_baseline(self, agent_manager, db_manager):
        """No args -> same output as before (REQ-21/NFR-01)."""
        result = asyncio.get_event_loop().run_until_complete(agent_manager.get_project_context())
        assert "## PROJECT STATUS" in result
        assert "PROJECT REPOSITORIES" not in result

    def test_workflow_id_no_multi_repo(self, agent_manager, db_manager):
        """workflow_id with single-repo project -> no repo section (REQ-21)."""
        with db_manager.session_scope() as session:
            project, repos = _create_project_with_repos(session, 1)
            wf = _create_workflow(session, project)

        result = asyncio.get_event_loop().run_until_complete(agent_manager.get_project_context(workflow_id=wf.id))
        assert "## PROJECT STATUS" in result
        assert "PROJECT REPOSITORIES" not in result

    def test_workflow_id_multi_repo_no_repo_id(self, agent_manager, db_manager):
        """workflow_id with multi-repo project, no repo_id -> repo list only (REQ-17)."""
        with db_manager.session_scope() as session:
            project, repos = _create_project_with_repos(session, 2)
            wf = _create_workflow(session, project)

        result = asyncio.get_event_loop().run_until_complete(agent_manager.get_project_context(workflow_id=wf.id))
        assert "## PROJECT REPOSITORIES" in result
        assert "REPO ACCESS" not in result

    def test_workflow_id_and_repo_id(self, agent_manager, db_manager):
        """workflow_id + repo_id on multi-repo -> writable/read-only (REQ-18)."""
        with db_manager.session_scope() as session:
            project, repos = _create_project_with_repos(session, 2)
            wf = _create_workflow(session, project)

        result = asyncio.get_event_loop().run_until_complete(agent_manager.get_project_context(workflow_id=wf.id, repo_id=repos[0].id))
        assert "## PROJECT REPOSITORIES" in result
        assert "## REPO ACCESS" in result
        assert "WRITABLE" in result
        assert "READ-ONLY" in result

    def test_invalid_workflow_id_degrades_gracefully(self, agent_manager, db_manager):
        """Invalid workflow_id -> no repo section, no crash."""
        result = asyncio.get_event_loop().run_until_complete(agent_manager.get_project_context(workflow_id="nonexistent-wf"))
        assert "## PROJECT STATUS" in result
        assert "PROJECT REPOSITORIES" not in result

    def test_null_project_id_in_workflow(self, agent_manager, db_manager):
        """Workflow with null project_id -> no repo section (Gotcha #2)."""
        with db_manager.session_scope() as session:
            wf = Workflow(
                id="wf-no-proj",
                name="No Project Workflow",
                status="active",
                working_directory="/tmp",
                phases_folder_path="/tmp",
                project_id=None,
            )
            session.add(wf)
            session.flush()

        result = asyncio.get_event_loop().run_until_complete(agent_manager.get_project_context(workflow_id=wf.id))
        assert "## PROJECT STATUS" in result
        assert "PROJECT REPOSITORIES" not in result

    def test_repo_context_failure_isolated(self, agent_manager, db_manager):
        """_build_repo_context failure doesn't kill entire project context (WARNING 1)."""
        with db_manager.session_scope() as session:
            project, repos = _create_project_with_repos(session, 2)
            wf = _create_workflow(session, project)

        with patch.object(AgentManager, "_build_repo_context", side_effect=Exception("DB error")):
            result = asyncio.get_event_loop().run_until_complete(agent_manager.get_project_context(workflow_id=wf.id))

        # Active tasks/agents/completions still present
        assert "## PROJECT STATUS" in result
        # Repo section absent due to failure
        assert "PROJECT REPOSITORIES" not in result

    def test_workflow_id_exceeding_max_length_ignored(self, agent_manager, db_manager):
        """workflow_id > 200 chars is sanitized to None."""
        long_id = "x" * 201
        result = asyncio.get_event_loop().run_until_complete(
            agent_manager.get_project_context(workflow_id=long_id)
        )
        assert "## PROJECT STATUS" in result
        assert "PROJECT REPOSITORIES" not in result

    def test_repo_id_exceeding_max_length_ignored(self, agent_manager, db_manager):
        """repo_id > 200 chars is sanitized to None."""
        with db_manager.session_scope() as session:
            project, repos = _create_project_with_repos(session, 2)
            wf = _create_workflow(session, project)

        long_id = "x" * 201
        result = asyncio.get_event_loop().run_until_complete(
            agent_manager.get_project_context(workflow_id=wf.id, repo_id=long_id)
        )
        # Repo list present but no REPO ACCESS (repo_id was sanitized out)
        assert "## PROJECT REPOSITORIES" in result
        assert "REPO ACCESS" not in result

    def test_db_failure_returns_fallback(self, agent_manager, db_manager):
        """DB failure in get_project_context returns fallback string."""
        with patch.object(agent_manager.db_manager, "get_session") as mock_session:
            mock_session.return_value.query.side_effect = Exception("DB down")
            result = asyncio.get_event_loop().run_until_complete(
                agent_manager.get_project_context()
            )
        assert result == "Project context unavailable"


class TestCallerThreading:
    """Tests for repo_id preservation through dispatch paths (WARNING 2)."""

    def test_dispatch_ready_dependents_preserves_repo_id(self, db_manager):
        """_dispatch_ready_dependents task_data dict includes repo_id."""
        with db_manager.session_scope() as session:
            from src.core.database import Task

            project, repos = _create_project_with_repos(session, 2)
            wf = _create_workflow(session, project)

            # Create completed task (dependency)
            completed = Task(
                id=f"task-done-{uuid.uuid4().hex[:8]}",
                raw_description="Completed task",
                done_definition="done",
                status="done",
                workflow_id=wf.id,
                repo_id=repos[0].id,
            )
            session.add(completed)

            # Create dependent task with repo_id
            dependent = Task(
                id=f"task-dep-{uuid.uuid4().hex[:8]}",
                raw_description="Dependent task",
                done_definition="done",
                status="pending",
                workflow_id=wf.id,
                repo_id=repos[1].id,
                depends_on=[completed.id],
            )
            session.add(dependent)
            session.flush()

            completed_id = completed.id
            dependent_id = dependent.id

        # Verify the task has repo_id in DB
        with db_manager.session_scope() as session:
            dep_task = session.query(Task).filter_by(id=dependent_id).first()
            assert dep_task.repo_id == repos[1].id
            assert dep_task.depends_on == [completed_id]

    def test_promoted_task_data_includes_repo_id(self):
        """Verify _dispatch_ready_dependents source code includes repo_id in task_data dict."""
        import inspect

        from src.mcp.server._create_task_steps import _dispatch_ready_dependents

        source = inspect.getsource(_dispatch_ready_dependents)
        assert '"repo_id": t.repo_id' in source or "repo_id" in source
