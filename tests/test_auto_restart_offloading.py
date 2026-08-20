"""Regression: AutoRestart.requeue_and_terminate ran two
self.db_manager.session_scope() blocks directly inside async def --
blocking DB I/O on the event loop, unlike the tmux kill_session call three
lines away in the same method, which was already correctly offloaded via
run_in_executor with a comment explaining why."""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from src.core.database import Agent
from src.monitoring.auto_restart import AutoRestart


@pytest.fixture
def auto_restart():
    db_manager = Mock()
    agent_manager = Mock()
    guardian = Mock()
    guardian.record_auto_restart = Mock()
    return AutoRestart(db_manager, agent_manager, guardian)


@pytest.mark.asyncio
async def test_requeue_and_terminate_offloads_both_db_blocks(auto_restart, monkeypatch):
    agent = Agent(
        id="agent-1", tmux_session_name="agent_agent-1", status="working",
        current_task_id="task-1",
    )

    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value=None)
    monkeypatch.setattr("asyncio.get_event_loop", lambda: fake_loop)
    auto_restart.agent_manager._resolve_tmux_transcript_dir = Mock(return_value=None)

    await auto_restart.requeue_and_terminate(agent)

    called_funcs = [c.args[1] for c in fake_loop.run_in_executor.call_args_list]
    assert auto_restart._reset_stuck_task in called_funcs
    assert auto_restart._terminate_and_reset_agent in called_funcs
    assert auto_restart.agent_manager.tmux_server.kill_session in called_funcs
