"""Regression test: _complete_task_normally must defer agent termination
until after the HTTP response is sent, not fire it as a concurrent
background task.

Found live 2026-09-03: a task's own complete_my_task call committed
"done" almost instantly, then _complete_task_normally scheduled agent
termination via spawn_background_task (asyncio.create_task -- no
ordering guarantee relative to the response). The rest of the handler
(spec-gate evaluation, which can include an LLM call scoring a large
review document) took over a minute to finish in that incident, while
the fire-and-forget termination killed the agent's tmux session -- and
with it the MCP client process still waiting on the response -- within
~10 seconds. The eventual response had no process left to deliver it
to; the agent saw a permanent "Connection closed" on complete_my_task
even though the task had already succeeded.

The fix: schedule termination via FastAPI's BackgroundTasks instead,
which the ASGI framework guarantees runs only after the response has
been sent. These tests prove the ordering directly: termination must
not have run by the time _complete_task_normally returns, and must run
once background_tasks is invoked (simulating what Starlette does after
sending the response).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks

from src.core.database import Task, Workflow
from src.mcp.server._shared import UpdateTaskStatusRequest


@pytest.fixture
def _wired_server_state(monkeypatch):
    import src.mcp.server._update_task_status_steps as steps

    fake_state = MagicMock()
    fake_state.agent_manager.terminate_agent = AsyncMock(return_value=None)
    monkeypatch.setattr(steps, "server_state", fake_state)
    return fake_state


@pytest.mark.asyncio
async def test_termination_has_not_run_when_complete_task_normally_returns(
    db_manager, _wired_server_state
):
    from src.mcp.server._update_task_status_steps import _complete_task_normally

    workflow_id = "wf-defer-term"
    task_id = "task-defer-term"
    with db_manager.session_scope() as session:
        session.add(Workflow(id=workflow_id, name="w", phases_folder_path="/tmp", status="active"))
        session.add(Task(id=task_id, workflow_id=workflow_id, raw_description="r", done_definition="d", status="in_progress"))

    request = UpdateTaskStatusRequest(task_id=task_id, status="done", summary="done")
    background_tasks = BackgroundTasks()

    with db_manager.session_scope() as session:
        task = session.query(Task).filter_by(id=task_id).first()
        with patch(
            "src.mcp.server._update_task_status_steps.terminate_agents_and_process_queue",
            new_callable=AsyncMock,
        ) as mock_terminate:
            await _complete_task_normally(session, "agent-1", task, request, phase=None, background_tasks=background_tasks)
            assert not mock_terminate.called, (
                "termination must not run before the response is sent -- "
                "it was scheduled directly instead of via background_tasks"
            )

            await background_tasks()
            mock_terminate.assert_called_once_with(_wired_server_state.agent_manager, ["agent-1"])


@pytest.mark.asyncio
async def test_termination_runs_after_slow_post_completion_work_via_background_tasks(
    db_manager, _wired_server_state
):
    """Even if the rest of the request pipeline were slow, termination
    scheduled through BackgroundTasks only runs when the framework
    invokes it post-response -- never concurrently with the handler."""
    from src.mcp.server._update_task_status_steps import _complete_task_normally

    workflow_id = "wf-defer-term-2"
    task_id = "task-defer-term-2"
    with db_manager.session_scope() as session:
        session.add(Workflow(id=workflow_id, name="w", phases_folder_path="/tmp", status="active"))
        session.add(Task(id=task_id, workflow_id=workflow_id, raw_description="r", done_definition="d", status="in_progress"))

    request = UpdateTaskStatusRequest(task_id=task_id, status="failed", failure_reason="x")
    background_tasks = BackgroundTasks()

    with db_manager.session_scope() as session:
        task = session.query(Task).filter_by(id=task_id).first()
        with patch(
            "src.mcp.server._update_task_status_steps.terminate_agents_and_process_queue",
            new_callable=AsyncMock,
        ) as mock_terminate:
            await _complete_task_normally(session, "agent-1", task, request, phase=None, background_tasks=background_tasks)
            assert not mock_terminate.called

    assert len(background_tasks.tasks) == 1, "exactly one termination task must be queued, not fired immediately"
    await background_tasks()
    mock_terminate.assert_called_once()
