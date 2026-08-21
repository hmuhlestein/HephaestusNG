"""Regression coverage for the /stop and /cancel workflow-execution
endpoints' tmux kill-session offloading.

Both endpoints (src/mcp/server/workflow_execution_routes.py) called
subprocess.run(["tmux", "kill-session", ...]) directly inside their
per-agent termination loop -- an un-offloaded subprocess.run blocks the
whole asyncio event loop (every other request this process is serving)
for as long as the tmux CLI takes to respond (up to its 5s timeout), once
per agent being stopped/cancelled. src/mcp/frontend/_shared.py's separate
FrontendAPI.stop_workflow already had this same class of bug fixed
(test_stop_workflow_offloading.py) -- this is the actual live MCP route
handler, a distinct code path with the identical bug.
"""

import os
import tempfile
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.core.database import Agent, DatabaseManager, Task, Workflow
from src.mcp.server import app


@pytest.fixture
def test_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()

    yield db_manager

    os.unlink(db_path)


@pytest.fixture
def test_client(test_db, monkeypatch):
    import src.mcp.server._shared as server_module

    monkeypatch.setattr(server_module.server_state, "db_manager", test_db)
    monkeypatch.setattr(server_module.server_state, "initialized", True, raising=False)
    return TestClient(app)


def _seed_active_workflow_with_working_agent(test_db, status="active"):
    session = test_db.get_session()
    workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"

    session.add(
        Workflow(
            id=workflow_id, name="t", phases_folder_path="/tmp", status=status,
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        Task(
            id=task_id, raw_description="do it", done_definition="done",
            status="in_progress", workflow_id=workflow_id, assigned_agent_id=agent_id,
        )
    )
    session.add(
        Agent(
            id=agent_id, system_prompt="p", status="working", cli_type="test",
            current_task_id=task_id, tmux_session_name="agent-tmux-1",
        )
    )
    session.commit()
    session.close()
    return workflow_id


@pytest.mark.parametrize("endpoint_suffix", ["stop", "cancel"])
def test_kill_session_is_offloaded_to_executor(endpoint_suffix, test_client, test_db):
    workflow_id = _seed_active_workflow_with_working_agent(test_db)

    fake_loop = MagicMock()

    async def run_now(_executor, func, *args):
        return func(*args)

    fake_loop.run_in_executor = AsyncMock(side_effect=run_now)

    with (
        patch("asyncio.get_event_loop", return_value=fake_loop),
        patch(
            "src.autopilot.orchestrator.engine_client.terminate_agent",
            return_value=True,
        ) as mock_terminate,
    ):
        resp = test_client.post(
            f"/api/workflow-executions/{workflow_id}/{endpoint_suffix}", json={}
        )

    assert resp.status_code == 200

    kill_calls = [
        c for c in fake_loop.run_in_executor.call_args_list
        if getattr(c.args[1], "__name__", None) == "_kill_tmux_session"
    ]
    assert len(kill_calls) == 1
    executor_arg, func_arg, session_name_arg = kill_calls[0].args
    assert executor_arg is None
    assert session_name_arg == "agent-tmux-1"

    # terminate_agent must be reached THROUGH run_in_executor (wrapped in a
    # functools.partial), not called directly on the event loop -- it does
    # blocking DB queries.
    terminate_calls = [
        c for c in fake_loop.run_in_executor.call_args_list
        if getattr(c.args[1], "func", None) is mock_terminate
    ]
    assert len(terminate_calls) == 1
    mock_terminate.assert_called_once()

    # The Task/Agent lookup queries inside _terminate_workflow_agents must
    # also go through run_in_executor -- at least 4 total calls for this
    # one-agent workflow (task query, agent query, tmux kill, terminate).
    assert fake_loop.run_in_executor.call_count >= 4
