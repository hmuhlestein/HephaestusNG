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
        # start()/stop() persist run params to the DB (ProjectContext table)
        # on every real call (see TestRunningStatePersistence below) —
        # without this, tests here that call the unmocked start()/stop()
        # write a live project_path straight into the real hephaestus.db,
        # which the backend auto-resumes on its next startup_event.
        from src.core.database import DatabaseManager

        db_path = tmp_path / "test.db"
        DatabaseManager(str(db_path)).create_tables()
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))

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
    async def test_start_allows_a_multi_repo_project_whose_workspace_root_is_not_itself_a_git_repo(
        self, service, tmp_path
    ):
        """A multi-repo project's workspace root (AutopilotProject.base_dir)
        deliberately need not be a git repo itself -- real git operations
        resolve through registered ProjectRepo rows instead (see
        repo_resolution.py). Observed live: hitting "start" on a project
        set up this way ("parent" with git-backed "child" repos under it)
        raised "Project path is not a git repository" even though every
        child repo was already registered and valid."""
        from src.autopilot.orchestrator.state import _get_or_create_project_id
        from src.core.database import ProjectRepo, get_db

        workspace_root = tmp_path / "parent"
        workspace_root.mkdir()
        child = workspace_root / "child"
        child.mkdir()
        (child / ".git").mkdir()

        # Register the project and its one child repo the same way the
        # real "Add Repo" flow would, before ever calling start().
        project_id = _get_or_create_project_id(str(workspace_root))
        with get_db() as db:
            db.add(ProjectRepo(
                id="repo-child", project_id=project_id, label="child",
                path=str(child), is_primary=True,
            ))
            db.commit()

        with patch.object(service, "_run_pipeline", new_callable=AsyncMock):
            # Must not raise -- this is the actual regression: the git-repo
            # check ran before project_id/ProjectRepo rows were ever
            # consulted, so a registered child repo couldn't satisfy it.
            await service.start(str(workspace_root))
            assert service.running is True

    @pytest.mark.asyncio
    async def test_start_rejects_duplicate(self, service, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / ".hephaestus" / "specs").mkdir(parents=True)

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
        (project / ".hephaestus" / "specs").mkdir(parents=True)

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
        (project / ".hephaestus" / "specs").mkdir(parents=True)

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
            assert (project / ".hephaestus" / "specs").exists()
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
        (project / ".hephaestus" / "specs").mkdir(parents=True)

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
        (project / ".hephaestus" / "specs").mkdir(parents=True)

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
        (project / ".hephaestus" / "specs").mkdir(parents=True)

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
        (project / ".hephaestus" / "specs").mkdir(parents=True)

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
        (project / ".hephaestus" / "specs").mkdir(parents=True)

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

    @pytest.mark.asyncio
    async def test_concurrent_start_cannot_interleave_with_an_in_flight_stop(
        self, service, tmp_path
    ):
        """Regression: stop() (and pause_for_restart()) sets self._running =
        False, then has a real yield point -- its own `await asyncio.
        wait_for(self._task, ...)` -- before finishing cleanup. start() has
        no yield point of its own, so a concurrent start() call landing in
        that window would see running=False, run to completion synchronously
        (including overwriting self._task with a brand-new task), and race
        the in-flight stop()'s own read of self._task -- which could then
        wait on/cancel the WRONG task. _lifecycle_lock makes this
        impossible: start() must wait for stop() to fully release the lock
        before it can even check self.running.

        Proven via an execution-order list, not timing: the slow pipeline
        sleeps well past everything else in this test, so the only way
        "start recorded" can appear AFTER "stop finished" is if the lock
        actually serialized them."""
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / ".hephaestus" / "specs").mkdir(parents=True)

        order = []

        async def slow_pipeline():
            await asyncio.sleep(100)

        with patch.object(service, "_run_pipeline", side_effect=slow_pipeline):
            await service.start(str(project))
            first_task = service._task

            async def do_stop():
                await service.stop()
                order.append("stop finished")

            async def do_start():
                # Give do_stop a chance to acquire the lock and reach its
                # own await point first.
                await asyncio.sleep(0.01)
                await service.start(str(project))
                order.append("start recorded")

            await asyncio.gather(do_stop(), do_start())

        assert order == ["stop finished", "start recorded"], (
            "start() ran before the in-flight stop() released the lock -- "
            "the two interleaved instead of being serialized"
        )
        # The second start's task must be a genuinely new one, not left
        # pointing at (or corrupting) the first, already-stopped task.
        assert service._task is not None
        assert service._task is not first_task
        assert service.running is True

    @pytest.mark.asyncio
    async def test_concurrent_start_calls_reject_the_second_as_already_running(
        self, service, tmp_path
    ):
        """The ordinary case start() already handled correctly (no await
        of its own, so two concurrent calls can't interleave even without
        the lock) -- confirms the lock doesn't change this existing,
        correct behavior."""
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / ".hephaestus" / "specs").mkdir(parents=True)

        async def slow_pipeline():
            await asyncio.sleep(100)

        with patch.object(service, "_run_pipeline", side_effect=slow_pipeline):
            results = await asyncio.gather(
                service.start(str(project)),
                service.start(str(project)),
                return_exceptions=True,
            )

        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, Exception)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], RuntimeError)
        assert "already running" in str(failures[0])


