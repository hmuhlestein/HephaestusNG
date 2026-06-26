"""Shared pytest fixtures for Hephaestus tests."""

import pytest
import tempfile
import os
import uuid
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock

# Set test database environment variable before any imports
os.environ["HEPHAESTUS_TEST_DB"] = ":memory:"


@pytest.fixture(scope="session")
def temp_db():
    """Create temporary database file for tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield f.name
    # Cleanup after session
    if os.path.exists(f.name):
        os.unlink(f.name)


@pytest.fixture
def clean_db(temp_db):
    """Ensure clean database for each test."""
    if os.path.exists(temp_db):
        os.unlink(temp_db)
    yield temp_db


@pytest.fixture
def db_manager():
    """Create a fresh in-memory database manager for each test."""
    from src.core.database import DatabaseManager

    manager = DatabaseManager(":memory:")
    manager.create_tables()
    yield manager


@pytest.fixture
def phase_manager(db_manager):
    """Create a phase manager with test database."""
    from src.phases.phase_manager import PhaseManager

    manager = PhaseManager(db_manager)
    yield manager


@pytest.fixture
def mock_llm_provider():
    """Create a mock LLM provider for tests."""
    mock = AsyncMock()
    mock.enrich_task = AsyncMock(return_value={
        "enriched_description": "Enriched test task description",
        "complexity": "medium",
        "suggested_approach": "Test approach"
    })
    mock.generate_agent_prompt = AsyncMock(return_value="System prompt for test agent")
    return mock


@pytest.fixture
def test_workflow_definition():
    """Create a test workflow definition."""
    from src.sdk.models import Phase, WorkflowConfig, WorkflowDefinition

    phases = [
        Phase(
            id=1,
            name="Planning",
            description="Plan the project",
            done_definitions=["Requirements documented"],
            working_directory="/project",
        ),
        Phase(
            id=2,
            name="Implementation",
            description="Implement the solution",
            done_definitions=["Code written", "Tests pass"],
            working_directory="/project",
        ),
        Phase(
            id=3,
            name="Testing",
            description="Test the solution",
            done_definitions=["All tests pass"],
            working_directory="/project",
        ),
    ]

    config = WorkflowConfig(
        has_result=True,
        result_criteria="Working application",
        on_result_found="stop_all",
    )

    return WorkflowDefinition(
        id="test-workflow",
        name="Test Workflow",
        phases=phases,
        config=config,
        description="Test workflow for integration tests",
    )


@pytest.fixture
def test_bugfix_definition():
    """Create a bugfix workflow definition for testing multiple definitions."""
    from src.sdk.models import Phase, WorkflowConfig, WorkflowDefinition

    phases = [
        Phase(
            id=1,
            name="Analysis",
            description="Analyze the bug",
            done_definitions=["Bug understood", "Root cause identified"],
            working_directory="/project",
        ),
        Phase(
            id=2,
            name="Fix",
            description="Implement the fix",
            done_definitions=["Fix implemented", "Tests updated"],
            working_directory="/project",
        ),
    ]

    config = WorkflowConfig(
        has_result=True,
        result_criteria="Bug fixed and tests pass",
        on_result_found="stop_all",
    )

    return WorkflowDefinition(
        id="bugfix-workflow",
        name="Bug Fix Workflow",
        phases=phases,
        config=config,
        description="Workflow for fixing bugs",
    )


@pytest.fixture
def sample_task_data():
    """Create sample task data for tests."""
    return {
        "task_description": "Write unit tests for authentication module",
        "done_definition": "All auth functions have >90% test coverage with passing tests",
        "ai_agent_id": f"test-agent-{uuid.uuid4()}",
        "priority": "medium",
        "phase_id": "1",
    }


@pytest.fixture
def sample_ticket_data():
    """Create sample ticket data for tests."""
    return {
        "title": "Fix login bug #123",
        "description": "Users cannot log in with valid credentials. Need to investigate auth flow.",
        "ticket_type": "bug",
        "priority": "high",
        "tags": ["auth", "urgent"],
    }


@pytest.fixture
def mock_agent_manager():
    """Create a mock agent manager."""
    mock = MagicMock()
    mock.create_agent_for_task = AsyncMock()
    mock.get_project_context = AsyncMock(return_value="Test project context")
    return mock


@pytest.fixture
def mock_rag_system():
    """Create a mock RAG system."""
    mock = MagicMock()
    mock.retrieve_for_task = AsyncMock(return_value=[
        {"content": "Memory 1", "type": "learning"},
        {"content": "Memory 2", "type": "discovery"},
    ])
    return mock


@pytest.fixture
def test_workflow_id():
    """Generate a unique test workflow ID."""
    return f"test-workflow-{uuid.uuid4()}"


@pytest.fixture
def test_agent_id():
    """Generate a unique test agent ID."""
    return str(uuid.uuid4())


@pytest.fixture
def initialized_phase_manager(db_manager, test_workflow_definition):
    """Create a phase manager with registered workflow definition."""
    from src.phases.phase_manager import PhaseManager

    manager = PhaseManager(db_manager)

    # Register the test definition
    phases_config = [
        {
            "order": phase.id,
            "name": phase.name,
            "description": phase.description,
            "done_definitions": phase.done_definitions,
            "working_directory": phase.working_directory,
        }
        for phase in test_workflow_definition.phases
    ]

    workflow_config = {}
    if test_workflow_definition.config:
        workflow_config = {
            "has_result": test_workflow_definition.config.has_result,
            "result_criteria": test_workflow_definition.config.result_criteria,
            "on_result_found": test_workflow_definition.config.on_result_found,
        }

    manager.register_definition(
        definition_id=test_workflow_definition.id,
        name=test_workflow_definition.name,
        description=test_workflow_definition.description,
        phases_config=phases_config,
        workflow_config=workflow_config,
    )

    yield manager


@pytest.fixture
def workflow_with_execution(initialized_phase_manager):
    """Create a phase manager with a started workflow execution."""
    workflow_id, _ = initialized_phase_manager.start_execution(
        definition_id="test-workflow",
        description="Test execution for integration tests",
        working_directory="/tmp/test-project",
    )
    return initialized_phase_manager, workflow_id


# Async fixtures
@pytest.fixture
def event_loop():
    """Create event loop for async tests."""
    import asyncio
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
"""Shared mock infrastructure for testing API endpoints without a server.

