"""Regression test: _complete_task_normally must not block the event loop
collecting cost data.

Found live 2026-08-19, tracing why the backend intermittently reported
"unreachable" (heph status) despite the process itself running fine and
logging continuously: TaskCompletionService.collect_cost_on_completion
was called directly inside this async function, with no thread-pool
offload. It reads the CLI's own transcript file and cascades through the
same synchronous task -> workflow -> feature -> design -> project cost
rollup as _invoke_and_record's own cost recording (llm_client.py,
fixed in the same investigation) -- runs on EVERY task completion (done or
failed), not just every LLM call, making it the more frequent of the two
event-loop-blocking call sites. With 3 agents completing tasks
concurrently, /health -- a bare dict return with zero I/O of its own --
intermittently took 2+ seconds or timed out, because nothing else on the
single-threaded event loop could run while this chain of synchronous
SQLite writes and file reads was in progress.
"""

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
async def test_collect_cost_on_completion_runs_off_the_event_loop_thread(
    db_manager, _wired_server_state, monkeypatch
):
    from src.mcp.server._update_task_status_steps import _complete_task_normally

    workflow_id = "wf-cc-offload"
    task_id = "task-cc-offload"
    with db_manager.session_scope() as session:
        session.add(
            Workflow(id=workflow_id, name="w", phases_folder_path="/tmp", status="active")
        )
        session.add(
            Task(
                id=task_id, workflow_id=workflow_id, raw_description="r", done_definition="d",
                status="in_progress",
            )
        )

    main_thread_id = threading.get_ident()
    call_thread_id = {}

    def _fake_collect_cost_on_completion(tid):
        call_thread_id["id"] = threading.get_ident()

    request = UpdateTaskStatusRequest(task_id=task_id, status="failed", failure_reason="x")

    with db_manager.session_scope() as session:
        task = session.query(Task).filter_by(id=task_id).first()
        with patch(
            "src.services.task_completion_service.TaskCompletionService.collect_cost_on_completion",
            side_effect=_fake_collect_cost_on_completion,
        ):
            await _complete_task_normally(session, "agent-1", task, request, phase=None)

    assert call_thread_id.get("id") is not None, "collect_cost_on_completion was never called"
    assert call_thread_id["id"] != main_thread_id, (
        "collect_cost_on_completion ran on the event loop's own thread -- "
        "it must run in the executor's thread pool instead"
    )
