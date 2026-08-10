"""Tests for src/mcp/server.py's _notify_and_pause_for_restart.

Regression context (docs/SAFE_RESTART_DESIGN.md): shutdown_event notified
in-flight agents of an imminent restart, then paused pipelines immediately
afterward with nothing waiting on the notification -- an agent mid tool
call (e.g. complete_my_task) could have that exact call still on the wire
when the backend actually stopped accepting connections, silently
dropping it. _notify_and_pause_for_restart now waits
SAFE_RESTART_GRACE_SECONDS after notifying before proceeding, but only
when something was actually notified.
"""

from unittest.mock import AsyncMock

import pytest

from src.mcp import server


class FakeService:
    def __init__(self, project_id):
        self.project_id = project_id
        self.pause_for_restart = AsyncMock()


class TestNotifyAndPauseForRestart:
    @pytest.mark.asyncio
    async def test_no_running_services_is_a_noop(self, monkeypatch):
        sleep_mock = AsyncMock()
        monkeypatch.setattr(server.asyncio, "sleep", sleep_mock)
        notify_mock = AsyncMock(return_value=0)
        monkeypatch.setattr(server, "_notify_agents_of_restart", notify_mock)

        await server._notify_and_pause_for_restart([])

        notify_mock.assert_not_called()
        sleep_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_grace_period_when_nothing_notified(self, monkeypatch):
        """The common case -- no agents actively working -- must not pay
        the grace delay on every restart."""
        sleep_mock = AsyncMock()
        monkeypatch.setattr(server.asyncio, "sleep", sleep_mock)
        monkeypatch.setattr(
            server, "_notify_agents_of_restart", AsyncMock(return_value=0)
        )
        svc = FakeService("proj-a")

        await server._notify_and_pause_for_restart([svc])

        sleep_mock.assert_not_called()
        svc.pause_for_restart.assert_called_once()

    @pytest.mark.asyncio
    async def test_waits_grace_period_before_pausing_when_agents_notified(
        self, monkeypatch
    ):
        sleep_mock = AsyncMock()
        monkeypatch.setattr(server.asyncio, "sleep", sleep_mock)
        monkeypatch.setattr(
            server, "_notify_agents_of_restart", AsyncMock(return_value=2)
        )
        svc = FakeService("proj-a")

        await server._notify_and_pause_for_restart([svc])

        sleep_mock.assert_called_once_with(server.SAFE_RESTART_GRACE_SECONDS)
        svc.pause_for_restart.assert_called_once()

    @pytest.mark.asyncio
    async def test_waits_only_once_for_multiple_projects(self, monkeypatch):
        sleep_mock = AsyncMock()
        monkeypatch.setattr(server.asyncio, "sleep", sleep_mock)
        monkeypatch.setattr(
            server, "_notify_agents_of_restart", AsyncMock(return_value=1)
        )
        services = [FakeService("proj-a"), FakeService("proj-b")]

        await server._notify_and_pause_for_restart(services)

        sleep_mock.assert_called_once_with(server.SAFE_RESTART_GRACE_SECONDS)
        for svc in services:
            svc.pause_for_restart.assert_called_once()

    @pytest.mark.asyncio
    async def test_pauses_every_service_even_if_one_notify_call_raises(
        self, monkeypatch
    ):
        """A DB error notifying one project's agents must not stop other
        projects from being paused for the restart."""
        sleep_mock = AsyncMock()
        monkeypatch.setattr(server.asyncio, "sleep", sleep_mock)

        async def flaky_notify(project_id):
            if project_id == "proj-bad":
                raise RuntimeError("db exploded")
            return 1

        monkeypatch.setattr(
            server, "_notify_agents_of_restart", AsyncMock(side_effect=flaky_notify)
        )
        svc_bad = FakeService("proj-bad")
        svc_good = FakeService("proj-good")

        await server._notify_and_pause_for_restart([svc_bad, svc_good])

        svc_bad.pause_for_restart.assert_called_once()
        svc_good.pause_for_restart.assert_called_once()
