"""Tests for commit resolution with multi-repo support.

REQ-14: _resolve_repo_path_for_commit resolves via task.repo_id chain
REQ-15: CommitDiffResponse includes repo_id and repo_label
REQ-06: Falls back to primary repo when task.repo_id is None
"""

import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.database import (
    AutopilotProject,
    Base,
    ProjectRepo,
    RepoResolutionError,
    Task,
    Ticket,
    TicketCommit,
    Workflow,
    resolve_project_repo,
)


@pytest.fixture
def db_engine():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _skip_fk(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """Create a database session for testing."""
    SessionLocal = sessionmaker(bind=db_engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def multi_repo_project(db_session):
    """Create a project with two repos, a workflow, and tasks."""
    project_id = f"proj-{uuid.uuid4()}"
    backend_repo_id = f"repo-{uuid.uuid4()}"
    frontend_repo_id = f"repo-{uuid.uuid4()}"
    workflow_id = f"wf-{uuid.uuid4()}"
    task_id = f"task-{uuid.uuid4()}"
    ticket_id = f"ticket-{uuid.uuid4()}"

    # Create project
    project = AutopilotProject(
        id=project_id,
        name="Multi-Repo Project",
        base_dir="/tmp/multi-repo",
    )
    db_session.add(project)

    # Create repos
    backend_repo = ProjectRepo(
        id=backend_repo_id,
        project_id=project_id,
        label="backend",
        path="/tmp/multi-repo/backend",
        is_primary=True,
    )
    frontend_repo = ProjectRepo(
        id=frontend_repo_id,
        project_id=project_id,
        label="frontend",
        path="/tmp/multi-repo/frontend",
        is_primary=False,
    )
    db_session.add_all([backend_repo, frontend_repo])

    # Create workflow
    workflow = Workflow(
        id=workflow_id,
        project_id=project_id,
        definition_id="autopilot",
        name="Test Workflow",
        phases_folder_path="/tmp/phases",
        status="active",
    )
    db_session.add(workflow)

    # Create task with repo_id
    task = Task(
        id=task_id,
        raw_description="Implement backend API",
        done_definition="API endpoints working",
        workflow_id=workflow_id,
        repo_id=backend_repo_id,
    )
    db_session.add(task)

    # Create ticket linked to task
    ticket = Ticket(
        id=ticket_id,
        workflow_id=workflow_id,
        created_by_agent_id="agent-1",
        title="Backend API",
        description="Implement REST API",
        ticket_type="feature",
        priority="high",
        status="in_progress",
        task_id=task_id,
    )
    db_session.add(ticket)

    db_session.commit()

    return {
        "project_id": project_id,
        "backend_repo_id": backend_repo_id,
        "frontend_repo_id": frontend_repo_id,
        "workflow_id": workflow_id,
        "task_id": task_id,
        "ticket_id": ticket_id,
    }


class TestResolveRepoPathForCommit:
    """Test _resolve_repo_path_for_commit with multi-repo support."""

    def test_resolves_via_task_repo_id(self, db_session, db_engine, multi_repo_project):
        """REQ-14: Resolves via task.repo_id when task_id is set."""
        from contextlib import contextmanager
        from src.mcp.tickets_api import _resolve_repo_path_for_commit

        data = multi_repo_project
        commit_sha = "abc12345"

        # Create commit linked to ticket
        commit = TicketCommit(
            id=f"tc-{uuid.uuid4()}",
            ticket_id=data["ticket_id"],
            agent_id="agent-1",
            commit_sha=commit_sha,
            commit_message="Add API endpoints",
            commit_timestamp=datetime.utcnow(),
        )
        db_session.add(commit)
        db_session.commit()

        # Mock get_db at the source module since the function imports it locally
        @contextmanager
        def mock_get_db():
            yield db_session

        with patch("src.core.database.get_db", mock_get_db):
            result = _resolve_repo_path_for_commit(commit_sha)

        assert result is not None
        path, repo_id, label = result
        assert path == "/tmp/multi-repo/backend"
        assert repo_id == data["backend_repo_id"]
        assert label == "backend"

    def test_resolves_via_ticket_repo_id(self, db_session, db_engine, multi_repo_project):
        """REQ-14: Falls back to ticket.repo_id when task_id is None."""
        from contextlib import contextmanager
        from src.mcp.tickets_api import _resolve_repo_path_for_commit

        data = multi_repo_project
        commit_sha = "def67890"

        # Update ticket to have repo_id but no task_id
        ticket = db_session.query(Ticket).filter_by(id=data["ticket_id"]).first()
        ticket.task_id = None
        ticket.repo_id = data["frontend_repo_id"]
        db_session.commit()

        # Create commit
        commit = TicketCommit(
            id=f"tc-{uuid.uuid4()}",
            ticket_id=data["ticket_id"],
            agent_id="agent-1",
            commit_sha=commit_sha,
            commit_message="Add frontend components",
            commit_timestamp=datetime.utcnow(),
        )
        db_session.add(commit)
        db_session.commit()

        @contextmanager
        def mock_get_db():
            yield db_session

        with patch("src.core.database.get_db", mock_get_db):
            result = _resolve_repo_path_for_commit(commit_sha)

        assert result is not None
        path, repo_id, label = result
        assert path == "/tmp/multi-repo/frontend"
        assert repo_id == data["frontend_repo_id"]
        assert label == "frontend"

    def test_falls_back_to_primary_repo(self, db_session, db_engine, multi_repo_project):
        """REQ-06: Falls back to primary repo when no repo_id is set."""
        from contextlib import contextmanager
        from src.mcp.tickets_api import _resolve_repo_path_for_commit

        data = multi_repo_project
        commit_sha = "ghi11111"

        # Update ticket to have no repo_id and no task_id
        ticket = db_session.query(Ticket).filter_by(id=data["ticket_id"]).first()
        ticket.task_id = None
        ticket.repo_id = None
        db_session.commit()

        # Create commit
        commit = TicketCommit(
            id=f"tc-{uuid.uuid4()}",
            ticket_id=data["ticket_id"],
            agent_id="agent-1",
            commit_sha=commit_sha,
            commit_message="Fix bug",
            commit_timestamp=datetime.utcnow(),
        )
        db_session.add(commit)
        db_session.commit()

        @contextmanager
        def mock_get_db():
            yield db_session

        with patch("src.core.database.get_db", mock_get_db):
            result = _resolve_repo_path_for_commit(commit_sha)

        assert result is not None
        path, repo_id, label = result
        assert path == "/tmp/multi-repo/backend"  # Primary repo
        assert repo_id == data["backend_repo_id"]
        assert label == "backend"

    def test_returns_none_for_unlinked_commit(self, db_session, db_engine):
        """Returns None when commit isn't linked to any ticket."""
        from src.mcp.tickets_api import _resolve_repo_path_for_commit

        with patch("src.mcp.tickets_api.get_db") as mock_get_db:
            mock_get_db.return_value.__enter__ = MagicMock(return_value=db_session)
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            result = _resolve_repo_path_for_commit("nonexistent_sha")

        assert result is None

    def test_returns_none_on_error(self, db_session, db_engine):
        """Returns None (never raises) on any error."""
        from src.mcp.tickets_api import _resolve_repo_path_for_commit

        with patch("src.mcp.tickets_api.get_db") as mock_get_db:
            mock_get_db.side_effect = Exception("DB error")

            result = _resolve_repo_path_for_commit("abc123")

        assert result is None


class TestCommitDiffResponse:
    """Test CommitDiffResponse model includes repo fields."""

    def test_response_includes_repo_fields(self):
        """REQ-23: CommitDiffResponse includes repo_id and repo_label."""
        from src.mcp.tickets_api import CommitDiffResponse, FileDiff

        response = CommitDiffResponse(
            success=True,
            commit_sha="abc123",
            commit_message="Test commit",
            author="Test Author",
            commit_timestamp="2024-01-01T00:00:00Z",
            files_changed=1,
            total_insertions=10,
            total_deletions=5,
            total_files=1,
            files=[],
            repo_id="repo-123",
            repo_label="backend",
        )

        assert response.repo_id == "repo-123"
        assert response.repo_label == "backend"

    def test_response_repo_fields_optional(self):
        """REQ-23: repo_id and repo_label are optional."""
        from src.mcp.tickets_api import CommitDiffResponse

        response = CommitDiffResponse(
            success=True,
            commit_sha="abc123",
            commit_message="Test commit",
            author="Test Author",
            commit_timestamp="2024-01-01T00:00:00Z",
            files_changed=0,
            total_insertions=0,
            total_deletions=0,
            total_files=0,
            files=[],
        )

        assert response.repo_id is None
        assert response.repo_label is None
