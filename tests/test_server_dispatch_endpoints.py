"""Tests for the per-cli/model concurrency gate wired into server.py's
manual dispatch endpoints (restart_task_endpoint, bump_task_priority_endpoint).

These endpoints had no test coverage at all before this gate was added --
process_queue and create_agent_for_task_direct's equivalent logic is
covered in test_queue_service.py / test_orchestrator_helpers.py, but the
wiring in these two endpoints was previously only verified by manual
review + a syntax check. Uses a real in-memory-SQLite-backed QueueService
(same pattern as test_queue_service.py) so resolve_cli_model_dispatch's
reservation logic runs for real; AgentDispatchService.build_dispatch_context/
dispatch are mocked out since their own internals (RAG, phase context,
worktree setup) aren't this test's concern.

create_task's endpoint is NOT covered here: unlike these two, it also runs
duplicate-detection embeddings and LLM-based task enrichment before ever
reaching the concurrency gate, which would need a much larger mocking
surface to reach in isolation. Its use of the exact same
resolve_cli_model_dispatch/reservation primitives is already covered by
this file and test_queue_service.py.
"""

import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Agent, Base, Phase, Task, Workflow
from src.services.queue_service import QueueService


@pytest.fixture
def db_manager():
    """Real in-memory SQLite DatabaseManager -- same pattern as
    test_queue_service.py's fixture of the same name."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    class TestDatabaseManager:
        def __init__(self):
            self.engine = engine
            self.Session = Session

        def get_session(self):
            return self.Session()

        @contextmanager
        def session_scope(self):
            session = self.Session()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    return TestDatabaseManager()


def _seed_task(db, task_id, status, cli_tool=None, cli_model=None, saturate=False):
    """Create a task, optionally tied to a phase with a cli_tool/cli_model
    override, and optionally with a busy agent already occupying that
    combo's single slot."""
    session = db.get_session()
    try:
        phase_id = None
        if cli_tool:
            phase_id = f"phase-{task_id}"
            session.add(Workflow(id=f"wf-{task_id}", name="t", phases_folder_path="/tmp"))
            session.add(
                Phase(
                    id=phase_id, workflow_id=f"wf-{task_id}", order=1, name="development",
                    description="d", done_definitions=[], cli_tool=cli_tool, cli_model=cli_model,
                )
            )
        session.add(
            Task(
                id=task_id, raw_description="r", done_definition="d",
                status=status, phase_id=phase_id,
            )
        )
        if saturate:
            session.add(
                Agent(id=f"busy-{task_id}", system_prompt="p", status="working", cli_type=cli_tool, cli_model=cli_model)
            )
        session.commit()
    finally:
        session.close()


def _make_queue_service(db, **kwargs):
    return QueueService(db, max_concurrent_agents=10, **kwargs)


def _make_server_state(db, queue_service):
    server_state = Mock()
    server_state.db_manager = db
    server_state.queue_service = queue_service
    server_state.broadcast_update = AsyncMock()
    return server_state


