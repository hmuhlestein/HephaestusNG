"""Regression: _resume_interrupted_workflows' on-demand retry path
(reactivate=True, the design-level Play/Resume button's /api/autopilot/recover
call) only reset tasks in "failed" status, not tasks individually paused
("blocked", via /api/tasks/{id}/pause). A workflow whose only non-terminal
task was "blocked" flipped back to "active" on every Resume click but never
re-dispatched that task -- invisible to both the failed-task reset and the
orphaned-agent scan below it -- so the workflow looked like it "immediately
paused again" no matter how many times Play was pressed.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.core.database import DatabaseManager, Task, Workflow


@pytest.fixture
def test_db(tmp_path):
    db_path = tmp_path / "test.db"
    manager = DatabaseManager(str(db_path))
    manager.create_tables()
    return manager


async def _run_resume(test_db, workflow_id=None, project_id=None):
    import src.mcp.server as server_module

    with patch.object(server_module, "server_state") as mock_state:
        mock_state.db_manager = test_db
        mock_state.agent_manager = MagicMock()
        mock_state.queue_service.should_queue_task.return_value = True
        return await server_module._resume_interrupted_workflows(
            workflow_id=workflow_id, project_id=project_id, reactivate=True
        )


class TestResumeInterruptedWorkflowsUnblocksTasks:
    @pytest.mark.asyncio
    async def test_blocked_task_is_reset_and_requeued(self, test_db):
        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-blocked", name="t", phases_folder_path="/tmp",
                status="paused", definition_id="feature_architect",
            )
        )
        session.add(
            Task(
                id="task-blocked", workflow_id="wf-blocked", phase_id="phase-1",
                raw_description="r", done_definition="d",
                status="blocked", assigned_agent_id="old-agent",
            )
        )
        session.commit()
        session.close()

        result = await _run_resume(test_db, "wf-blocked")

        session = test_db.get_session()
        task = session.query(Task).filter_by(id="task-blocked").first()
        wf = session.query(Workflow).filter_by(id="wf-blocked").first()
        assert task.status == "pending"
        assert task.assigned_agent_id is None
        assert wf.status == "active"
        session.close()
        assert result["resumed"] == 1

    @pytest.mark.asyncio
    async def test_failed_task_still_reset_and_requeued(self, test_db):
        """Sanity check the fix isn't overbroad: pre-existing "failed" task
        handling must still work exactly as before."""
        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-failed", name="t", phases_folder_path="/tmp",
                status="paused", definition_id="feature_architect",
            )
        )
        session.add(
            Task(
                id="task-failed", workflow_id="wf-failed", phase_id="phase-1",
                raw_description="r", done_definition="d",
                status="failed", failure_reason="boom",
            )
        )
        session.commit()
        session.close()

        result = await _run_resume(test_db, "wf-failed")

        session = test_db.get_session()
        task = session.query(Task).filter_by(id="task-failed").first()
        session.close()
        assert task.status == "pending"
        assert task.failure_reason is None
        assert result["resumed"] == 1


class TestResumeInterruptedWorkflowsProjectScoping:
    """Regression: the project-level Play button, on hitting the "already
    running" self-conflict 409, used to just show a no-op toast -- the
    service loop being up doesn't by itself re-drive a workflow stuck on an
    individually-blocked task, so a project could sit paused forever no
    matter how many times Play was pressed. Play now cascades into
    recovering every one of the project's own workflows, scoped by
    project_id instead of a single workflow_id."""

    @pytest.mark.asyncio
    async def test_recovers_every_workflow_in_the_project(self, test_db):
        session = test_db.get_session()
        session.add_all(
            [
                Workflow(
                    id="wf-proj-a-1", name="t", phases_folder_path="/tmp",
                    status="paused", definition_id="feature_architect",
                    project_id="proj-a",
                ),
                Workflow(
                    id="wf-proj-a-2", name="t", phases_folder_path="/tmp",
                    status="failed", definition_id="feature_architect",
                    project_id="proj-a",
                ),
            ]
        )
        session.add_all(
            [
                Task(
                    id="task-a1", workflow_id="wf-proj-a-1", phase_id="phase-1",
                    raw_description="r", done_definition="d", status="blocked",
                ),
                Task(
                    id="task-a2", workflow_id="wf-proj-a-2", phase_id="phase-1",
                    raw_description="r", done_definition="d", status="failed",
                ),
            ]
        )
        session.commit()
        session.close()

        result = await _run_resume(test_db, project_id="proj-a")

        session = test_db.get_session()
        task_a1 = session.query(Task).filter_by(id="task-a1").first()
        task_a2 = session.query(Task).filter_by(id="task-a2").first()
        wf_a1 = session.query(Workflow).filter_by(id="wf-proj-a-1").first()
        wf_a2 = session.query(Workflow).filter_by(id="wf-proj-a-2").first()
        session.close()
        assert task_a1.status == "pending"
        assert task_a2.status == "pending"
        assert wf_a1.status == "active"
        assert wf_a2.status == "active"
        assert result["resumed"] == 2

    @pytest.mark.asyncio
    async def test_does_not_touch_a_different_projects_workflows(self, test_db):
        session = test_db.get_session()
        session.add_all(
            [
                Workflow(
                    id="wf-proj-a", name="t", phases_folder_path="/tmp",
                    status="paused", definition_id="feature_architect",
                    project_id="proj-a",
                ),
                Workflow(
                    id="wf-proj-b", name="t", phases_folder_path="/tmp",
                    status="paused", definition_id="feature_architect",
                    project_id="proj-b",
                ),
            ]
        )
        session.add_all(
            [
                Task(
                    id="task-a", workflow_id="wf-proj-a", phase_id="phase-1",
                    raw_description="r", done_definition="d", status="blocked",
                ),
                Task(
                    id="task-b", workflow_id="wf-proj-b", phase_id="phase-1",
                    raw_description="r", done_definition="d", status="blocked",
                ),
            ]
        )
        session.commit()
        session.close()

        result = await _run_resume(test_db, project_id="proj-a")

        session = test_db.get_session()
        task_b = session.query(Task).filter_by(id="task-b").first()
        wf_b = session.query(Workflow).filter_by(id="wf-proj-b").first()
        session.close()
        assert task_b.status == "blocked"
        assert wf_b.status == "paused"
        assert result["resumed"] == 1
