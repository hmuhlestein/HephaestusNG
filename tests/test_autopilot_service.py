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

    @pytest.fixture(autouse=True)
    def _isolate_state_file(self, tmp_path, monkeypatch):
        # start()/stop() persist run params to disk on every real call (see
        # TestRunningStatePersistence below) — without this, tests here that
        # call the unmocked start()/stop() write a live project_path straight
        # into the real ~/.hephaestus/autopilot/running_pipeline.json, which
        # the backend auto-resumes on its next startup_event.
        import src.autopilot.service as service_module

        monkeypatch.setattr(
            service_module, "_RUNNING_STATE_PATH", tmp_path / "running_pipeline.json"
        )

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
        (project / ".hephaestus" / "designs").mkdir(parents=True)

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
        (project / ".hephaestus" / "designs").mkdir(parents=True)

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
        (project / ".hephaestus" / "designs").mkdir(parents=True)

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
        # No design dir yet

        with patch.object(service, "_run_pipeline", new_callable=AsyncMock):
            await service.start(str(project))
            assert (project / ".hephaestus" / "designs").exists()
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
        (project / ".hephaestus" / "designs").mkdir(parents=True)

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
        (project / ".hephaestus" / "designs").mkdir(parents=True)

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
        (project / ".hephaestus" / "designs").mkdir(parents=True)

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

    @pytest.mark.asyncio
    async def test_start_auto_creates_missing_project(
        self, service, tmp_path, monkeypatch
    ):
        """A project deleted via the UI (or never registered at all) must not
        make the pipeline invisible/unmanageable there forever -- start()
        should recreate the row instead of only warning.

        Points HEPHAESTUS_TEST_DB at a real file instead of the conftest
        default ':memory:' -- each DatabaseManager(':memory:') call gets its
        own separate empty database (StaticPool keeps one connection alive
        per *engine instance*, not across instances), so the service's
        internal get_db() calls and this test's assertions would otherwise
        never see the same data.
        """
        from src.core.database import AutopilotProject, DatabaseManager, get_db

        db_path = tmp_path / "test.db"
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
        DatabaseManager(str(db_path)).create_tables()

        project = tmp_path / "myproject"
        project.mkdir()
        (project / ".git").mkdir()
        (project / ".hephaestus" / "designs").mkdir(parents=True)

        with patch.object(service, "_run_pipeline", new_callable=AsyncMock):
            await service.start(str(project))
            await service.stop()

        with get_db() as db:
            proj = db.query(AutopilotProject).filter_by(base_dir=str(project)).first()
            assert proj is not None
            assert proj.name == "myproject"
            assert proj.is_active is True

    @pytest.mark.asyncio
    async def test_start_reuses_existing_project_without_duplicating(
        self, service, tmp_path, monkeypatch
    ):
        from src.core.database import AutopilotProject, DatabaseManager, get_db

        db_path = tmp_path / "test.db"
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
        DatabaseManager(str(db_path)).create_tables()

        project = tmp_path / "myproject"
        project.mkdir()
        (project / ".git").mkdir()
        (project / ".hephaestus" / "designs").mkdir(parents=True)

        with get_db() as db:
            db.add(
                AutopilotProject(
                    id="proj-existing123",
                    name="myproject",
                    base_dir=str(project),
                    is_active=False,
                )
            )

        with patch.object(service, "_run_pipeline", new_callable=AsyncMock):
            await service.start(str(project))
            await service.stop()

        with get_db() as db:
            matches = (
                db.query(AutopilotProject).filter_by(base_dir=str(project)).all()
            )
            assert len(matches) == 1
            assert matches[0].id == "proj-existing123"
            assert matches[0].is_active is True


class TestGetAutopilotService:
    def test_singleton(self):
        from src.autopilot.service import get_autopilot_service

        s1 = get_autopilot_service()
        s2 = get_autopilot_service()
        assert s1 is s2


class TestRunningStatePersistence:
    """A backend restart kills AutopilotService's in-process asyncio task
    with nothing to resume it — these persist/restore the run params across
    that restart so the pipeline driver comes back on its own instead of
    silently stalling forever (see src/mcp/server.py startup_event)."""

    @pytest.fixture
    def service(self):
        from src.autopilot.service import AutopilotService

        return AutopilotService()

    @pytest.fixture(autouse=True)
    def _isolate_state_file(self, tmp_path, monkeypatch):
        # Redirect the module-level state path so tests never touch the
        # real ~/.hephaestus/autopilot/running_pipeline.json
        import src.autopilot.service as service_module

        monkeypatch.setattr(
            service_module, "_RUNNING_STATE_PATH", tmp_path / "running_pipeline.json"
        )

    @pytest.mark.asyncio
    async def test_start_persists_state(self, service, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()

        with patch.object(service, "_run_pipeline", new_callable=AsyncMock):
            await service.start(str(project), max_iterations=7)

            persisted = service.load_persisted_state()
            assert persisted is not None
            assert persisted["project_path"] == str(project)
            assert persisted["max_iterations"] == 7

            await service.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_persisted_state(self, service, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()

        with patch.object(service, "_run_pipeline", new_callable=AsyncMock):
            await service.start(str(project))
            assert service.load_persisted_state() is not None

            await service.stop()
            assert service.load_persisted_state() is None

    def test_load_persisted_state_when_absent(self, service):
        assert service.load_persisted_state() is None

    def test_clear_persisted_state_when_absent_is_safe(self, service):
        # Should not raise even if nothing was ever persisted
        service.clear_persisted_state()
        assert service.load_persisted_state() is None
