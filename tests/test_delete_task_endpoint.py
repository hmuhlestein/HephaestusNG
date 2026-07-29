"""Regression: pause/cancel only apply to pending/queued/in-progress tasks
and never remove the row -- an old, stuck task (e.g. one sitting 'blocked'
or 'in_progress' with a long-dead agent from a past run) had no way to
actually disappear from the queue view. DELETE /api/tasks/{task_id}
removes the task outright, in any status, terminating its assigned agent
first (which clears Agent.current_task_id -- required since foreign_keys
is enforced) and cleaning up dependent records before deleting the row.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.database import Agent, DatabaseManager, Memory, Task, Workflow


@pytest.fixture
def test_db(tmp_path):
    db_path = tmp_path / "test.db"
    manager = DatabaseManager(str(db_path))
    manager.create_tables()
    return manager


async def _run_delete(test_db, task_id, agent_manager=None):
    import src.mcp.server as server_module

    with patch.object(server_module, "server_state") as mock_state:
        mock_state.db_manager = test_db
        mock_state.agent_manager = agent_manager or MagicMock(
            terminate_agent=AsyncMock()
        )
        mock_state.broadcast_update = AsyncMock()
        return await server_module.delete_task_endpoint(task_id)


class TestDeleteTaskEndpoint:
    @pytest.mark.asyncio
    async def test_deletes_a_task_with_no_agent(self, test_db):
        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-1", name="t", phases_folder_path="/tmp",
                status="active", definition_id="autopilot",
            )
        )
        session.add(
            Task(
                id="task-1", workflow_id="wf-1", phase_id="phase-1",
                raw_description="r", done_definition="d", status="pending",
            )
        )
        session.commit()
        session.close()

        result = await _run_delete(test_db, "task-1")

        assert result == {"success": True, "task_id": "task-1"}
        session = test_db.get_session()
        assert session.query(Task).filter_by(id="task-1").first() is None
        session.close()

    @pytest.mark.asyncio
    async def test_terminates_assigned_agent_before_deleting(self, test_db):
        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-2", name="t", phases_folder_path="/tmp",
                status="active", definition_id="autopilot",
            )
        )
        session.add(
            Agent(id="agent-1", system_prompt="p", status="working", cli_type="claude")
        )
        session.add(
            Task(
                id="task-2", workflow_id="wf-2", phase_id="phase-1",
                raw_description="r", done_definition="d",
                status="in_progress", assigned_agent_id="agent-1",
            )
        )
        session.commit()
        session.close()

        mock_agent_manager = MagicMock(terminate_agent=AsyncMock())
        result = await _run_delete(test_db, "task-2", agent_manager=mock_agent_manager)

        assert result["success"] is True
        mock_agent_manager.terminate_agent.assert_awaited_once_with("agent-1")
        session = test_db.get_session()
        assert session.query(Task).filter_by(id="task-2").first() is None
        session.close()

    @pytest.mark.asyncio
    async def test_cleans_up_dependent_records(self, test_db):
        """Sanity check: with foreign_keys=ON, deleting a task that still
        has a referencing Memory row would otherwise raise IntegrityError."""
        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-3", name="t", phases_folder_path="/tmp",
                status="active", definition_id="autopilot",
            )
        )
        session.add(
            Agent(id="agent-2", system_prompt="p", status="idle", cli_type="claude")
        )
        session.add(
            Task(
                id="task-3", workflow_id="wf-3", phase_id="phase-1",
                raw_description="r", done_definition="d", status="done",
            )
        )
        session.add(
            Memory(
                id="mem-1", agent_id="agent-2", content="learned something",
                memory_type="learning", related_task_id="task-3",
            )
        )
        session.commit()
        session.close()

        result = await _run_delete(test_db, "task-3")

        assert result["success"] is True
        session = test_db.get_session()
        assert session.query(Task).filter_by(id="task-3").first() is None
        assert session.query(Memory).filter_by(id="mem-1").first() is None
        session.close()

    @pytest.mark.asyncio
    async def test_deletes_task_with_cost_history(self, test_db):
        """CostEntry.task_id is also an enforced FK -- a task that ever
        recorded real LLM cost (the common case, not the exception, now
        that cost tracking exists) would otherwise fail to delete with an
        IntegrityError."""
        from src.core.database import CostEntry

        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-4", name="t", phases_folder_path="/tmp",
                status="active", definition_id="autopilot",
            )
        )
        session.add(
            Task(
                id="task-4", workflow_id="wf-4", phase_id="phase-1",
                raw_description="r", done_definition="d", status="done",
            )
        )
        session.add(
            CostEntry(
                id="cost-1", task_id="task-4", workflow_id="wf-4",
                source="pi", cost_usd=0.05,
            )
        )
        session.commit()
        session.close()

        result = await _run_delete(test_db, "task-4")

        assert result["success"] is True
        session = test_db.get_session()
        assert session.query(Task).filter_by(id="task-4").first() is None
        assert session.query(CostEntry).filter_by(id="cost-1").first() is None
        session.close()

    @pytest.mark.asyncio
    async def test_missing_task_returns_404(self, test_db):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await _run_delete(test_db, "does-not-exist")
        assert exc_info.value.status_code == 404
