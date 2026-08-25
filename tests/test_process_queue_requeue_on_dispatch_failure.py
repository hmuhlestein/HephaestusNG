"""Regression tests for process_queue's dequeue/dispatch compensation
(src/mcp/server/background_loops.py).

process_queue calls dequeue_task (status "queued" -> "assigned") before
enrichment and dispatch, both of which can raise. The blanket handler used
to just log, leaving the task "assigned" with assigned_agent_id=None --
invisible to get_next_queued_task (which only reads "queued") and to every
recovery sweep (mechanical_recovery's detectors find their task via
filter_by(assigned_agent_id=agent.id); _clean_stale_assigned_tasks requires
assigned_agent_id isnot(None)). Observed live: a review task stranded that
way while its workflow stayed active for hours.
"""

import uuid

import pytest

from src.core.database import DatabaseManager, Task, Workflow


@pytest.fixture
def db_manager(tmp_path):
    db = DatabaseManager(str(tmp_path / "test.db"))
    db.create_tables()
    return db


@pytest.fixture
def queued_task_id(db_manager):
    task_id = str(uuid.uuid4())
    with db_manager.session_scope() as session:
        session.add(
            Workflow(id="wf-1", name="wf-1", status="active", phases_folder_path="/tmp")
        )
        session.add(
            Task(
                id=task_id,
                workflow_id="wf-1",
                raw_description="r",
                enriched_description="already enriched",
                done_definition="d",
                status="queued",
                priority="medium",
            )
        )
    return task_id


def _wire_server_state(monkeypatch, db_manager):
    from src.mcp.server import background_loops as server
    from src.services.queue_service import QueueService

    monkeypatch.setattr(server.server_state, "db_manager", db_manager)
    monkeypatch.setattr(
        server.server_state,
        "queue_service",
        QueueService(db_manager, max_concurrent_agents=3),
    )
    monkeypatch.setattr(server.server_state, "phase_manager", None)
    return server


def _fetch(db_manager, task_id):
    with db_manager.session_scope() as session:
        task = session.query(Task).filter_by(id=task_id).first()
        return task.status, task.assigned_agent_id


class TestProcessQueueRequeueOnDispatchFailure:
    @pytest.mark.asyncio
    async def test_dispatch_failure_returns_task_to_queue(
        self, db_manager, queued_task_id, monkeypatch
    ):
        from src.services.agent_dispatch_service import AgentDispatchService

        server = _wire_server_state(monkeypatch, db_manager)

        async def fake_build_context(**kwargs):
            return {"phase_cli_tool": None, "phase_cli_model": None}

        async def failing_dispatch(**kwargs):
            raise RuntimeError("agent launch failed")

        monkeypatch.setattr(
            AgentDispatchService, "build_dispatch_context", fake_build_context
        )
        monkeypatch.setattr(AgentDispatchService, "dispatch", failing_dispatch)

        await server.process_queue()

        status, agent_id = _fetch(db_manager, queued_task_id)
        assert status == "queued", (
            f"task stranded in {status!r} after failed dispatch -- "
            "no queue read or recovery sweep can see it again"
        )
        assert agent_id is None

    @pytest.mark.asyncio
    async def test_failure_after_successful_dispatch_does_not_requeue(
        self, db_manager, queued_task_id, monkeypatch
    ):
        """An agent is already running on the task -- requeueing would
        launch a second one for the same work."""
        from src.services.agent_dispatch_service import AgentDispatchService

        server = _wire_server_state(monkeypatch, db_manager)

        class _Agent:
            id = "agent-1"

        async def fake_build_context(**kwargs):
            return {"phase_cli_tool": None, "phase_cli_model": None}

        async def ok_dispatch(**kwargs):
            return _Agent()

        def failing_mark_assigned(*args, **kwargs):
            raise RuntimeError("commit failed")

        monkeypatch.setattr(
            AgentDispatchService, "build_dispatch_context", fake_build_context
        )
        monkeypatch.setattr(AgentDispatchService, "dispatch", ok_dispatch)
        monkeypatch.setattr(
            AgentDispatchService, "mark_assigned", failing_mark_assigned
        )

        await server.process_queue()

        status, _ = _fetch(db_manager, queued_task_id)
        assert status == "assigned"


