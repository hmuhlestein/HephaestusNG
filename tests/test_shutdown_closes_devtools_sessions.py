"""Regression: DevToolsManager.close_all() was implemented but never
called anywhere -- CDP browser sessions opened by the devtools MCP tools
were never cleaned up on server shutdown."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.mcp.server import lifecycle as server


@pytest.mark.asyncio
async def test_shutdown_event_closes_devtools_sessions(monkeypatch):
    # Make every other shutdown_event step a no-op so this test isolates
    # just the devtools cleanup.
    monkeypatch.setattr(server.server_state, "shutdown_event", MagicMock())
    monkeypatch.setattr(server.server_state, "background_queue_processor_task", None)
    monkeypatch.setattr(server.server_state, "phase_advancement_sweep_task", None)
    monkeypatch.setattr(server.server_state, "active_websockets", [])

    mock_registry = MagicMock()
    mock_registry.running.return_value = []
    monkeypatch.setattr(
        "src.autopilot.service.get_registry", lambda: mock_registry
    )

    mock_devtools_manager = MagicMock()
    mock_devtools_manager.close_all = AsyncMock()
    monkeypatch.setattr(
        "src.mcp.devtools.devtools_manager", mock_devtools_manager
    )

    await server.shutdown_event()

    mock_devtools_manager.close_all.assert_awaited_once()