Provides fixture factories for:
- Database sessions (in-memory SQLite)
- Autopilot service (mocked pipeline)
- File system (temp directories for queue/state/features)
- FastAPI TestClient with all dependencies overridden
"""

import pytest
import json
import os
import tempfile
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from src.core.database import Base, get_db, DatabaseManager


# ── In-memory database ────────────────────────────────────────────


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Create an in-memory SQLite session with all tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def db_manager(db_session) -> DatabaseManager:
    """Create a DatabaseManager backed by in-memory SQLite."""
    manager = Mock(spec=DatabaseManager)
    manager.get_session.return_value = db_session
    return manager


# ── Autopilot service mock ────────────────────────────────────────


@pytest.fixture
def mock_autopilot_service():
    """Create a mock AutopilotService with configurable state."""
    service = Mock()
    service.running = False
    service._project_path = None
    service._current_design = None
    service._designs_processed = 0
    service._designs_succeeded = 0
    service._designs_failed = 0
    service._error = None
    service._start_time = None

    def status():
        return {
            "running": service.running,
            "project_path": service._project_path,
            "current_design": service._current_design,
            "designs_processed": service._designs_processed,
            "designs_succeeded": service._designs_succeeded,
            "designs_failed": service._designs_failed,
            "elapsed_seconds": 0,
            "error": service._error,
        }

    service.status = status
    service.start = AsyncMock()
    service.stop = AsyncMock()
    return service


# ── Filesystem fixtures ───────────────────────────────────────────


@pytest.fixture
def autopilot_dirs(tmp_path):
    """Create temp directories mimicking the autopilot project structure."""
    state_dir = tmp_path / "state"
    queue_dir = tmp_path / "queue"
    features_dir = tmp_path / "features"
    designs_dir = tmp_path / "designs"
    state_dir.mkdir()
    queue_dir.mkdir()
    features_dir.mkdir()
    designs_dir.mkdir()

    # Write a default state.json
    (state_dir / "state.json").write_text(json.dumps({
        "designs_processed": 0,
        "designs_succeeded": 0,
        "designs_failed": 0,
    }))

    return {
        "root": tmp_path,
        "state": state_dir,
        "queue": queue_dir,
        "features": features_dir,
        "designs": designs_dir,
    }


@pytest.fixture
def sample_design_file(autopilot_dirs):
    """Create a sample design file in the queue."""
    design = autopilot_dirs["queue"] / "add_calculator.md"
    design.write_text(
        "# Add Calculator Feature\n\n"
        "## Requirements\n"
        "- Create a calculator that adds two numbers\n"
        "- Return the sum\n\n"
        "## Acceptance Criteria\n"
        "- Given two numbers, when added, returns the correct sum\n"
    )
    return design


# ── FastAPI app with overrides ─────────────────────────────────────


@pytest.fixture
def mock_app(autopilot_dirs, mock_autopilot_service):
    """Create a FastAPI app with all dependencies mocked."""
    from fastapi import FastAPI
    from src.mcp import autopilot_api as api_mod

    app = FastAPI()

    # Patch module-level variables
    api_mod.DESIGN_QUEUE_DIR = str(autopilot_dirs["queue"])
    api_mod.FEATURES_DIR = str(autopilot_dirs["features"])
    api_mod.STATE_DIR = str(autopilot_dirs["state"])

    # Include the router
    app.include_router(api_mod.router)

    return app


@pytest.fixture
def client(autopilot_dirs):
    """Create a TestClient with mocked dependencies."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.mcp import autopilot_api as api_mod

    app = FastAPI()

    # Patch module-level variables
    api_mod.DESIGN_QUEUE_DIR = str(autopilot_dirs["queue"])
    api_mod.FEATURES_DIR = str(autopilot_dirs["features"])
    api_mod.STATE_DIR = str(autopilot_dirs["state"])

    # Include the router
    app.include_router(api_mod.router)

    with patch("src.core.database.get_db") as mock_get_db:
        mock_session = Mock()
        # Set up query chain to return proper types
        mock_query = Mock()
        mock_query.filter.return_value.first.return_value = None
        mock_query.filter.return_value.count.return_value = 0
        mock_query.filter.return_value.all.return_value = []
        mock_session.query.return_value = mock_query
        mock_get_db.return_value.__enter__ = Mock(return_value=mock_session)
        mock_get_db.return_value.__exit__ = Mock(return_value=False)
        with patch("src.autopilot.service.get_autopilot_service") as mock_svc:
            mock_svc.return_value = Mock(
                running=False,
                status=Mock(return_value={
                    "running": False,
                    "designs_processed": 0,
                    "designs_succeeded": 0,
                    "designs_failed": 0,
                    "current_design": None,
                    "elapsed_seconds": 0,
                    "error": None,
                }),
            )
            with patch("src.mcp.autopilot_api._get_active_project_id", return_value=None):
                yield TestClient(app)
