"""Tests for project-scoped WebSocket/SSE broadcasts.

Part of the multi-project concurrency fix: broadcast_update sent every
event to every connected client with no project_id at all, so a user
viewing project A's dashboard saw project B's task/agent activity (and
got toast notifications for it) with no indication it belonged elsewhere.

get_project_info_for_workflow/resolve_project_for_workflow (src/core/
database.py) resolve a workflow's project so broadcast call sites can tag
their payload; broadcast_update (src/mcp/server.py) merges project_id/
project_name into the message when given.
"""

import asyncio

import pytest

from src.core.database import (
    AutopilotProject,
    DatabaseManager,
    Workflow,
    get_project_info_for_workflow,
    resolve_project_for_workflow,
)


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _make_workflow_with_project(db_manager, workflow_id, project_id=None, project_name=None):
    with db_manager.session_scope() as session:
        if project_id:
            session.add(
                AutopilotProject(id=project_id, name=project_name, base_dir=f"/tmp/{project_id}")
            )
        session.add(
            Workflow(
                id=workflow_id,
                name=workflow_id,
                status="active",
                project_id=project_id,
                phases_folder_path="/tmp",
            )
        )


class TestGetProjectInfoForWorkflow:
    def test_resolves_project_id_and_name(self, db_manager):
        _make_workflow_with_project(db_manager, "wf-1", "proj-a", "Project A")
        with db_manager.session_scope() as session:
            project_id, project_name = get_project_info_for_workflow(session, "wf-1")
        assert project_id == "proj-a"
        assert project_name == "Project A"

    def test_workflow_with_no_project_id_returns_none(self, db_manager):
        with db_manager.session_scope() as session:
            session.add(
                Workflow(id="wf-2", name="wf-2", status="active", phases_folder_path="/tmp")
            )
        with db_manager.session_scope() as session:
            assert get_project_info_for_workflow(session, "wf-2") == (None, None)

    def test_missing_workflow_returns_none(self, db_manager):
        with db_manager.session_scope() as session:
            assert get_project_info_for_workflow(session, "does-not-exist") == (None, None)

    def test_no_workflow_id_returns_none(self, db_manager):
        with db_manager.session_scope() as session:
            assert get_project_info_for_workflow(session, None) == (None, None)


class TestResolveProjectForWorkflow:
    def test_resolves_via_own_session(self, db_manager):
        _make_workflow_with_project(db_manager, "wf-1", "proj-a", "Project A")
        assert resolve_project_for_workflow("wf-1") == ("proj-a", "Project A")

    def test_no_workflow_id_returns_none_without_touching_db(self, db_manager):
        assert resolve_project_for_workflow(None) == (None, None)


class TestBroadcastUpdateProjectTagging:
    """Targets ConnectionBroadcaster directly (SOLID review 1.6 extracted
    broadcast_update's actual logic there; ServerState.broadcast_update is
    now a thin delegator, covered separately below). Previously this class
    called the unbound `ServerState.broadcast_update(fake, ...)` against a
    duck-typed FakeServerState specifically to avoid ServerState's heavy
    __init__ -- ConnectionBroadcaster's own __init__ is cheap enough that no
    stand-in is needed at all now."""

    @pytest.mark.asyncio
    async def test_merges_project_id_and_name_into_message(self):
        from src.mcp.server.connection_broadcaster import ConnectionBroadcaster

        broadcaster = ConnectionBroadcaster()
        sent = asyncio.Queue()
        broadcaster.sse_queues = [sent]

        await broadcaster.broadcast_update(
            {"type": "task_created", "task_id": "t1"},
            project_id="proj-a", project_name="Project A",
        )

        message = sent.get_nowait()
        assert message["project_id"] == "proj-a"
        assert message["project_name"] == "Project A"
        assert message["type"] == "task_created"

    @pytest.mark.asyncio
    async def test_omits_project_fields_when_not_given(self):
        from src.mcp.server.connection_broadcaster import ConnectionBroadcaster

        broadcaster = ConnectionBroadcaster()
        sent = asyncio.Queue()
        broadcaster.sse_queues = [sent]

        await broadcaster.broadcast_update({"type": "task_created", "task_id": "t1"})

        message = sent.get_nowait()
        assert "project_id" not in message
        assert "project_name" not in message

    @pytest.mark.asyncio
    async def test_does_not_mutate_caller_dict(self):
        """broadcast_update must not leak project_id into a dict the
        caller still holds a reference to and might reuse."""
        from src.mcp.server.connection_broadcaster import ConnectionBroadcaster

        broadcaster = ConnectionBroadcaster()
        broadcaster.sse_queues = [asyncio.Queue()]
        original = {"type": "task_created", "task_id": "t1"}

        await broadcaster.broadcast_update(original, project_id="proj-a", project_name="A")

        assert "project_id" not in original


class TestServerStateDelegatesToConnectionBroadcaster:
    """ServerState.broadcast_update and its active_websockets/sse_queues
    properties must reach the same ConnectionBroadcaster instance -- this is
    what keeps server_state.active_websockets.append(...) (direct mutation,
    used by the websocket route) and server_state.broadcast_update(...)
    (used by ~30 call sites) seeing the same connected clients."""

    def test_direct_list_mutation_is_visible_to_broadcast(self):
        from src.mcp.server._shared import ServerState
        from src.mcp.server.connection_broadcaster import ConnectionBroadcaster

        state = ServerState.__new__(ServerState)
        state._broadcaster = ConnectionBroadcaster()

        fake_ws = object()
        state.active_websockets.append(fake_ws)

        assert state._broadcaster.active_websockets == [fake_ws]

    def test_reassigning_the_property_writes_through(self):
        """Covers tests/test_shutdown_closes_devtools_sessions.py's
        monkeypatch.setattr(server_state, "active_websockets", []) -- a
        read-only property would raise AttributeError there."""
        from src.mcp.server._shared import ServerState
        from src.mcp.server.connection_broadcaster import ConnectionBroadcaster

        state = ServerState.__new__(ServerState)
        state._broadcaster = ConnectionBroadcaster()

        state.active_websockets = []
        assert state._broadcaster.active_websockets == []

    @pytest.mark.asyncio
    async def test_broadcast_update_delegates(self):
        from src.mcp.server._shared import ServerState
        from src.mcp.server.connection_broadcaster import ConnectionBroadcaster

        state = ServerState.__new__(ServerState)
        state._broadcaster = ConnectionBroadcaster()
        sent = asyncio.Queue()
        state.sse_queues = [sent]

        await state.broadcast_update({"type": "task_created"}, project_id="proj-a")

        message = sent.get_nowait()
        assert message["project_id"] == "proj-a"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
