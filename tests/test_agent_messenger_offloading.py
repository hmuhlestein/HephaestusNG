"""Regression: AgentMessenger.send_message_to_agent -- the single path
every message/broadcast/nudge to an agent goes through -- did blocking DB
session I/O and blocking tmux capture-pane/send-keys shell-outs directly
inside async def with no executor offload, unlike the sibling terminate_agent
fix in terminator.py, which explicitly offloads for exactly this reason."""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.agents.messenger import AgentMessenger
from src.core.database import Agent


@pytest.fixture
def messenger(db_manager):
    agent_manager = Mock()
    return AgentMessenger(db_manager, agent_manager)


def _seed_agent(db_manager, agent_id="agent-1", tmux_session_name="agent_agent-1"):
    session = db_manager.get_session()
    try:
        session.add(Agent(
            id=agent_id, system_prompt="x", status="working",
            cli_type="claude", tmux_session_name=tmux_session_name,
        ))
        session.commit()
    finally:
        session.close()


@pytest.mark.asyncio
async def test_send_message_to_agent_offloads_blocking_calls(messenger, db_manager, monkeypatch):
    _seed_agent(db_manager)

    pane = Mock()
    pane.cmd.return_value = Mock(stdout=["$ "])  # not wedged
    tmux_session = Mock(name="agent_agent-1")
    tmux_session.name = "agent_agent-1"
    tmux_session.attached_window.attached_pane = pane

    messenger._agent_manager.tmux_server.has_session = Mock(return_value=True)
    messenger._agent_manager.tmux_server.sessions = [tmux_session]

    fake_loop = MagicMock()

    async def run_now(_executor, func, *args):
        return func(*args)

    fake_loop.run_in_executor = AsyncMock(side_effect=run_now)
    monkeypatch.setattr("asyncio.get_event_loop", lambda: fake_loop)

    async def fast_sleep(_seconds):
        return None

    with patch("asyncio.sleep", new=fast_sleep), patch(
        "src.interfaces.get_cli_agent"
    ) as mock_get_cli:
        mock_cli_agent = Mock()
        mock_cli_agent.format_message = Mock(side_effect=lambda m: m)
        mock_get_cli.return_value = mock_cli_agent

        await messenger.send_message_to_agent("agent-1", "hello agent")

    # Blocking work actually happened (via the executor stub calling func(*args))
    assert fake_loop.run_in_executor.call_count >= 4
    pane.send_keys.assert_any_call('"hello agent"', enter=True)

    session = db_manager.get_session()
    try:
        agent = session.query(Agent).filter_by(id="agent-1").first()
        assert agent.last_activity is not None
    finally:
        session.close()


@pytest.mark.asyncio
async def test_send_message_to_agent_missing_agent_skips_tmux(messenger, db_manager, monkeypatch):
    fake_loop = MagicMock()

    async def run_now(_executor, func, *args):
        return func(*args)

    fake_loop.run_in_executor = AsyncMock(side_effect=run_now)
    monkeypatch.setattr("asyncio.get_event_loop", lambda: fake_loop)

    await messenger.send_message_to_agent("no-such-agent", "hello")

    messenger._agent_manager.tmux_server.has_session.assert_not_called()
