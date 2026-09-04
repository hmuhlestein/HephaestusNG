"""Shared pytest fixtures for Hephaestus tests."""

import builtins
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.database import Base, DatabaseManager
from sqlalchemy import event

# Set test database env var BEFORE any test module imports server.py,
# which triggers ServerState() -> DatabaseManager(config.database_path)
# at module level. Without this, importing server.py touches the
# production database.
os.environ["HEPHAESTUS_TEST_DB"] = ":memory:"

# SQLAlchemy's declarative mapper configuration runs lazily on first ORM
# use and is documented as not thread-safe if that first trigger happens
# concurrently from multiple threads. Production configures it eagerly at
# server startup (see lifecycle.py's startup_event) specifically for this
# reason, but tests that call route handlers directly (not through
# `with TestClient(app) as client:`) never fire that startup event -- and
# several tests now dispatch concurrent DB-bound work via asyncio.gather
# (get_pipeline_status). Confirmed live: running those tests as part of
# the full suite (multiple threads racing to trigger mapper configuration
# for the first time) intermittently raised "RuntimeError: deque mutated
# during iteration" from inside configure_mappers' own bookkeeping,
# silently swallowed by the caller's own try/except and surfacing as
# wrong data instead of a loud error. Configuring mappers eagerly here,
# at conftest import time (before any test session starts), closes the
# same gap for tests that production's startup_event already closes.
from sqlalchemy.orm import configure_mappers as _configure_mappers

_configure_mappers()


@pytest.fixture(autouse=True, scope="session")
def _skip_fk_enforcement_for_tests():
    """Disable PRAGMA foreign_keys=ON for test database engines.

    The production DatabaseManager sets foreign_keys=ON per-connection,
    but test fixtures were written without FK enforcement (it was added
    in a later commit) and many create Agent rows with current_task_id
    referencing Task rows that don't exist yet or are created later.
    The FK constraints still exist in the schema — we just skip the
    per-connection pragma enforcement in tests."""
    _original_init = DatabaseManager.__init__
    # DatabaseManager(path) constructs a FRESH wrapper on every call (e.g.
    # every get_db()), but its underlying engine is cached/reused across
    # calls for the same path -- this must register the "connect" listener
    # exactly ONCE per engine, not once per wrapper construction. Without
    # this guard it re-registered a duplicate listener on the SAME engine
    # every single call, growing that engine's listener deque unbounded
    # and racing with itself: code that dispatches concurrent DB-bound
    # work (e.g. get_pipeline_status's asyncio.gather) can have multiple
    # threads simultaneously constructing DatabaseManager for the same
    # cached engine while OTHER threads are mid-iteration firing that
    # engine's "connect" event for a new physical connection -- confirmed
    # live: "RuntimeError: deque mutated during iteration" from inside
    # SQLAlchemy's own event.attr._exec_w_sync_on_first_run, surfacing as
    # silently wrong data (e.g. a queue_depth count reading 0) wherever
    # the caller's own try/except swallowed it. Guarded by
    # DatabaseManager._lock -- the same lock __init__ itself already uses
    # for the analogous "only create this engine once" check -- so the
    # check-and-register is atomic against concurrent callers.
    _patched_engine_ids: set = set()

    def _test_init(self, *args, **kwargs):
        _original_init(self, *args, **kwargs)
        if hasattr(self, 'engine'):
            with DatabaseManager._lock:
                if id(self.engine) in _patched_engine_ids:
                    return
                _patched_engine_ids.add(id(self.engine))

            @event.listens_for(self.engine, "connect")
            def _skip_fk(dbapi_conn, connection_record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=OFF")
                cursor.close()

    DatabaseManager.__init__ = _test_init
    yield
    DatabaseManager.__init__ = _original_init


# The one database a test must never open. Anything resolving to this path
# is the real, self-hosted production DB sitting in the repo root.
PRODUCTION_DB = (Path(__file__).resolve().parent.parent / "hephaestus.db").resolve()


def _resolves_to_production_db(database_path) -> bool:
    """True if this DatabaseManager argument would open the production DB."""
    if database_path is None:
        # Mirrors DatabaseManager.__init__/get_db's own fallback.
        database_path = os.environ.get("HEPHAESTUS_TEST_DB", "hephaestus.db")
    database_path = str(database_path)
    if database_path == ":memory:" or database_path.startswith("file::memory:"):
        return False
    try:
        return Path(database_path).resolve() == PRODUCTION_DB
    except (OSError, ValueError):
        return False


@pytest.fixture(autouse=True, scope="session")
def _forbid_production_database():
    """Fail loudly if any test opens the real hephaestus.db.

    HEPHAESTUS_TEST_DB (set above) only covers the paths that go through
    the None/env fallback. It does nothing for code that resolves a path
    of its own and passes it explicitly -- which is the normal, correct
    shape for production code: `DbManager(str(cfg.database_path))`. If a
    test fails to redirect the *config* that path comes from, production
    code opens the production database and nothing anywhere complains.

    Found live: test_heal_orphaned_agent_branches.py patched get_config
    only at its definition site (src.core.simple_config.get_config), but
    worktree_integration.py binds the name at import time. Run that file
    alone and the module is imported after the patch, so it picks up the
    test config and passes. Run anything importing src.mcp.server first
    and the binding is the real get_config, returning the memoized
    production Config -- so heal_orphaned_agent_branches enumerated the
    developer's REAL projects and attempted `git merge --ff-only` in
    them. The only symptom was one assertion failing in suite order.

    src/'s own AST guard (test_db_test_isolation_guard.py) cannot catch
    this: it checks how production code *constructs* DatabaseManager,
    and that code is already correct. The defect is in what the test
    lets the config resolve to, which is only observable at runtime.
    """
    import src.core.database as dbmod

    _wrapped_init = dbmod.DatabaseManager.__init__

    def _guarded_init(self, database_path="hephaestus.db", *args, **kwargs):
        if _resolves_to_production_db(database_path):
            raise RuntimeError(
                "A test tried to open the PRODUCTION database "
                f"({PRODUCTION_DB}).\n"
                "Production code resolves its own path from get_config(), so "
                "patching src.core.simple_config.get_config is not always "
                "enough -- modules that do `from src.core.simple_config import "
                "get_config` bind the name at import time. Patch it on the "
                "module under test as well:\n"
                "    monkeypatch.setattr(the_module, 'get_config', lambda: cfg)\n"
                "See _forbid_production_database in tests/conftest.py."
            )
        return _wrapped_init(self, database_path, *args, **kwargs)

    dbmod.DatabaseManager.__init__ = _guarded_init
    yield
    dbmod.DatabaseManager.__init__ = _wrapped_init


@pytest.fixture(autouse=True)
def _guard_mock_paths(monkeypatch):
    """Raise if any code opens a path whose str looks like a Mock repr.

    Bare Mock() objects silently auto-create attributes that pass through to
    open() as filenames, e.g. '<Mock name="get_config().database_path" id="...">'.
    """
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        if isinstance(file, str) and file.startswith("<Mock "):
            raise TypeError(
                f"open() called with a Mock repr as path: {file!r}\n"
                "Set the required attribute on your mock config "
                "(e.g. config.paths.database_path = ':memory:')."
            )
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)


