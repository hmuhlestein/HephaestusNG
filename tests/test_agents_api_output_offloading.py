"""Regression: GET /api/agents/{agent_id}/output called
AgentManager.get_agent_output synchronously inside async def -- it reads/
filters the whole transcript file (or shells out to tmux capture-pane),
documented up to ~4s for a large transcript, and this is a hot path the
dashboard polls repeatedly per active agent. Sibling routes in this same
file (get_agent_children, get_children_status, get_child_logs) already
offload identical-class work via asyncio.to_thread."""

from unittest.mock import MagicMock, patch

import pytest

from src.mcp.agents_api import get_agent_output


def _make_server_state(agent):
    state = MagicMock()
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = agent
    state.db_manager.get_session.return_value = session
    return state, session


@pytest.mark.asyncio
async def test_get_agent_output_offloads_to_thread():
    agent = MagicMock(tmux_session_name="agent_a1", status="working")
    state, _session = _make_server_state(agent)
    state.agent_manager.get_agent_output = MagicMock(return_value="some output")

    with patch("src.mcp.agents_api._get_server_state", return_value=state), patch(
        "asyncio.to_thread"
    ) as mock_to_thread:
        mock_to_thread.return_value = "some output"
        result = await get_agent_output("a1", lines=200, request=None)

    assert result == {"output": "some output"}
    mock_to_thread.assert_called_once()
    func_arg, agent_id_arg = mock_to_thread.call_args.args
    assert func_arg == state.agent_manager.get_agent_output
    assert agent_id_arg == "a1"
    assert mock_to_thread.call_args.kwargs == {"lines": 200}
