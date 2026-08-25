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
    # guardian_analysis_for_agent reads this nested under .monitoring, not
    # flat on config -- a bare `config.guardian_min_agent_age_seconds = 60`
    # leaves the code's actual read as an auto-created MagicMock, which
    # blows up comparing `float < MagicMock` before reaching any of the
    # logic these tests exist to exercise.
    config.monitoring.guardian_min_agent_age_seconds = 60
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

    fake_loop.run_in_executor.assert_any_call(
        None, dispatcher.agent_manager.tmux_server.has_session, "agent-tmux-1"
    )
    dispatcher.handle_missing_tmux_session.assert_called_once_with(agent)


@pytest.mark.asyncio
async def test_dead_pane_triggers_restart_even_though_session_exists(dispatcher):
    """Sessions are created with remain-on-exit on (launch_pipeline.py's
    _create_tmux_session) so a crashed agent leaves a dead pane behind
    instead of destroying the whole session -- that's what preserves
    capture-pane/pipe-pane evidence. But it also means has_session alone
    can no longer detect a dead agent: the session lives on forever with
    nothing left to do in it. is_pane_dead must be checked too, or a
    crashed agent just hangs forever uncaught instead of being restarted."""
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
    dispatcher.agent_manager.is_pane_dead = MagicMock(return_value=True)

    fake_loop = MagicMock()
    # has_session() -> True (session exists), is_pane_dead() -> True, then
    # the "is this task already done?" query -> mock_task (in_progress)
    fake_loop.run_in_executor = AsyncMock(side_effect=[True, True, mock_task])

    with patch("asyncio.get_event_loop", return_value=fake_loop):
        await dispatcher.guardian_analysis_for_agent(agent)

    assert fake_loop.run_in_executor.call_count == 3
    fake_loop.run_in_executor.assert_any_call(
        None, dispatcher.agent_manager.is_pane_dead, "agent-tmux-1"
    )
    dispatcher.handle_missing_tmux_session.assert_called_once_with(agent)


@pytest.mark.asyncio
async def test_live_pane_does_not_trigger_restart(dispatcher):
    agent = MagicMock(spec=Agent)
    agent.id = "agent-1"
    agent.agent_type = "phase"
    agent.current_task_id = "task-1"
    agent.tmux_session_name = "agent-tmux-1"
    agent.created_at = datetime.utcnow() - timedelta(hours=1)

    dispatcher.handle_missing_tmux_session = AsyncMock()

    fake_loop = MagicMock()
    # has_session() -> True, is_pane_dead() -> False
    fake_loop.run_in_executor = AsyncMock(side_effect=[True, False])

    with patch("asyncio.get_event_loop", return_value=fake_loop):
        # Bail out right after the liveness checks by making get_agent_output
        # return empty -- we only care that restart wasn't triggered.
        dispatcher.agent_manager.get_agent_output.return_value = ""
        await dispatcher.guardian_analysis_for_agent(agent)

    dispatcher.handle_missing_tmux_session.assert_not_called()
