"""Regression test: guardian_analysis_for_agent's missing-tmux-session
check must be offloaded to the executor, not called directly on the event
loop.

has_session shells out to the tmux binary -- blocking. This method is
fanned out per-agent via asyncio.create_task specifically so Guardian
analysis runs concurrently across agents (monitor.py); a blocking call
here serializes that per-agent analysis right back into one at a time,
defeating the whole point of the fan-out.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.database import Agent
from src.monitoring.guardian_dispatch import GuardianDispatcher


@pytest.fixture
def dispatcher():
    config = MagicMock()
    config.guardian_min_agent_age_seconds = 60
    return GuardianDispatcher(
        db_manager=MagicMock(),
        agent_manager=MagicMock(),
        config=config,
        guardian=MagicMock(),
        phase_manager=MagicMock(),
        auto_restart=MagicMock(),
        guardian_summaries_cache={},
    )


@pytest.mark.asyncio
async def test_has_session_is_offloaded_to_executor(dispatcher):
    agent = MagicMock(spec=Agent)
    agent.id = "agent-1"
    agent.agent_type = "phase"
    agent.current_task_id = "task-1"
    agent.tmux_session_name = "agent-tmux-1"
    agent.created_at = datetime.utcnow() - timedelta(hours=1)

    mock_session = MagicMock()
    mock_task = MagicMock()
    mock_task.id = "task-1"
    mock_task.status = "in_progress"
    mock_session.query.return_value.filter_by.return_value.first.return_value = mock_task
    dispatcher.db_manager.get_session.return_value = mock_session

    dispatcher.handle_missing_tmux_session = AsyncMock()

    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value=False)  # has_session() -> False (missing)

    with patch("asyncio.get_event_loop", return_value=fake_loop):
        await dispatcher.guardian_analysis_for_agent(agent)

    fake_loop.run_in_executor.assert_called_once_with(
        None, dispatcher.agent_manager.tmux_server.has_session, "agent-tmux-1"
    )
    dispatcher.handle_missing_tmux_session.assert_called_once_with(agent)