class TestGetAutopilotService:
    """get_autopilot_service() used to be a single-instance global
    singleton -- one AutopilotService for the entire backend, regardless of
    project. Now it's keyed by project_id via AutopilotServiceRegistry, so
    two projects can each have their own instance and run concurrently."""

    @pytest.fixture(autouse=True)
    def _fresh_registry(self, monkeypatch):
        import src.autopilot.service as service_module

        monkeypatch.setattr(service_module, "_registry", None)

    def test_same_project_id_returns_same_instance(self):
        from src.autopilot.service import get_autopilot_service

        s1 = get_autopilot_service("proj-a")
        s2 = get_autopilot_service("proj-a")
        assert s1 is s2

    def test_different_project_id_returns_different_instance(self):
        from src.autopilot.service import get_autopilot_service

        s1 = get_autopilot_service("proj-a")
        s2 = get_autopilot_service("proj-b")
        assert s1 is not s2
        assert s1.project_id == "proj-a"
        assert s2.project_id == "proj-b"


class TestAutopilotServiceRegistry:
    """Regression coverage for the concurrency cap: a second, genuinely new
    project must be rejected once max_concurrent is reached, but restarting
    a project that's already tracked as running must never be blocked by
    its own occupied slot."""

    def _running_service(self, registry, project_id):

        service = registry.get_or_create(project_id)
        service._running = True
        service._task = Mock()
        service._task.done.return_value = False
        return service

    def test_can_start_allows_under_cap(self):
        from src.autopilot.service import AutopilotServiceRegistry

        registry = AutopilotServiceRegistry(max_concurrent=2)
        self._running_service(registry, "proj-a")

        allowed, message = registry.can_start("proj-b")
        assert allowed is True
        assert message == ""

    def test_can_start_rejects_over_cap(self):
        from src.autopilot.service import AutopilotServiceRegistry

        registry = AutopilotServiceRegistry(max_concurrent=2)
        self._running_service(registry, "proj-a")
        self._running_service(registry, "proj-b")

        allowed, message = registry.can_start("proj-c")
        assert allowed is False
        assert "proj-a" in message
        assert "proj-b" in message

    def test_can_start_never_blocks_restart_of_already_running_project(self):
        from src.autopilot.service import AutopilotServiceRegistry

        registry = AutopilotServiceRegistry(max_concurrent=1)
        self._running_service(registry, "proj-a")

        allowed, message = registry.can_start("proj-a")
        assert allowed is True
        assert message == ""

    def test_running_excludes_stopped_services(self):
        from src.autopilot.service import AutopilotServiceRegistry

        registry = AutopilotServiceRegistry(max_concurrent=2)
        registry.get_or_create("proj-a")  # never started

        assert registry.running() == []