@pytest.fixture(autouse=True)
def _restore_global_config_paths():
    """Restore the process-global config singleton's path fields after each test.

    get_config() returns a module-level singleton, not something pytest
    resets between tests. Anything that mutates config.git.main_repo_path /
    config.paths.project_root / config.paths.worktree_base_path directly
    (e.g. _apply_active_project in project_routes.py) leaves those pointed
    at a since-deleted tmp_path for the rest of the process, breaking later
    tests that fall back to reading this singleton (WorktreeManager,
    validate_file_path's _default_allowed_roots) when run in full-suite
    order. See docs/PENDING_TEST_FAILURES.md Group A."""
    from src.core.simple_config import get_config

    config = get_config()
    main_repo_path = config.git.main_repo_path
    project_root = config.paths.project_root
    worktree_base_path = config.paths.worktree_base_path
    yield
    config.git.main_repo_path = main_repo_path
    config.paths.project_root = project_root
    config.paths.worktree_base_path = worktree_base_path


@pytest.fixture
def mock_heph_config():
    """Return a Mock config with all common fields pre-populated.

    Use this instead of bare Mock() when patching get_config() so that
    accessing any config attribute never silently returns another Mock.
    """
    config = MagicMock()
    config.paths.database_path = Path(":memory:")
    config.server.mcp_host = "127.0.0.1"
    config.server.mcp_port = 8300
    config.server.enable_cors = True
    config.paths.worktree_base_path = None
    config.paths.project_root = Path("/tmp/test-project")
    config.git.main_repo_path = Path("/tmp/test-project")
    config.git.base_branch = "main"
    config.git.branch_prefix = "agent-"
    config.git.auto_commit = False
    config.conflict_resolution_strategy = "newest_file_wins"
    config.llm.llm_provider = "openrouter"
    config.llm.llm_model = "openai/gpt-4o"
    config.task_dedup.task_similarity_threshold = 0.7
    config.task_dedup.task_related_threshold = 0.4
    return config


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
def db_manager(tmp_path, monkeypatch):
    """Create a fresh file-based database manager for each test.

    Uses tmp_path instead of :memory: because QueuePool (used by
    DatabaseManager) creates separate in-memory databases per
    connection, causing 'no such table' errors when the table
    was created on a different pooled connection.

    Also sets HEPHAESTUS_TEST_DB via monkeypatch so code that calls
    get_db() directly (e.g. orchestrator, monitor) uses this test DB
    instead of the production database."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    manager = DatabaseManager(str(db_path))
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
    mock.enrich_task = AsyncMock(
        return_value={
            "enriched_description": "Enriched test task description",
            "complexity": "medium",
            "suggested_approach": "Test approach",
        }
    )
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
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = session_local()
    try:
        yield session
    finally:
        session.close()


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
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "designs_processed": 0,
                "designs_succeeded": 0,
                "designs_failed": 0,
            }
        )
    )

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

    from src.mcp.autopilot import _shared as api_mod
    from src.mcp.autopilot import intervention_routes, queue_routes, router as autopilot_router

    app = FastAPI()

    # Patch module-level variables
    api_mod.DESIGN_QUEUE_DIR = str(autopilot_dirs["queue"])
    api_mod.FEATURES_DIR = str(autopilot_dirs["features"])
    api_mod.STATE_DIR = str(autopilot_dirs["state"])
    # AUTOPILOT_STATE_DIR is imported into each route module that reads it,
    # so the rebind must fan out to every reader's module namespace
    for _m in (api_mod, queue_routes, intervention_routes):
        _m.AUTOPILOT_STATE_DIR = str(autopilot_dirs["state"])

    # Include the router
    app.include_router(autopilot_router)

    return app


@pytest.fixture
def client(autopilot_dirs):
    """Create a TestClient with mocked dependencies."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.mcp.autopilot import _shared as api_mod
    from src.mcp.autopilot import intervention_routes, queue_routes, router as autopilot_router

    app = FastAPI()

    # Patch module-level variables
    api_mod.DESIGN_QUEUE_DIR = str(autopilot_dirs["queue"])
    api_mod.FEATURES_DIR = str(autopilot_dirs["features"])
    api_mod.STATE_DIR = str(autopilot_dirs["state"])
    # AUTOPILOT_STATE_DIR is imported into each route module that reads it,
    # so the rebind must fan out to every reader's module namespace
    for _m in (api_mod, queue_routes, intervention_routes):
        _m.AUTOPILOT_STATE_DIR = str(autopilot_dirs["state"])
    api_mod._cache.clear()

    # Include the router
    app.include_router(autopilot_router)

    with patch("src.core.database.get_db") as mock_get_db:
        mock_session = Mock()
        # Set up query chain to return proper types
        mock_query = Mock()
        mock_query.filter.return_value.first.return_value = None
        mock_query.filter.return_value.count.return_value = 0
        mock_query.filter.return_value.all.return_value = []
        mock_query.filter_by.return_value.first.return_value = None
        mock_query.filter_by.return_value.all.return_value = []
        mock_session.query.return_value = mock_query
        mock_get_db.return_value.__enter__ = Mock(return_value=mock_session)
        mock_get_db.return_value.__exit__ = Mock(return_value=False)
        with patch("src.autopilot.service.get_autopilot_service") as mock_svc:
            mock_svc.return_value = Mock(
                running=False,
                status=Mock(
                    return_value={
                        "running": False,
                        "designs_processed": 0,
                        "designs_succeeded": 0,
                        "designs_failed": 0,
                        "current_design": None,
                        "elapsed_seconds": 0,
                        "error": None,
                    }
                ),
            )
            with patch(
                "src.mcp.autopilot._shared._get_active_project_id", return_value=None
            ), patch(
                "src.mcp.autopilot.control_routes._get_active_project_id", return_value=None
            ):
                with patch("src.autopilot.service.get_registry") as mock_reg:
                    mock_reg.return_value.running.return_value = []
                    yield TestClient(app)


def pytest_configure(config):
    """Read coverage_floor from hephaestus_config.yaml.

    The primary enforcement path is .coveragerc's fail_under = 20 (read
    by pytest-cov directly).  This hook exposes the YAML-configured value
    on config so other plugins/scripts can access it programmatically.
    Override on the CLI with: pytest --cov-fail-under=N
    """
    try:
        from src.core.simple_config import get_config
        cfg = get_config()
        config._heph_coverage_floor = cfg.testing.coverage_floor
        config._heph_new_code_coverage_floor = cfg.testing.new_code_coverage_floor
    except Exception:
        config._heph_coverage_floor = 20
        config._heph_new_code_coverage_floor = 80
