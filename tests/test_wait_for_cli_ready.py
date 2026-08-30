"""Tests for LaunchPipeline._wait_for_cli_ready -- replaces the old flat
25s `asyncio.sleep` before delivering the initial prompt with active
detection of the CLI's own ready-for-input pattern
(cli_agent.get_health_check_pattern()), so a CLI that's ready sooner
doesn't sit idle for the rest of the fixed wait.
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.agents.manager import AgentManager
from src.core.database import DatabaseManager


@pytest.fixture
def launch_pipeline():
    db_manager = Mock(spec=DatabaseManager)
    llm_provider = Mock()
    agent_manager = AgentManager(db_manager=db_manager, llm_provider=llm_provider)
    return agent_manager._launch


def _mock_pane(captured_lines):
    pane = Mock()
    pane.cmd = Mock(return_value=MagicMock(stdout=captured_lines))
    return pane


@pytest.mark.asyncio
async def test_returns_as_soon_as_ready_pattern_matches(launch_pipeline):
    cli_agent = Mock()
    cli_agent.get_health_check_pattern = Mock(return_value=r"(Assistant:|Human:|›)")
    pane = _mock_pane(["some startup noise", "› "])

    result = await launch_pipeline._wait_for_cli_ready(
        pane, cli_agent, "claude", "agent-1", floor=0.01, timeout=2.0, poll_interval=0.01
    )

    pane.cmd.assert_called_with("capture-pane", "-p", "-S", "-10")
    assert result is True


@pytest.mark.asyncio
async def test_does_not_poll_before_the_floor_elapses(launch_pipeline):
    cli_agent = Mock()
    cli_agent.get_health_check_pattern = Mock(return_value=r"›")
    pane = _mock_pane(["› "])

    with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await launch_pipeline._wait_for_cli_ready(
            pane, cli_agent, "claude", "agent-1", floor=3.0, timeout=25.0, poll_interval=0.5
        )

    # First sleep call must be the mandatory floor, before any capture-pane poll.
    first_sleep_call = mock_sleep.call_args_list[0]
    assert first_sleep_call.args[0] == 3.0


@pytest.mark.asyncio
async def test_falls_back_after_timeout_when_pattern_never_appears(launch_pipeline):
    cli_agent = Mock()
    cli_agent.get_health_check_pattern = Mock(return_value=r"NEVER_MATCHES_ANYTHING")
    pane = _mock_pane(["still loading..."])

    # Should return (not hang) once the timeout elapses, even with no match.
    result = await launch_pipeline._wait_for_cli_ready(
        pane, cli_agent, "claude", "agent-1", floor=0.01, timeout=0.1, poll_interval=0.02
    )

    assert pane.cmd.call_count >= 1
    assert result is False


@pytest.mark.asyncio
async def test_ready_detection_is_faster_than_the_old_flat_wait(launch_pipeline):
    """The whole point of this change: a CLI whose ready pattern appears
    immediately after the floor must not pay anywhere near the old fixed
    25s wait."""
    import time

    cli_agent = Mock()
    cli_agent.get_health_check_pattern = Mock(return_value=r"›")
    pane = _mock_pane(["› "])

    start = time.monotonic()
    await launch_pipeline._wait_for_cli_ready(
        pane, cli_agent, "claude", "agent-1", floor=0.05, timeout=25.0, poll_interval=0.05
    )
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"expected near-immediate return once ready, took {elapsed:.2f}s"