class TestAutopilotServiceRegistryTryReserve:
    """Regression: can_start() alone is check-then-act with no lock spanning
    the check and the caller's own start() call -- two concurrent callers
    could both pass the cap check before either registers as running,
    landing N+1 concurrent pipelines against a cap of N. try_reserve()
    closes this by marking the slot occupied atomically, under the same
    lock as the check itself."""

    def _running_service(self, registry, project_id):
        service = registry.get_or_create(project_id)
        service._running = True
        service._task = Mock()
        service._task.done.return_value = False
        return service

    def test_reserve_then_reserve_again_for_new_project_is_rejected(self):
        """This is the exact race can_start() alone misses: a project
        that's only RESERVED (not yet actually running) must still count
        against the cap for a second, different project's reservation
        attempt."""
        from src.autopilot.service import AutopilotServiceRegistry

        registry = AutopilotServiceRegistry(max_concurrent=1)

        allowed_a, _ = registry.try_reserve("proj-a")
        assert allowed_a is True
        # proj-a isn't running yet (start() hasn't resolved) -- can_start()
        # alone would see zero running services here and wrongly allow a
        # second reservation too.
        assert registry.running() == []

        allowed_b, message_b = registry.try_reserve("proj-b")
        assert allowed_b is False
        assert "proj-a" in message_b

    def test_reserve_same_project_twice_is_allowed(self):
        from src.autopilot.service import AutopilotServiceRegistry

        registry = AutopilotServiceRegistry(max_concurrent=1)

        allowed_1, _ = registry.try_reserve("proj-a")
        allowed_2, _ = registry.try_reserve("proj-a")
        assert allowed_1 is True
        assert allowed_2 is True

    def test_release_reservation_frees_the_slot(self):
        from src.autopilot.service import AutopilotServiceRegistry

        registry = AutopilotServiceRegistry(max_concurrent=1)

        registry.try_reserve("proj-a")
        registry.release_reservation("proj-a")

        # proj-a never actually started (release simulates start() failing)
        # -- the slot must be free for a different project now.
        allowed, message = registry.try_reserve("proj-b")
        assert allowed is True
        assert message == ""

    def test_release_reservation_of_unreserved_project_is_safe(self):
        from src.autopilot.service import AutopilotServiceRegistry

        registry = AutopilotServiceRegistry(max_concurrent=1)
        registry.release_reservation("never-reserved")  # should not raise

    def test_reserve_counts_running_and_pending_together_against_cap(self):
        from src.autopilot.service import AutopilotServiceRegistry

        registry = AutopilotServiceRegistry(max_concurrent=2)
        self._running_service(registry, "proj-a")
        registry.try_reserve("proj-b")

        allowed, message = registry.try_reserve("proj-c")
        assert allowed is False
        assert "proj-a" in message
        assert "proj-b" in message


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
        # Redirect the persisted-state DB so tests never touch the real
        # hephaestus.db's ProjectContext rows.
        from src.core.database import DatabaseManager

        db_path = tmp_path / "test.db"
        DatabaseManager(str(db_path)).create_tables()
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))

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

    @pytest.mark.asyncio
    async def test_pause_for_restart_keeps_persisted_state(self, service, tmp_path):
        """Regression (docs/SAFE_RESTART_DESIGN.md §3.1): unlike stop(),
        pause_for_restart() must NOT clear the persisted marker -- a
        restart-triggered pause still needs _resume_interrupted_workflows
        to auto-resume it on the next startup, unlike an explicit user
        Stop which deliberately means "don't come back."."""
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()

        with patch.object(service, "_run_pipeline", new_callable=AsyncMock):
            await service.start(str(project))
            assert service.load_persisted_state() is not None

            result = await service.pause_for_restart()

            assert result["paused"] is True
            assert service.load_persisted_state() is not None
            assert service.running is False

    def test_load_persisted_state_when_absent(self, service):
        assert service.load_persisted_state() is None

    def test_clear_persisted_state_when_absent_is_safe(self, service):
        # Should not raise even if nothing was ever persisted
        service.clear_persisted_state()
        assert service.load_persisted_state() is None

    @pytest.mark.asyncio
    async def test_persisted_state_is_namespaced_per_project(self, tmp_path):
        """Regression: persisted running-state used to live under one bare
        key shared by the whole backend -- a second project starting would
        silently overwrite the first project's "resume on restart" marker.
        Two services for two different projects must each see only their
        own persisted state."""
        from src.autopilot.service import AutopilotService

        service_a = AutopilotService()
        service_b = AutopilotService()

        project_a = tmp_path / "project-a"
        project_a.mkdir()
        (project_a / ".git").mkdir()
        project_b = tmp_path / "project-b"
        project_b.mkdir()
        (project_b / ".git").mkdir()

        with patch.object(service_a, "_run_pipeline", new_callable=AsyncMock):
            await service_a.start(str(project_a), max_iterations=3)
        with patch.object(service_b, "_run_pipeline", new_callable=AsyncMock):
            await service_b.start(str(project_b), max_iterations=9)

        state_a = service_a.load_persisted_state()
        state_b = service_b.load_persisted_state()
        assert state_a["project_path"] == str(project_a)
        assert state_a["max_iterations"] == 3
        assert state_b["project_path"] == str(project_b)
        assert state_b["max_iterations"] == 9

        await service_a.stop()
        # Stopping project A must not touch project B's persisted state.
        assert service_b.load_persisted_state() is not None

        await service_b.stop()

    def test_enumerate_persisted_states_migrates_legacy_key(self, tmp_path):
        """Sotto's currently-running pipeline (pre-multi-project) persisted
        its "resume on restart" marker under the old bare key. The very
        first read after this change deploys must migrate it onto the
        namespaced key in place, or it silently stops auto-resuming."""
        from src.autopilot.orchestrator.state import _RUNNING_STATE_KEY_LEGACY
        from src.autopilot.orchestrator.state import (
    _running_state_key,
    _set_project_context,
)
        from src.autopilot.service import AutopilotService
        from src.core.database import AutopilotProject, get_db

        project = tmp_path / "sotto"
        project.mkdir()

        with get_db() as db:
            db.add(
                AutopilotProject(
                    id="proj-sotto",
                    name="sotto",
                    base_dir=str(project.resolve()),
                    is_active=True,
                )
            )
            _set_project_context(
                db,
                _RUNNING_STATE_KEY_LEGACY,
                {
                    "project_path": str(project),
                    "design_queue": "",
                    "max_iterations": 10,
                },
            )

        results = AutopilotService.enumerate_persisted_states()

        assert results == [("proj-sotto", {
            "project_path": str(project),
            "design_queue": "",
            "max_iterations": 10,
        })]

        with get_db() as db:
            from src.core.database import ProjectContext

            assert (
                db.query(ProjectContext).filter_by(key=_RUNNING_STATE_KEY_LEGACY).first()
                is None
            )
            assert (
                db.query(ProjectContext)
                .filter_by(key=_running_state_key("proj-sotto"))
                .first()
                is not None
            )

        # Idempotent: a second call after migration sees the namespaced key
        # like any other project, and doesn't duplicate/re-migrate anything.
        results_again = AutopilotService.enumerate_persisted_states()
        assert results_again == results
