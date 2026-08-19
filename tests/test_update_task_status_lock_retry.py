"""Regression test: update_task_status must retry on SQLite lock
contention rather than losing the agent's task-completion report to a
500.

Found live 2026-08-19, restarting the self-hosted backend: with 2-3
agents completing tasks close together, journal_mode=WAL and
busy_timeout=30000 (already configured) weren't enough on their own --
`Failed to update task status: (sqlite3.OperationalError) database is
locked` surfaced from the handler's own long chain of committed writes
(record_learnings, self-review gate, hard-floor checks, ticket creation,
task completion, spec-gate firing).

update_task_status is split into an inner `_update_task_status_once`
(the original handler body, unchanged) and an outer retry wrapper. The
retry is on the WHOLE handler, not a single failing commit, because once
session.commit() raises OperationalError, SQLAlchemy may already have
rolled back the pending change -- retrying commit() alone can silently
no-op instead of re-persisting anything. A full-handler retry is safe
here specifically because the handler's own terminal-state idempotency
guard (task.status in TaskStatus.TERMINAL) already makes a second pass a
no-op past whatever the first pass actually committed.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from src.mcp.server.agent_task_routes import (
    _LOCK_RETRY_ATTEMPTS,
    UpdateTaskStatusRequest,
    update_task_status,
)


def _lock_error():
    return OperationalError("UPDATE tasks ...", {}, Exception("database is locked"))


@pytest.mark.asyncio
async def test_retries_and_recovers_from_transient_lock_contention():
    real_response = object()
    impl = AsyncMock(side_effect=[_lock_error(), _lock_error(), real_response])

    with (
        patch("src.mcp.server.agent_task_routes._update_task_status_once", impl),
        patch("src.mcp.server.agent_task_routes.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await update_task_status(
            UpdateTaskStatusRequest(task_id="t1", status="done", summary="done"),
            agent_id="agent-1",
        )

    assert result is real_response
    assert impl.call_count == 3
    assert mock_sleep.call_count == 2  # one sleep between each retry


@pytest.mark.asyncio
async def test_gives_up_after_exhausting_all_attempts():
    impl = AsyncMock(side_effect=_lock_error())

    with (
        patch("src.mcp.server.agent_task_routes._update_task_status_once", impl),
        patch("src.mcp.server.agent_task_routes.asyncio.sleep", new_callable=AsyncMock),
    ):
        with pytest.raises(OperationalError):
            await update_task_status(
                UpdateTaskStatusRequest(task_id="t2", status="done", summary="done"),
                agent_id="agent-1",
            )

    assert impl.call_count == _LOCK_RETRY_ATTEMPTS


@pytest.mark.asyncio
async def test_does_not_retry_a_non_lock_operational_error():
    """A different SQLite error (e.g. a real schema/constraint problem)
    must fail immediately -- retrying it 5 times would just waste the
    caller's time on something retrying can never fix."""
    other_error = OperationalError("...", {}, Exception("no such table: bogus"))
    impl = AsyncMock(side_effect=other_error)

    with (
        patch("src.mcp.server.agent_task_routes._update_task_status_once", impl),
        patch("src.mcp.server.agent_task_routes.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        with pytest.raises(OperationalError):
            await update_task_status(
                UpdateTaskStatusRequest(task_id="t3", status="done", summary="done"),
                agent_id="agent-1",
            )

    assert impl.call_count == 1
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_does_not_retry_an_http_exception():
    """A normal validation/business-logic rejection (already an
    HTTPException, e.g. the 400 for a missing summary) must pass straight
    through, not get treated as retryable lock contention."""
    from fastapi import HTTPException

    impl = AsyncMock(side_effect=HTTPException(status_code=400, detail="summary required"))

    with (
        patch("src.mcp.server.agent_task_routes._update_task_status_once", impl),
        patch("src.mcp.server.agent_task_routes.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await update_task_status(
                UpdateTaskStatusRequest(task_id="t4", status="done", summary=""),
                agent_id="agent-1",
            )

    assert exc_info.value.status_code == 400
    assert impl.call_count == 1
    mock_sleep.assert_not_called()
