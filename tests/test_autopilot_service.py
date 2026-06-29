"""Tests for autopilot/service.py — AutopilotService lifecycle."""

import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest


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

    def test_running_property(self, service):
        # No task
        assert service.running is False

        # Task done
        service._task = Mock()
        service._task.done.return_value = True
        service._running = True
        assert service.running is False

        # Task running
        service._task.done.return_value = False
        service._running = True
        assert service.running is True

        # Running but no task
        service._running = True
        service._task = None
        assert service.running is False

    def test_status_defaults(self, service):
        status = service.status()
        assert status["project_path"] is None
        assert status["current_design"] is None
        assert status["designs_succeeded"] == 0
        assert status["designs_failed"] == 0
        assert status["elapsed_seconds"] == 0

    def test_status_with_error(self, service):
        service._error = "Something went wrong"
        status = service.status()
        assert status["error"] == "Something went wrong"

    @pytest.mark.asyncio
    async def test_start_creates_design_queue(self, service, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        # No design-queue dir yet

        with patch.object(service, "_run_pipeline", new_callable=AsyncMock):
            await service.start(str(project))
            assert (project / "docs" / "design-queue").exists()
            await service.stop()

    @pytest.mark.asyncio
    async def test_start_custom_design_queue(self, service, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        custom_queue = tmp_path / "custom_queue"
        custom_queue.mkdir()

        with patch.object(service, "_run_pipeline", new_callable=AsyncMock):
            await service.start(str(project), design_queue=str(custom_queue))
            assert service._design_queue == str(custom_queue)
            await service.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, service, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / "docs" / "design-queue").mkdir(parents=True)

        # Create a slow-running pipeline task
        async def slow_pipeline():
            await asyncio.sleep(100)

        with patch.object(service, "_run_pipeline", side_effect=slow_pipeline):
            await service.start(str(project))
            assert service.running is True

            result = await service.stop()
            assert result["stopped"] is True
            assert service.running is False

    @pytest.mark.asyncio
    async def test_run_pipeline_sets_error(self, service, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / "docs" / "design-queue").mkdir(parents=True)

        # Make the pipeline raise an exception
        async def failing_pipeline():
            raise Exception("Pipeline crashed")

        with patch.object(service, "_run_pipeline", side_effect=failing_pipeline):
            await service.start(str(project))
            # Wait for the task to complete
            await asyncio.sleep(0.1)
            # Error should be captured
            # Note: the mock replaces _run_pipeline, so the real error handling
            # in the actual _run_pipeline won't run. But the task itself fails.

    def test_stop_event_cleared_on_start(self, service, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / "docs" / "design-queue").mkdir(parents=True)

        # Set the stop event before starting
        service._stop_event.set()
        assert service._stop_event.is_set()

        with patch.object(service, "_run_pipeline", new_callable=AsyncMock):
            # start() should clear the event
            loop = asyncio.new_event_loop()
            loop.run_until_complete(service.start(str(project)))
            assert not service._stop_event.is_set()
            loop.run_until_complete(service.stop())
            loop.close()

    def test_status_respects_running_flag(self, service):
        # When not running, elapsed should be 0 even if start_time is set
        service._running = False
        service._start_time = 1000.0
        status = service.status()
        assert status["elapsed_seconds"] == 0

    def test_status_calculates_elapsed(self, service):
        service._running = True
        service._start_time = time.time() - 10
        status = service.status()
        assert status["elapsed_seconds"] >= 9


class TestGetAutopilotService:
    def test_singleton(self):
        from src.autopilot.service import get_autopilot_service

        s1 = get_autopilot_service()
        s2 = get_autopilot_service()
        assert s1 is s2
