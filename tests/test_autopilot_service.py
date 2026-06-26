"""Tests for autopilot/service.py — AutopilotService lifecycle."""

import pytest
import asyncio
from unittest.mock import patch, AsyncMock, Mock
from pathlib import Path


class TestAutopilotService:
    @pytest.fixture
    def service(self):
        from src.autopilot.service import AutopilotService
        return AutopilotService()

    def test_initial_state(self, service):
        assert service.running is False
        assert service._project_path is None
        assert service._designs_processed == 0

    def test_status_not_running(self, service):
        status = service.status()
        assert status["running"] is False
        assert status["designs_processed"] == 0
        assert status["error"] is None

    @pytest.mark.asyncio
    async def test_start_validates_project_path(self, service):
        with pytest.raises(ValueError, match="does not exist"):
            await service.start("/nonexistent/path")

    @pytest.mark.asyncio
    async def test_start_validates_git_repo(self, service, tmp_path):
        (tmp_path / ".git").rmdir() if (tmp_path / ".git").exists() else None
        # Create a non-git directory
        project = tmp_path / "not_a_repo"
        project.mkdir()
        with pytest.raises(ValueError, match="not a git repository"):
            await service.start(str(project))

    @pytest.mark.asyncio
    async def test_start_rejects_duplicate(self, service, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / "docs" / "design-queue").mkdir(parents=True)

        with patch.object(service, "_run_pipeline", new_callable=AsyncMock):
            await service.start(str(project))
            assert service.running is True

            with pytest.raises(RuntimeError, match="already running"):
                await service.start(str(project))

            await service.stop()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self, service):
        result = await service.stop()
        assert result["stopped"] is True
        assert "not running" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_start_sets_state(self, service, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / "docs" / "design-queue").mkdir(parents=True)

        with patch.object(service, "_run_pipeline", new_callable=AsyncMock):
            await service.start(str(project), max_iterations=5)

            assert service.running is True
            assert service._project_path == str(project)
            assert service._max_iterations == 5
            assert service._designs_processed == 0

            await service.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_running(self, service, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / "docs" / "design-queue").mkdir(parents=True)

        with patch.object(service, "_run_pipeline", new_callable=AsyncMock):
            await service.start(str(project))
            result = await service.stop()
            assert result["stopped"] is True
            assert service.running is False

    def test_status_while_running(self, service):
        service._running = True
        service._start_time = 1.0  # some past time
        service._designs_processed = 3
        status = service.status()
        assert status["designs_processed"] == 3
        assert "elapsed_seconds" in status


class TestGetAutopilotService:
    def test_singleton(self):
        from src.autopilot.service import get_autopilot_service
        s1 = get_autopilot_service()
        s2 = get_autopilot_service()
        assert s1 is s2