class TestRestartTaskEndpointCliModelConcurrency:
    @pytest.mark.asyncio
    async def test_dispatches_on_fallback_when_primary_combo_saturated(self, db_manager):
        from src.mcp.server import restart_task_endpoint

        _seed_task(db_manager, "task-1", status="failed", cli_tool="pi", cli_model="qwen-local", saturate=True)
        qs = _make_queue_service(
            db_manager,
            cli_model_concurrency_limits={"pi/qwen-local": 1},
            default_cli_tool="pi",
            default_cli_model="qwen-local",
            cli_model_fallback="mimo-v2.5-pro",
        )
        server_state = _make_server_state(db_manager, qs)
        dispatched_agent = Mock(id="new-agent")

        with patch("src.mcp.server.server_state", server_state), \
             patch(
                 "src.services.agent_dispatch_service.AgentDispatchService.build_dispatch_context",
                 new=AsyncMock(return_value={"phase_cli_tool": "pi", "phase_cli_model": "qwen-local"}),
             ), \
             patch(
                 "src.services.agent_dispatch_service.AgentDispatchService.dispatch",
                 new=AsyncMock(return_value=dispatched_agent),
             ) as mock_dispatch, \
             patch("src.services.agent_dispatch_service.AgentDispatchService.mark_assigned"):
            result = await restart_task_endpoint(task_id="task-1")

        assert result["status"] == "assigned"
        _, kwargs = mock_dispatch.call_args
        assert kwargs["dispatch_context"]["phase_cli_tool"] == "pi"
        assert kwargs["dispatch_context"]["phase_cli_model"] == "mimo-v2.5-pro"
        # Reservation released after dispatch -- a second call must succeed too.
        assert qs.get_active_agent_count_for_cli_model("pi", "mimo-v2.5-pro") == 0

    @pytest.mark.asyncio
    async def test_queues_instead_of_dispatching_when_saturated_with_no_fallback(self, db_manager):
        from src.mcp.server import restart_task_endpoint

        _seed_task(db_manager, "task-1", status="failed", cli_tool="pi", cli_model="qwen-local", saturate=True)
        qs = _make_queue_service(
            db_manager,
            cli_model_concurrency_limits={"pi/qwen-local": 1},
            default_cli_tool="pi",
            default_cli_model="qwen-local",
            # no cli_model_fallback configured
        )
        server_state = _make_server_state(db_manager, qs)

        with patch("src.mcp.server.server_state", server_state), \
             patch(
                 "src.services.agent_dispatch_service.AgentDispatchService.build_dispatch_context",
                 new=AsyncMock(return_value={"phase_cli_tool": "pi", "phase_cli_model": "qwen-local"}),
             ), \
             patch(
                 "src.services.agent_dispatch_service.AgentDispatchService.dispatch",
                 new=AsyncMock(),
             ) as mock_dispatch:
            result = await restart_task_endpoint(task_id="task-1")

        assert result["status"] == "queued"
        mock_dispatch.assert_not_called()
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "queued"
        finally:
            session.close()

    @pytest.mark.asyncio
    async def test_no_limits_configured_dispatches_normally(self, db_manager):
        """Regression: the concurrency gate must be a true no-op (no phase
        lookups, no behavior change) when cli_model_concurrency_limits is
        unset -- the original, pre-gate behavior for every existing caller."""
        from src.mcp.server import restart_task_endpoint

        _seed_task(db_manager, "task-1", status="failed")
        qs = _make_queue_service(db_manager)  # no concurrency limits
        server_state = _make_server_state(db_manager, qs)
        dispatched_agent = Mock(id="new-agent")

        with patch("src.mcp.server.server_state", server_state), \
             patch(
                 "src.services.agent_dispatch_service.AgentDispatchService.build_dispatch_context",
                 new=AsyncMock(return_value={"phase_cli_tool": None, "phase_cli_model": None}),
             ), \
             patch(
                 "src.services.agent_dispatch_service.AgentDispatchService.dispatch",
                 new=AsyncMock(return_value=dispatched_agent),
             ) as mock_dispatch, \
             patch("src.services.agent_dispatch_service.AgentDispatchService.mark_assigned"):
            result = await restart_task_endpoint(task_id="task-1")

        assert result["status"] == "assigned"
        mock_dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_restarting_a_blocked_task_clears_the_full_pause_triad(self, db_manager):
        """Regression found during Phase 2 §4.8's re-audit (not one of the
        four historically-fixed sites): a task can be "blocked" -- exactly
        what pause_feature sets on a paused workflow's in-flight tasks --
        so this endpoint's `if wf.status != "active": wf.status = "active"`
        was reachable on a genuinely paused workflow, not just a
        "completed" one. Left paused_by/paused_at stale, producing an
        inconsistent status="active"-but-still-flagged-paused row."""
        from src.core.database import Feature
        from src.mcp.server import restart_task_endpoint

        session = db_manager.get_session()
        try:
            session.add(
                Workflow(
                    id="wf-blocked", name="t", phases_folder_path="/tmp",
                    status="paused", paused_by="user",
                )
            )
            session.add(
                Feature(
                    id="feat-blocked", design_id="des-1", feature_key="k",
                    name="n", scope="s", workflow_id="wf-blocked",
                    status="paused",
                )
            )
            session.add(
                Task(
                    id="task-blocked", raw_description="r", done_definition="d",
                    status="blocked", workflow_id="wf-blocked",
                )
            )
            session.commit()
        finally:
            session.close()

        qs = _make_queue_service(db_manager)
        server_state = _make_server_state(db_manager, qs)
        dispatched_agent = Mock(id="new-agent")

        with patch("src.mcp.server.server_state", server_state), \
             patch(
                 "src.services.agent_dispatch_service.AgentDispatchService.build_dispatch_context",
                 new=AsyncMock(return_value={"phase_cli_tool": None, "phase_cli_model": None}),
             ), \
             patch(
                 "src.services.agent_dispatch_service.AgentDispatchService.dispatch",
                 new=AsyncMock(return_value=dispatched_agent),
             ), \
             patch("src.services.agent_dispatch_service.AgentDispatchService.mark_assigned"):
            await restart_task_endpoint(task_id="task-blocked")

        session = db_manager.get_session()
        try:
            wf = session.query(Workflow).filter_by(id="wf-blocked").first()
            assert wf.status == "active"
            assert wf.paused_by is None
            assert wf.paused_at is None
        finally:
            session.close()


