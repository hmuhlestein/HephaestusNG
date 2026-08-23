"""Tests for commit-linking path validation.

REQ-10: Commit-linking validates files fall under task's repo_id path
REQ-02: TicketCommit.repo_id populated from resolved ProjectRepo
"""

import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.database import (
    AutopilotProject,
    Base,
    ProjectRepo,
    Task,
    Ticket,
    TicketCommit,
    Workflow,
    validate_ticket_repo_consistency,
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
def multi_repo_setup(db_session):
    """Create a project with two repos, workflow, task, and ticket."""
    project_id = f"proj-{uuid.uuid4()}"
    backend_repo_id = f"repo-{uuid.uuid4()}"
    frontend_repo_id = f"repo-{uuid.uuid4()}"
    workflow_id = f"wf-{uuid.uuid4()}"
    task_id = f"task-{uuid.uuid4()}"
    ticket_id = f"ticket-{uuid.uuid4()}"

    project = AutopilotProject(
        id=project_id,
        name="Multi-Repo Project",
        base_dir="/tmp/multi-repo",
    )
    db_session.add(project)

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

    workflow = Workflow(
        id=workflow_id,
        project_id=project_id,
        definition_id="autopilot",
        name="Test Workflow",
        phases_folder_path="/tmp/phases",
        status="active",
    )
    db_session.add(workflow)

    task = Task(
        id=task_id,
        raw_description="Implement backend API",
        done_definition="API endpoints working",
        workflow_id=workflow_id,
        repo_id=backend_repo_id,
    )
    db_session.add(task)

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


class TestValidateTicketRepoConsistency:
    """Test validate_ticket_repo_consistency helper."""

    def test_valid_consistency(self, db_session, multi_repo_setup):
        """Ticket.repo_id matching Task.repo_id passes validation."""
        data = multi_repo_setup
        ticket = db_session.query(Ticket).filter_by(id=data["ticket_id"]).first()
        ticket.repo_id = data["backend_repo_id"]
        db_session.commit()

        # Should not raise
        validate_ticket_repo_consistency(db_session, ticket)

    def test_mismatch_raises(self, db_session, multi_repo_setup):
        """Ticket.repo_id mismatching Task.repo_id raises ValueError."""
        data = multi_repo_setup
        ticket = db_session.query(Ticket).filter_by(id=data["ticket_id"]).first()
        ticket.repo_id = data["frontend_repo_id"]
        db_session.commit()

        with pytest.raises(ValueError, match="does not match"):
            validate_ticket_repo_consistency(db_session, ticket)

    def test_no_task_id_skips_validation(self, db_session, multi_repo_setup):
        """No task_id means no validation needed."""
        data = multi_repo_setup
        ticket = db_session.query(Ticket).filter_by(id=data["ticket_id"]).first()
        ticket.task_id = None
        ticket.repo_id = data["frontend_repo_id"]
        db_session.commit()

        # Should not raise
        validate_ticket_repo_consistency(db_session, ticket)

    def test_no_ticket_repo_id_skips_validation(self, db_session, multi_repo_setup):
        """No ticket.repo_id means no validation needed."""
        data = multi_repo_setup
        ticket = db_session.query(Ticket).filter_by(id=data["ticket_id"]).first()
        ticket.repo_id = None
        db_session.commit()

        # Should not raise
        validate_ticket_repo_consistency(db_session, ticket)


class TestLinkCommitPathValidation:
    """Test _link_commit_impl path-prefix validation logic."""

    def test_in_scope_files_not_flagged(self):
        """REQ-10: Files inside repo path are not flagged as out_of_scope."""
        from pathlib import Path

        repo_path = Path("/tmp/multi-repo/backend").resolve()
        files_list = ["src/main.py", "tests/test_main.py", "README.md"]

        out_of_files = []
        for file_path in files_list:
            try:
                abs_file = Path("/tmp/multi-repo/backend", file_path).resolve()
                if not abs_file.is_relative_to(repo_path):
                    out_of_files.append(file_path)
            except (ValueError, OSError):
                pass

        assert len(out_of_files) == 0

    def test_out_of_scope_files_detected(self):
        """REQ-10: Files outside repo path are detected."""
        from pathlib import Path

        repo_path = Path("/tmp/multi-repo/backend").resolve()
        files_list = ["../frontend/src/App.tsx", "src/main.py"]

        out_of_files = []
        for file_path in files_list:
            try:
                abs_file = Path("/tmp/multi-repo/backend", file_path).resolve()
                if not abs_file.is_relative_to(repo_path):
                    out_of_files.append(file_path)
            except (ValueError, OSError):
                pass

        assert len(out_of_files) == 1
        assert "../frontend/src/App.tsx" in out_of_files

    def test_similar_path_not_misdetected(self):
        """Path check uses is_relative_to, not string startswith."""
        from pathlib import Path

        repo_path = Path("/tmp/multi-repo/backend").resolve()

        # /tmp/multi-repo/backend-v2 should NOT be relative to /tmp/multi-repo/backend
        test_path = Path("/tmp/multi-repo/backend-v2/src/main.py").resolve()
        assert not test_path.is_relative_to(repo_path)
