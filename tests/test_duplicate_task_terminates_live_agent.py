"""Regression: _check_for_duplicate_task must terminate a duplicate task's
already-live agent, not just tag Task.status="duplicated" as inert metadata.

Observed live: workflow e35be066's product_requirements phase got two
independent tasks (a12d727b, created first via /start_workflow_execution;
cf7dae59, created ~16s later via the orchestrator's own _create_phase_task)
for the same phase. This embedding-based dedup check is asynchronous and
decoupled from either task's own dispatch decision -- by the time it ran
and correctly identified cf7dae59 as a duplicate of a12d727b, cf7dae59's
agent had already been dispatched and was actively running. Marking status
alone left that agent running (burning real cost on already-superseded
work) and occupying a capacity slot that delayed a12d727b's own dispatch
by several minutes -- the exact "second task's agent runs before/instead
of the first's" symptom this closes.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.database import Agent, Task, Workflow


@pytest.fixture
def _wired_server_state(db_manager, monkeypatch):
    import src.mcp.server._create_task_steps as steps

    monkeypatch.setattr(steps.server_state, "db_manager", db_manager)
    return steps.server_state


def _seed_task_with_live_agent(db_manager, task_id, workflow_id, agent_id, agent_status="working"):
    with db_manager.session_scope() as session:
        session.add(Workflow(id=workflow_id, name="w", phases_folder_path="/tmp", status="active"))
        session.add(
            Agent(
                id=agent_id,
                system_prompt="test",
                status=agent_status,
                cli_type="claude",
                tmux_session_name=f"tmux-{agent_id}",
            )
        )
        session.add(
            Task(
                id=task_id,
                workflow_id=workflow_id,
                raw_description="x",
                done_definition="x",
                status="in_progress",
                assigned_agent_id=agent_id,
            )
        )


def await_or_run(coro):
    import asyncio

    return asyncio.run(coro)


def _wire_duplicate_detection(monkeypatch, wired_server_state, duplicate_of="other-task"):
    import src.mcp.server._create_task_steps as steps

    monkeypatch.setattr(steps, "get_config", lambda: MagicMock(task_dedup=MagicMock(task_dedup_enabled=True)))
    wired_server_state.embedding_service = MagicMock(
        generate_embedding=AsyncMock(return_value=[0.1, 0.2, 0.3])
    )
    wired_server_state.task_similarity_service = MagicMock(
        check_for_duplicates=AsyncMock(
            return_value={"is_duplicate": True, "duplicate_of": duplicate_of, "max_similarity": 0.97}
        )
    )


class TestCheckForDuplicateTaskTerminatesLiveAgent:
    def test_terminates_the_duplicate_tasks_live_agent(self, db_manager, _wired_server_state, monkeypatch):
        from src.mcp.server._create_task_steps import _check_for_duplicate_task

        task_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        agent_id = str(uuid.uuid4())
        _seed_task_with_live_agent(db_manager, task_id, workflow_id, agent_id)
        _wire_duplicate_detection(monkeypatch, _wired_server_state)

        terminate_mock = AsyncMock()
        _wired_server_state.agent_manager = MagicMock(terminate_agent=terminate_mock)

        result = await_or_run(_check_for_duplicate_task(task_id, None, {"enriched_description": "x"}))

        assert result is True
        terminate_mock.assert_awaited_once_with(agent_id)

    def test_does_not_terminate_an_already_terminated_agent(self, db_manager, _wired_server_state, monkeypatch):
        """No live agent to stop -- must not call terminate_agent at all."""
        from src.mcp.server._create_task_steps import _check_for_duplicate_task

        task_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        agent_id = str(uuid.uuid4())
        _seed_task_with_live_agent(db_manager, task_id, workflow_id, agent_id, agent_status="terminated")
        _wire_duplicate_detection(monkeypatch, _wired_server_state)

        terminate_mock = AsyncMock()
        _wired_server_state.agent_manager = MagicMock(terminate_agent=terminate_mock)

        result = await_or_run(_check_for_duplicate_task(task_id, None, {"enriched_description": "x"}))

        assert result is True
        terminate_mock.assert_not_awaited()

    def test_no_agent_assigned_yet_does_not_call_terminate(self, db_manager, _wired_server_state, monkeypatch):
        """The common, non-racing case: dedup runs before this task has
        been dispatched at all -- nothing to terminate."""
        from src.mcp.server._create_task_steps import _check_for_duplicate_task

        task_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        with db_manager.session_scope() as session:
            session.add(Workflow(id=workflow_id, name="w", phases_folder_path="/tmp", status="active"))
            session.add(
                Task(
                    id=task_id, workflow_id=workflow_id, raw_description="x", done_definition="x",
                    status="pending",
                )
            )
        _wire_duplicate_detection(monkeypatch, _wired_server_state)

        terminate_mock = AsyncMock()
        _wired_server_state.agent_manager = MagicMock(terminate_agent=terminate_mock)

        result = await_or_run(_check_for_duplicate_task(task_id, None, {"enriched_description": "x"}))

        assert result is True
        terminate_mock.assert_not_awaited()

    def test_marks_task_duplicated_before_terminating(self, db_manager, _wired_server_state, monkeypatch):
        """The DB write must be committed (and the session closed) before
        the terminate call -- terminate_agent resets stray assigned/
        in_progress tasks back to "pending", which would otherwise
        silently undo the "duplicated" status this same call just set."""
        from src.mcp.server._create_task_steps import _check_for_duplicate_task

        task_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        agent_id = str(uuid.uuid4())
        _seed_task_with_live_agent(db_manager, task_id, workflow_id, agent_id)
        _wire_duplicate_detection(monkeypatch, _wired_server_state, duplicate_of="original-task")

        status_when_terminate_called = {}

        async def _fake_terminate(aid):
            with db_manager.session_scope() as session:
                t = session.query(Task).filter_by(id=task_id).first()
                status_when_terminate_called["status"] = t.status

        _wired_server_state.agent_manager = MagicMock(terminate_agent=_fake_terminate)

        await_or_run(_check_for_duplicate_task(task_id, None, {"enriched_description": "x"}))

        assert status_when_terminate_called["status"] == "duplicated"