class TestBumpTaskPriorityEndpointCliModelConcurrency:
    @pytest.mark.asyncio
    async def test_dispatches_on_fallback_when_primary_combo_saturated(self, db_manager):
        from src.mcp.server import bump_task_priority_endpoint

        _seed_task(db_manager, "task-1", status="queued", cli_tool="pi", cli_model="qwen-local", saturate=True)
        qs = _make_queue_service(
            db_manager,
            cli_model_concurrency_limits={"pi/qwen-local": 1},
            default_cli_tool="pi",
            default_cli_model="qwen-local",
            cli_model_fallback="mimo-v2.5-pro",
        )
        server_state = _make_server_state(db_manager, qs)
        dispatched_agent = Mock(id="new-agent")

        with patch("src.mcp.server.server_state", server_state), \
             patch(
                 "src.services.agent_dispatch_service.AgentDispatchService.build_dispatch_context",
                 new=AsyncMock(return_value={"phase_cli_tool": "pi", "phase_cli_model": "qwen-local"}),
             ), \
             patch(
                 "src.services.agent_dispatch_service.AgentDispatchService.dispatch",
                 new=AsyncMock(return_value=dispatched_agent),
             ) as mock_dispatch, \
             patch("src.services.agent_dispatch_service.AgentDispatchService.mark_assigned"):
            result = await bump_task_priority_endpoint(task_id="task-1")

        assert result["success"] is True
        _, kwargs = mock_dispatch.call_args
        assert kwargs["dispatch_context"]["phase_cli_tool"] == "pi"
        assert kwargs["dispatch_context"]["phase_cli_model"] == "mimo-v2.5-pro"
        assert qs.get_active_agent_count_for_cli_model("pi", "mimo-v2.5-pro") == 0

    @pytest.mark.asyncio
    async def test_dispatches_on_primary_anyway_when_saturated_with_no_fallback(self, db_manager):
        """This endpoint's whole contract is 'start immediately, bypassing
        limits' -- when no fallback is usable it must still dispatch (on
        the primary) rather than queue, unlike restart_task_endpoint."""
        from src.mcp.server import bump_task_priority_endpoint

        _seed_task(db_manager, "task-1", status="queued", cli_tool="pi", cli_model="qwen-local", saturate=True)
        qs = _make_queue_service(
            db_manager,
            cli_model_concurrency_limits={"pi/qwen-local": 1},
            default_cli_tool="pi",
            default_cli_model="qwen-local",
            # no cli_model_fallback configured
        )
        server_state = _make_server_state(db_manager, qs)
        dispatched_agent = Mock(id="new-agent")

        with patch("src.mcp.server.server_state", server_state), \
             patch(
                 "src.services.agent_dispatch_service.AgentDispatchService.build_dispatch_context",
                 new=AsyncMock(return_value={"phase_cli_tool": "pi", "phase_cli_model": "qwen-local"}),
             ), \
             patch(
                 "src.services.agent_dispatch_service.AgentDispatchService.dispatch",
                 new=AsyncMock(return_value=dispatched_agent),
             ) as mock_dispatch, \
             patch("src.services.agent_dispatch_service.AgentDispatchService.mark_assigned"):
            result = await bump_task_priority_endpoint(task_id="task-1")

        assert result["success"] is True
        mock_dispatch.assert_called_once()
        _, kwargs = mock_dispatch.call_args
        # No override -- dispatched on the (saturated) primary combo as-is.
        assert kwargs["dispatch_context"]["phase_cli_tool"] == "pi"
        assert kwargs["dispatch_context"]["phase_cli_model"] == "qwen-local"
