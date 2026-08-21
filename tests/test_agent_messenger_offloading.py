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
        # Terminator's grace-period check reads this to avoid killing an
        # agent's tmux session right after a message was sent to it.
        assert agent.pending_message_sent_at is not None
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


def _wire_tmux(messenger, pane_cmd_side_effect):
    pane = Mock()
    pane.cmd.side_effect = pane_cmd_side_effect
    tmux_session = Mock(name="agent_agent-1")
    tmux_session.name = "agent_agent-1"
    tmux_session.attached_window.attached_pane = pane
    messenger._agent_manager.tmux_server.has_session = Mock(return_value=True)
    messenger._agent_manager.tmux_server.sessions = [tmux_session]
    return pane


def _fake_loop_and_sleep(monkeypatch):
    fake_loop = MagicMock()

    async def run_now(_executor, func, *args):
        return func(*args)

    fake_loop.run_in_executor = AsyncMock(side_effect=run_now)
    monkeypatch.setattr("asyncio.get_event_loop", lambda: fake_loop)

    async def fast_sleep(_seconds):
        return None

    return fake_loop, fast_sleep


class TestDeliveryConfirmation:
    """Regression: CLIAgentInterface.message_queued_confirmation_pattern
    and the retry loop in send_message_to_agent -- closes a live incident
    where a message sent while Claude Code was deep in a long tool-call
    wait left the typed text sitting inert (Enter never actually
    registered as submit), with no visible error and the caller reporting
    success regardless."""

    @pytest.mark.asyncio
    async def test_mocked_cli_agent_does_not_crash_delivery(
        self, messenger, db_manager, monkeypatch, caplog
    ):
        """The bug this test would have caught: a bare Mock() cli_agent's
        auto-attribute message_queued_confirmation_pattern() returns a
        truthy Mock, not a string -- re.search(Mock(), text) raises
        TypeError, which the outer except swallowed silently, skipping
        the last-activity update and the "Sent message" debug log
        entirely with no visible failure anywhere. Verified via a real
        log line reaching the end of the function, not just "last_activity
        is not None" (that column has its own default, so it stays
        non-None even if the function never got that far)."""
        import logging

        _seed_agent(db_manager)
        pane = _wire_tmux(messenger, lambda *a: Mock(stdout=["$ "]))
        fake_loop, fast_sleep = _fake_loop_and_sleep(monkeypatch)

        with patch("asyncio.sleep", new=fast_sleep), patch(
            "src.interfaces.get_cli_agent"
        ) as mock_get_cli:
            mock_cli_agent = Mock()  # message_queued_confirmation_pattern unconfigured
            mock_cli_agent.format_message = Mock(side_effect=lambda m: m)
            mock_get_cli.return_value = mock_cli_agent

            with caplog.at_level(logging.DEBUG, logger="src.agents.messenger"):
                await messenger.send_message_to_agent("agent-1", "hello agent")

        assert "Sent message to agent" in caplog.text
        assert pane.send_keys.call_count == 2  # message + one submit Enter, no retries

    @pytest.mark.asyncio
    async def test_retries_submit_until_confirmation_pattern_appears(
        self, messenger, db_manager, monkeypatch
    ):
        from src.interfaces.cli_interface import ClaudeCodeAgent

        _seed_agent(db_manager)

        calls = {"n": 0}

        def pane_cmd(*args):
            if args[:2] == ("capture-pane", "-p") and args[2:] == ("-S", "-20"):
                calls["n"] += 1
                if calls["n"] < 2:
                    return Mock(stdout=['❯ "hello agent"'])  # not yet confirmed
                return Mock(stdout=["❯ Press up to edit queued messages"])
            return Mock(stdout=["$ "])  # wedge check

        pane = _wire_tmux(messenger, pane_cmd)
        fake_loop, fast_sleep = _fake_loop_and_sleep(monkeypatch)

        with patch("asyncio.sleep", new=fast_sleep), patch(
            "src.interfaces.get_cli_agent", return_value=ClaudeCodeAgent()
        ):
            await messenger.send_message_to_agent("agent-1", "hello agent")

        # message + first submit Enter + exactly one retry Enter (confirmed
        # on the 2nd capture-pane check, so no 2nd retry needed)
        assert pane.send_keys.call_count == 3

    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries_without_crashing(
        self, messenger, db_manager, monkeypatch, caplog
    ):
        import logging

        from src.interfaces.cli_interface import ClaudeCodeAgent

        _seed_agent(db_manager)

        def pane_cmd(*args):
            if args[:2] == ("capture-pane", "-p") and args[2:] == ("-S", "-20"):
                # Text never leaves the pane and the confirmation hint
                # never appears -- the exact failure signature observed
                # live (agent 335b2a1d, first attempt).
                return Mock(stdout=['❯ "hello agent"'])
            return Mock(stdout=["$ "])

        pane = _wire_tmux(messenger, pane_cmd)
        fake_loop, fast_sleep = _fake_loop_and_sleep(monkeypatch)

        with patch("asyncio.sleep", new=fast_sleep), patch(
            "src.interfaces.get_cli_agent", return_value=ClaudeCodeAgent()
        ):
            with caplog.at_level(logging.DEBUG, logger="src.agents.messenger"):
                await messenger.send_message_to_agent("agent-1", "hello agent")

        assert "still doesn't look queued after 3 submit attempts" in caplog.text
        # Delivery is still reported/finalized normally -- this is a
        # best-effort warning, not a hard failure.
        assert "Sent message to agent" in caplog.text
        session = db_manager.get_session()
        try:
            agent = session.query(Agent).filter_by(id="agent-1").first()
            assert agent.last_activity is not None
        finally:
            session.close()
