"""Regression tests for src/mcp/frontend/_shared.py's FrontendAPI: blocking
calls made directly inside async def routes must be offloaded."""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.core.database import DatabaseManager, Phase, Workflow
from src.mcp.frontend._shared import FrontendAPI


@pytest.fixture
def frontend_api():
    return FrontendAPI(db_manager=Mock(), agent_manager=Mock())


@pytest.fixture
def phase_db_manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    manager = DatabaseManager(str(db_path))
    manager.create_tables()
    session = manager.get_session()
    try:
        session.add(Workflow(
            id="wf-1", name="Test Workflow",
            phases_folder_path="/test/phases", status="active",
        ))
        session.add(Phase(
            id="phase-1", workflow_id="wf-1", order=1, name="phase1",
            description="x", done_definitions=[],
        ))
        session.commit()
    finally:
        session.close()
    return manager


@pytest.mark.asyncio
async def test_sync_blocking_status_offloads_sync_task_blocking_status(
    frontend_api, monkeypatch
):
    """sync_blocking_status called TaskBlockingService.sync_task_blocking_status
    directly inside async def -- that method does N+1 blocking DB round
    trips (one query for all tasks, then a get_db() session per task),
    stalling the event loop for the full duration of the sync."""
    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value={"success": True})
    monkeypatch.setattr("asyncio.get_event_loop", lambda: fake_loop)

    with patch(
        "src.services.task_blocking_service.TaskBlockingService.sync_task_blocking_status"
    ) as mock_sync:
        result = await frontend_api.sync_blocking_status()

    fake_loop.run_in_executor.assert_called_once()
    executor_arg, func_arg = fake_loop.run_in_executor.call_args.args[:2]
    assert executor_arg is None
    assert func_arg == mock_sync
    assert result == {"success": True}


@pytest.mark.asyncio
async def test_create_phase_prompt_version_retry_uses_asyncio_sleep(
    phase_db_manager,
):
    """create_phase_prompt_version's IntegrityError retry loop called
    time.sleep instead of asyncio.sleep -- a real, blocking sleep inside
    async def that stalls the event loop on every retry, unlike every other
    retry/backoff path in this file which already uses asyncio.sleep."""
    from sqlalchemy.exc import IntegrityError

    api = FrontendAPI(db_manager=phase_db_manager, agent_manager=Mock())

    real_commit = __import__("sqlalchemy").orm.Session.commit
    calls = {"n": 0}

    def flaky_commit(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("stmt", {}, Exception("dup version"))
        return real_commit(self)

    with patch(
        "sqlalchemy.orm.Session.commit", new=flaky_commit
    ), patch("asyncio.sleep", new=AsyncMock()) as mock_asyncio_sleep, patch(
        "time.sleep"
    ) as mock_time_sleep:
        result = await api.create_phase_prompt_version(
            "phase-1", {"description": "updated"}
        )

    assert result["success"] is True
    mock_asyncio_sleep.assert_called()
    mock_time_sleep.assert_not_called()
