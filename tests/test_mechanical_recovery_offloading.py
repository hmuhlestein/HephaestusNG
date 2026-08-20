"""Regression: mechanical_recovery_for_agent's stuck-detection pane read did
a blocking DB session query plus a blocking tmux capture-pane call directly
inline inside async def -- stalling the event loop on every monitoring
cycle, same class of issue fixed elsewhere in this codebase. Must be
offloaded via run_in_executor."""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from src.core.database import Agent
from src.monitoring.mechanical_recovery import MechanicalRecoveryDetector


@pytest.fixture
def detector():
    db_manager = Mock()
    agent_manager = Mock()
    agent_manager.tmux_server = Mock()
    agent_manager.get_agent_output = Mock(return_value="Agent working on task...")
    config = Mock()
    auto_restart = Mock()
    return MechanicalRecoveryDetector(db_manager, agent_manager, config, auto_restart)


@pytest.mark.asyncio
async def test_stuck_check_pane_read_is_offloaded(detector, monkeypatch):
    agent = Agent(id="a1", cli_type="claude")

    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value="")
    monkeypatch.setattr("asyncio.get_event_loop", lambda: fake_loop)

    await detector.mechanical_recovery_for_agent(agent)

    fake_loop.run_in_executor.assert_called_once()
    executor_arg, func_arg, agent_id_arg = fake_loop.run_in_executor.call_args.args
    assert executor_arg is None
    assert func_arg == detector._capture_stuck_check_pane
    assert agent_id_arg == "a1"


def test_capture_stuck_check_pane_falls_back_when_agent_row_missing(detector):
    """The sync helper itself must not raise when the DB row or tmux
    session can't be found -- mirrors the original inline try/except's
    silent-fallback behavior."""
    session = Mock()
    session.query.return_value.filter_by.return_value.first.return_value = None
    detector.db_manager.get_session = Mock(return_value=session)

    result = detector._capture_stuck_check_pane("missing-agent")

    assert result == ""
    session.close.assert_called_once()
