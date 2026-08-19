"""Regression coverage for FrontendAPI.stop_workflow's tmux kill-session
offloading (Phase 3 Tier 2 item 10, docs/AUTOPILOT_REFACTOR_PLAN.md).

stop_workflow called subprocess.run(["tmux", "kill-session", ...]) directly
-- reset_phase, a few lines below in the same file, does the identical
operation but correctly offloads it via loop.run_in_executor. An
un-offloaded subprocess.run blocks the whole asyncio event loop (every
other request this process is serving) for as long as the tmux CLI takes
to respond, once per agent being stopped.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.database import Agent, DatabaseManager, Task, Workflow
from src.mcp.frontend._shared import FrontendAPI


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "test.db"))
    manager.create_tables()
    return manager


@pytest.fixture
def frontend_api(db):
    return FrontendAPI(db_manager=db, agent_manager=None)


def _seed_active_workflow_with_working_agent(db):
    session = db.get_session()
    session.add(
        Workflow(
            id="wf-1",
            name="wf-1",
            phases_folder_path="/tmp",
            status="active",
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        Task(
            id="task-1",
            raw_description="do it",
            done_definition="done",
            status="in_progress",
            workflow_id="wf-1",
            assigned_agent_id="agent-1",
        )
    )
    session.add(
        Agent(
            id="agent-1",
            system_prompt="p",
            status="working",
            cli_type="test",
            current_task_id="task-1",
            tmux_session_name="agent-tmux-1",
        )
    )
    session.commit()
    session.close()


@pytest.mark.asyncio
async def test_kill_session_is_offloaded_to_executor(frontend_api, db):
    _seed_active_workflow_with_working_agent(db)

    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value=None)

    with (
        patch("asyncio.get_event_loop", return_value=fake_loop),
        patch("src.mcp.frontend._shared.terminate_agent", return_value=True),
    ):
        result = await frontend_api.stop_workflow("wf-1")

    assert result["success"] is True
    fake_loop.run_in_executor.assert_called_once()
    # First positional arg is the executor (None = default), second is the
    # functools.partial wrapping subprocess.run -- confirms the tmux kill
    # actually went through the executor, not a direct blocking call.
    executor_arg, partial_arg = fake_loop.run_in_executor.call_args.args
    assert executor_arg is None
    assert partial_arg.func.__name__ == "run"
    assert partial_arg.args[0] == ["tmux", "kill-session", "-t", "agent-tmux-1"]
