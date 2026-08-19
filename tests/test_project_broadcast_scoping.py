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


class FakeServerState:
    """Minimal stand-in exposing exactly what broadcast_update touches --
    avoids constructing a full ServerState (heavy init: agent manager,
    LLM client, etc.) just to test the message-merging behavior."""

    def __init__(self):
        self.active_websockets = []
        self.sse_queues = []


class TestBroadcastUpdateProjectTagging:
    @pytest.mark.asyncio
    async def test_merges_project_id_and_name_into_message(self):
        from src.mcp.server._shared import ServerState

        fake = FakeServerState()
        sent = asyncio.Queue()
        fake.sse_queues = [sent]

        await ServerState.broadcast_update(
            fake, {"type": "task_created", "task_id": "t1"},
            project_id="proj-a", project_name="Project A",
        )

        message = sent.get_nowait()
        assert message["project_id"] == "proj-a"
        assert message["project_name"] == "Project A"
        assert message["type"] == "task_created"

    @pytest.mark.asyncio
    async def test_omits_project_fields_when_not_given(self):
        from src.mcp.server._shared import ServerState

        fake = FakeServerState()
        sent = asyncio.Queue()
        fake.sse_queues = [sent]

        await ServerState.broadcast_update(fake, {"type": "task_created", "task_id": "t1"})

        message = sent.get_nowait()
        assert "project_id" not in message
        assert "project_name" not in message

    @pytest.mark.asyncio
    async def test_does_not_mutate_caller_dict(self):
        """broadcast_update must not leak project_id into a dict the
        caller still holds a reference to and might reuse."""
        from src.mcp.server._shared import ServerState

        fake = FakeServerState()
        fake.sse_queues = [asyncio.Queue()]
        original = {"type": "task_created", "task_id": "t1"}

        await ServerState.broadcast_update(fake, original, project_id="proj-a", project_name="A")

        assert "project_id" not in original


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
