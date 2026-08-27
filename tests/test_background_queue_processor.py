"""Tests for background_queue_processor's project scoping
(src/mcp/server.py).

Part of the multi-project concurrency fix: process_queue() used to be
called once per tick with no project scoping at all -- one busy project
could consume the entire global max_concurrent_agents budget and starve
a quieter active project's queue. background_queue_processor must now
give every currently-active project its own turn each tick.
"""

import asyncio

import pytest

from src.core.database import AutopilotProject, DatabaseManager, Workflow


@pytest.fixture
def db_manager(tmp_path):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _make_active_project(db_manager, project_id):
    with db_manager.session_scope() as session:
        session.add(
            AutopilotProject(
                id=project_id, name=project_id, base_dir=f"/tmp/{project_id}", is_active=True
            )
        )


class TestBackgroundQueueProcessorProjectScoping:
    @pytest.mark.asyncio
    async def test_calls_process_queue_once_per_active_project(
        self, db_manager, monkeypatch
    ):
        # Phase 1c: server.py is now a package. background_loops owns the
        # sweep/queue-processor functions and reads server_state from
        # _shared, so patch where the name is looked up.
        from src.mcp.server import background_loops as server
        from src.services.queue_service import QueueService

        _make_active_project(db_manager, "proj-a")
        _make_active_project(db_manager, "proj-b")

        monkeypatch.setattr(server.server_state, "db_manager", db_manager)
        monkeypatch.setattr(
            server.server_state,
            "queue_service",
            QueueService(db_manager, max_concurrent_agents=3),
        )
        server.server_state.shutdown_event = asyncio.Event()

        processed_project_ids = []

        async def fake_process_queue(project_id=None):
            processed_project_ids.append(project_id)
            if len(processed_project_ids) >= 2:
                server.server_state.shutdown_event.set()

        monkeypatch.setattr(server, "process_queue", fake_process_queue)

        # Seed one queued task per project so get_queue_status reports
        # queued_tasks_count > 0 and process_queue actually gets called.
        with db_manager.session_scope() as session:
            session.add(
                Workflow(id="wf-a", name="wf-a", status="active", project_id="proj-a", phases_folder_path="/tmp")
            )
            session.add(
                Workflow(id="wf-b", name="wf-b", status="active", project_id="proj-b", phases_folder_path="/tmp")
            )
        import uuid

        from src.core.database import Task

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id=str(uuid.uuid4()), workflow_id="wf-a", raw_description="r",
                    done_definition="d", status="queued", priority="medium",
                )
            )
            session.add(
                Task(
                    id=str(uuid.uuid4()), workflow_id="wf-b", raw_description="r",
                    done_definition="d", status="queued", priority="medium",
                )
            )

        await server.background_queue_processor()

        assert set(processed_project_ids) == {"proj-a", "proj-b"}

    @pytest.mark.asyncio
    async def test_falls_back_to_global_when_no_project_active(
        self, db_manager, monkeypatch
    ):
        import uuid

        from src.core.database import Task

        # Phase 1c: server.py is now a package. background_loops owns the
        # sweep/queue-processor functions and reads server_state from
        # _shared, so patch where the name is looked up.
        from src.mcp.server import background_loops as server
        from src.services.queue_service import QueueService

        monkeypatch.setattr(server.server_state, "db_manager", db_manager)
        monkeypatch.setattr(
            server.server_state,
            "queue_service",
            QueueService(db_manager, max_concurrent_agents=3),
        )
        server.server_state.shutdown_event = asyncio.Event()

        processed_project_ids = []

        async def fake_process_queue(project_id=None):
            processed_project_ids.append(project_id)
            server.server_state.shutdown_event.set()

        monkeypatch.setattr(server, "process_queue", fake_process_queue)

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id=str(uuid.uuid4()), raw_description="r",
                    done_definition="d", status="queued", priority="medium",
                )
            )

        await server.background_queue_processor()

        assert processed_project_ids == [None]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