class TestCliModelReservationRelease:
    """get_next_queued_task reserves the task's cli/model slot, but the
    release used to sit in a finally wrapping only the dispatch call.
    Enrichment and context-building run in between and can raise, leaking
    the slot for the life of the process -- a combo limited to one slot is
    then permanently undispatchable."""

    @pytest.mark.asyncio
    async def test_reservation_released_when_failure_precedes_dispatch(
        self, db_manager, queued_task_id, monkeypatch
    ):
        from src.services.agent_dispatch_service import AgentDispatchService

        server = _wire_server_state(monkeypatch, db_manager)
        qs = server.server_state.queue_service

        real_next = qs.get_next_queued_task

        def next_with_reservation(project_id=None):
            task = real_next(project_id)
            task._reserved_cli_model = ("claude", "sonnet")
            return task

        released = []
        monkeypatch.setattr(qs, "get_next_queued_task", next_with_reservation)
        monkeypatch.setattr(
            qs, "release_cli_model_slot", lambda *args: released.append(args)
        )

        async def failing_build_context(**kwargs):
            raise RuntimeError("context build failed before dispatch")

        monkeypatch.setattr(
            AgentDispatchService, "build_dispatch_context", failing_build_context
        )

        await server.process_queue()

        assert released == [("claude", "sonnet")], (
            "slot leaked -- this combo can never be dispatched on again"
        )
        assert _fetch(db_manager, queued_task_id)[0] == "queued"

    @pytest.mark.asyncio
    async def test_reservation_released_exactly_once_on_success(
        self, db_manager, queued_task_id, monkeypatch
    ):
        """release_cli_model_slot decrements a counter -- releasing twice
        hands out a slot another dispatch is still holding."""
        from src.services.agent_dispatch_service import AgentDispatchService

        server = _wire_server_state(monkeypatch, db_manager)
        qs = server.server_state.queue_service

        real_next = qs.get_next_queued_task

        def next_with_reservation(project_id=None):
            task = real_next(project_id)
            task._reserved_cli_model = ("claude", "sonnet")
            return task

        released = []
        monkeypatch.setattr(qs, "get_next_queued_task", next_with_reservation)
        monkeypatch.setattr(
            qs, "release_cli_model_slot", lambda *args: released.append(args)
        )

        class _Agent:
            id = "agent-1"

        async def fake_build_context(**kwargs):
            return {"phase_cli_tool": None, "phase_cli_model": None}

        async def ok_dispatch(**kwargs):
            return _Agent()

        monkeypatch.setattr(
            AgentDispatchService, "build_dispatch_context", fake_build_context
        )
        monkeypatch.setattr(AgentDispatchService, "dispatch", ok_dispatch)
        monkeypatch.setattr(
            AgentDispatchService, "mark_assigned", lambda *a, **k: None
        )
        monkeypatch.setattr(
            server.server_state, "broadcast_update", _noop_broadcast
        )

        await server.process_queue()

        assert released == [("claude", "sonnet")]


async def _noop_broadcast(*args, **kwargs):
    return None


class TestBumpEndpointRequeueOnDispatchFailure:
    """bump_task_priority_endpoint repeats process_queue's non-atomic
    dequeue-then-dispatch, so it strands tasks the same way."""

    @pytest.mark.asyncio
    async def test_dispatch_failure_returns_task_to_queue(
        self, db_manager, queued_task_id, monkeypatch
    ):
        from fastapi import HTTPException

        from src.mcp.server import task_admin_routes
        from src.services.agent_dispatch_service import AgentDispatchService

        # task_admin_routes reads the same _shared.server_state singleton.
        _wire_server_state(monkeypatch, db_manager)

        async def failing_build_context(**kwargs):
            raise RuntimeError("agent launch failed")

        monkeypatch.setattr(
            AgentDispatchService, "build_dispatch_context", failing_build_context
        )

        with pytest.raises(HTTPException):
            await task_admin_routes.bump_task_priority_endpoint(task_id=queued_task_id, x_agent_id="system")

        status, agent_id = _fetch(db_manager, queued_task_id)
        assert status == "queued", (
            f"task stranded in {status!r} after a failed priority bump"
        )
        assert agent_id is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
