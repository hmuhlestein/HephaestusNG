"""Regression: AgentManager.send_recovery_keystrokes -- the stuck-TUI
recovery path guardian/mechanical_recovery call -- did a blocking DB query
and blocking tmux send_keys calls directly inside async def with no
executor offload, stalling the event loop specifically at the moment an
agent is already wedged and other monitoring work is trying to run."""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.agents.manager import AgentManager
from src.core.database import Agent, DatabaseManager


@pytest.fixture
def db_manager(tmp_path):
    manager = DatabaseManager(str(tmp_path / "test.db"))
    manager.create_tables()
    return manager


@pytest.fixture
def agent_manager(db_manager):
    return AgentManager(db_manager, Mock(), tmux_server=Mock())


def _seed_agent(db_manager, agent_id="agent-1", tmux_session_name="agent_agent-1", cli_type="claude"):
    session = db_manager.get_session()
    try:
        session.add(Agent(
            id=agent_id, system_prompt="x", status="working",
            cli_type=cli_type, tmux_session_name=tmux_session_name,
        ))
        session.commit()
    finally:
        session.close()


@pytest.mark.asyncio
async def test_send_recovery_keystrokes_offloads_blocking_calls(
    agent_manager, db_manager, monkeypatch
):
    _seed_agent(db_manager)

    pane = Mock()
    tmux_session = Mock()
    tmux_session.name = "agent_agent-1"
    tmux_session.attached_window.attached_pane = pane

    agent_manager.tmux_server.has_session = Mock(return_value=True)
    agent_manager.tmux_server.sessions = [tmux_session]

    fake_loop = MagicMock()

    async def run_now(_executor, func, *args):
        return func(*args)

    fake_loop.run_in_executor = AsyncMock(side_effect=run_now)
    monkeypatch.setattr("asyncio.get_event_loop", lambda: fake_loop)

    async def fast_sleep(_seconds):
        return None

    with patch("asyncio.sleep", new=fast_sleep), patch(
        "src.agents.manager.get_cli_agent"
    ) as mock_get_cli:
        mock_cli_agent = Mock()
        mock_cli_agent.recovery_keystrokes = Mock(return_value=["Escape"])
        mock_get_cli.return_value = mock_cli_agent

        result = await agent_manager.send_recovery_keystrokes("agent-1")

    assert result is True
    assert fake_loop.run_in_executor.call_count >= 3
    pane.send_keys.assert_any_call("Escape", enter=False, literal=False)


@pytest.mark.asyncio
async def test_send_raw_key_sends_literal_key_to_pane(agent_manager, db_manager):
    """The tmux viewer's Esc button -- one literal key, no text typed and
    no Enter follows, unlike send_message_to_agent."""
    _seed_agent(db_manager)

    pane = Mock()
    tmux_session = Mock()
    tmux_session.name = "agent_agent-1"
    tmux_session.attached_window.attached_pane = pane

    agent_manager.tmux_server.has_session = Mock(return_value=True)
    agent_manager.tmux_server.sessions = [tmux_session]

    result = await agent_manager.send_raw_key("agent-1", "Escape")

    assert result is True
    pane.send_keys.assert_called_once_with("Escape", enter=False, literal=False)


@pytest.mark.asyncio
async def test_send_raw_key_returns_false_when_agent_has_no_tmux_session(
    agent_manager, db_manager
):
    session = db_manager.get_session()
    try:
        session.add(Agent(
            id="agent-2", system_prompt="x", status="idle",
            cli_type="claude", tmux_session_name=None,
        ))
        session.commit()
    finally:
        session.close()

    result = await agent_manager.send_raw_key("agent-2", "Escape")

    assert result is False


@pytest.mark.asyncio
async def test_send_raw_key_returns_false_when_tmux_session_is_gone(
    agent_manager, db_manager
):
    _seed_agent(db_manager)
    agent_manager.tmux_server.has_session = Mock(return_value=False)
    agent_manager.tmux_server.sessions = []

    result = await agent_manager.send_raw_key("agent-1", "Escape")

    assert result is False
