"""Tests for get_project_context with multi-repo support.

REQ-17: get_project_context includes repo list for multi-repo projects
REQ-18: Writable vs read-only distinction for implementation agents
REQ-21: No additional text for single-repo projects
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.database import (
    Agent,
    AutopilotProject,
    Base,
    Phase,
    ProjectRepo,
    Task,
    Workflow,
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
def single_repo_project(db_session):
    """Create a project with one repo."""
    project_id = f"proj-{uuid.uuid4()}"
    repo_id = f"repo-{uuid.uuid4()}"
    workflow_id = f"wf-{uuid.uuid4()}"
    task_id = f"task-{uuid.uuid4()}"

    project = AutopilotProject(
        id=project_id,
        name="Single Repo Project",
        base_dir="/tmp/single-repo",
    )
    db_session.add(project)

    repo = ProjectRepo(
        id=repo_id,
        project_id=project_id,
        label="primary",
        path="/tmp/single-repo",
        is_primary=True,
    )
    db_session.add(repo)

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
        raw_description="Test task",
        done_definition="Done when complete",
        workflow_id=workflow_id,
        repo_id=repo_id,
    )
    db_session.add(task)
    db_session.commit()

    return {"project_id": project_id, "repo_id": repo_id, "task_id": task_id}


@pytest.fixture
def multi_repo_project(db_session):
    """Create a project with two repos."""
    project_id = f"proj-{uuid.uuid4()}"
    backend_repo_id = f"repo-{uuid.uuid4()}"
    frontend_repo_id = f"repo-{uuid.uuid4()}"
    workflow_id = f"wf-{uuid.uuid4()}"
    task_id = f"task-{uuid.uuid4()}"
    phase_id = f"phase-{uuid.uuid4()}"

    project = AutopilotProject(
        id=project_id,
        name="Multi Repo Project",
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

    phase = Phase(
        id=phase_id,
        name="development",
        workflow_id=workflow_id,
        order=1,
        description="Development phase",
        done_definitions=["All tests pass"],
    )
    db_session.add(phase)

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
        phase_id=phase_id,
        repo_id=backend_repo_id,
    )
    db_session.add(task)
    db_session.commit()

    return {
        "project_id": project_id,
        "backend_repo_id": backend_repo_id,
        "frontend_repo_id": frontend_repo_id,
        "task_id": task_id,
        "phase_id": phase_id,
    }


class TestGetProjectContext:
    """Test get_project_context with repo awareness."""

    @pytest.mark.asyncio
    async def test_single_repo_no_extra_text(self, db_engine, db_session, single_repo_project):
        """REQ-21: Single-repo project emits no additional repo text."""
        from src.agents.manager import AgentManager

        manager = AgentManager.__new__(AgentManager)
        manager.db_manager = MagicMock()
        manager.db_manager.get_session.return_value = db_session

        data = single_repo_project
        task = db_session.query(Task).filter_by(id=data["task_id"]).first()

        result = await manager.get_project_context(task=task)

        assert "PROJECT REPOSITORIES" not in result
        assert "WRITABLE" not in result
        assert "READ-ONLY" not in result

    @pytest.mark.asyncio
    async def test_multi_repo_shows_writable_and_readonly(self, db_engine, db_session, multi_repo_project):
        """REQ-18: Multi-repo project shows writable vs read-only distinction."""
        from src.agents.manager import AgentManager

        manager = AgentManager.__new__(AgentManager)
        manager.db_manager = MagicMock()
        manager.db_manager.get_session.return_value = db_session

        data = multi_repo_project
        task = db_session.query(Task).filter_by(id=data["task_id"]).first()

        result = await manager.get_project_context(task=task)

        assert "PROJECT REPOSITORIES" in result
        assert "backend (WRITABLE" in result
        assert "frontend (READ-ONLY reference)" in result

    @pytest.mark.asyncio
    async def test_multi_repo_architect_shows_assign_instruction(self, db_engine, db_session, multi_repo_project):
        """REQ-17: Feature architect sees 'assign each Feature' instruction."""
        from src.agents.manager import AgentManager

        manager = AgentManager.__new__(AgentManager)
        manager.db_manager = MagicMock()
        manager.db_manager.get_session.return_value = db_session

        data = multi_repo_project
        task = db_session.query(Task).filter_by(id=data["task_id"]).first()
        # Simulate architect phase: no repo_id, phase is feature_architect
        task.repo_id = None
        phase = db_session.query(Phase).filter_by(id=data["phase_id"]).first()
        phase.name = "feature_architect"
        db_session.commit()

        result = await manager.get_project_context(task=task)

        assert "PROJECT REPOSITORIES" in result
        assert "assign each Feature to exactly one" in result
        assert "WRITABLE" not in result
        assert "READ-ONLY" not in result

    @pytest.mark.asyncio
    async def test_no_task_no_repo_section(self, db_engine, db_session, multi_repo_project):
        """REQ-21: No task means no repo section."""
        from src.agents.manager import AgentManager

        manager = AgentManager.__new__(AgentManager)
        manager.db_manager = MagicMock()
        manager.db_manager.get_session.return_value = db_session

        result = await manager.get_project_context(task=None)

        assert "PROJECT REPOSITORIES" not in result
