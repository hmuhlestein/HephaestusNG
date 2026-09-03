"""Regression test: terminate_agents_and_process_queue must not let one
agent's termination failure skip the rest of the batch or process_queue.

Found alongside the update_task_status "Connection closed" fix: this
function is now also scheduled via FastAPI's BackgroundTasks (Starlette
runs it strictly after the HTTP response has already been sent), so an
uncaught exception here would surface only as an ASGI-level traceback
well after the client's response went out -- nothing the caller could
react to. Worse, without per-agent isolation, one agent raising during
termination would abort the loop entirely, leaving any remaining agents
in the batch un-terminated and skipping process_queue outright -- a
stalled queue, silently.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.mcp.server.background_loops import terminate_agents_and_process_queue


@pytest.mark.asyncio
async def test_one_agents_termination_failure_does_not_skip_the_rest(monkeypatch):
    import src.mcp.server.background_loops as background_loops

    process_queue_mock = AsyncMock()
    monkeypatch.setattr(background_loops, "process_queue", process_queue_mock)

    agent_manager = MagicMock()
    terminated = []

    async def fake_terminate(agent_id):
        if agent_id == "agent-bad":
            raise RuntimeError("boom terminating agent-bad")
        terminated.append(agent_id)

    agent_manager.terminate_agent = AsyncMock(side_effect=fake_terminate)

    await terminate_agents_and_process_queue(
        agent_manager, ["agent-good-1", "agent-bad", "agent-good-2"], project_id="proj-1"
    )

    assert terminated == ["agent-good-1", "agent-good-2"], (
        "agent-bad's failure must not prevent the other agents in the "
        "same batch from being terminated"
    )
    process_queue_mock.assert_awaited_once_with("proj-1")
