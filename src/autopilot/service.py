"""In-process Autopilot service.

Replaces the subprocess-based orchestrator with an asyncio task that runs
inside the backend process. The CLI and API both call this service;
neither spawns a subprocess.

This fixes:
- B5: Liveness disagreement (one PID convention, not three)
- B6: Duplicate HephaestusSDK (service uses the existing backend services)
"""

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from src.core.constants import DESIGN_CONTEXT_SUBDIR

logger = logging.getLogger(__name__)

# Persisted across backend restarts so a running pipeline can resume itself —
# without this, an in-flight pipeline goes silently dead on any backend
# restart/crash (this class lives entirely in-process, see module docstring),
# and nothing ever notices except the separate diagnostic monitor process,
# which only patches over the gap much later and much more crudely.
# See src.autopilot.orchestrator._running_state_key -- backed by the
# ProjectContext table (a generic key-value store) instead of a JSON file,
# so a DB-level reset of workflow state can't leave this pointing at a
# workflow that no longer exists. Namespaced per project_id (see
# AutopilotServiceRegistry below) -- multiple projects can each have their
# own persisted "was running" marker.


class AutopilotService:
    """Manages one project's autopilot pipeline as an asyncio task inside
    the backend. One instance per project_id -- see AutopilotServiceRegistry,
    which is the only supported way to obtain one (via get_autopilot_service).

    Usage:
        service = get_autopilot_service(project_id)
        await service.start(project_path="/path/to/project")
        status = service.status()
        await service.stop()
    """

    def __init__(self, project_id: Optional[str] = None):
        # project_id is always overwritten with the real resolved id by
        # start() before it returns (via _get_or_create_project_id) -- the
        # None default here only matters transiently before the first
        # start() call, and preserves the zero-arg construction existing
        # tests/callers may still use.
        self.project_id = project_id
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._stop_event = asyncio.Event()
        self._project_path: Optional[str] = None
        self._design_queue: Optional[str] = None
        self._max_iterations: int = 10
        self._start_time: Optional[float] = None
        self._current_design: Optional[str] = None
        self._designs_processed: int = 0
        self._designs_succeeded: int = 0
        self._designs_failed: int = 0
        self._error: Optional[str] = None

    @property
    def running(self) -> bool:
        task_done = self._task.done() if self._task else "N/A"
        r = self._running and self._task is not None and not self._task.done()
        logger.warning(
            f"[SERVICE] running check: _running={self._running}, _task={self._task is not None}, done={task_done}, result={r}"
        )
        return r

    async def start(
        self,
        project_path: str,
        design_queue: str = "",
        max_iterations: int = 10,
    ) -> Dict[str, Any]:
        """Start the autopilot pipeline.

        Args:
            project_path: Root directory of the project to work on
            design_queue: Directory containing design documents (default: <project>/docs/design)
            max_iterations: Maps to engine's max_total_gotos

        Returns:
            Dict with 'started' key

        Raises:
            RuntimeError: If pipeline is already running
        """
        if self.running:
            raise RuntimeError("Pipeline is already running")

        project = Path(project_path).resolve()
        if not project.exists():
            raise ValueError(f"Project path does not exist: {project_path}")

        # Verify it's a git repo
        if not (project / ".git").exists():
            raise ValueError(f"Project path is not a git repository: {project_path}")

        dq = design_queue or str(project / DESIGN_CONTEXT_SUBDIR)
        Path(dq).mkdir(parents=True, exist_ok=True)

        # Activate the matching project so pick_next_design() finds its
        # designs, auto-creating the AutopilotProject row if none exists,
        # and resume any workflows the user had explicitly paused for it.
        # Extracted to _get_or_create_project_id (orchestrator.py) so
        # callers that need project_id BEFORE starting a pipeline (e.g.
        # POST /start's concurrency-cap check) share this exact logic
        # instead of a second, divergent copy.
        try:
            from src.autopilot.orchestrator.state import _get_or_create_project_id

            self.project_id = _get_or_create_project_id(str(project))
        except Exception as e:
            logger.warning(f"Could not activate project: {e}")

        # Reset state
        self._stop_event.clear()
        self._project_path = str(project)
        self._design_queue = dq
        self._max_iterations = max_iterations
        self._start_time = time.time()
        self._current_design = None
        self._designs_processed = 0
        self._designs_succeeded = 0
        self._designs_failed = 0
        self._error = None
        self._running = True

        # Start the pipeline task
        self._task = asyncio.create_task(self._run_pipeline())
        logger.info(f"Autopilot service started for {project}")

        self._persist_running_state()

        return {"started": True, "project": str(project)}

    def _persist_running_state(self) -> None:
        """Write current run params so a restart can resume this pipeline."""
        try:
            from src.autopilot.orchestrator.state import (
    _running_state_key,
    _set_project_context,
)
            from src.core.database import get_db

            with get_db() as db:
                _set_project_context(
                    db,
                    _running_state_key(self.project_id),
                    {
                        "project_path": self._project_path,
                        "design_queue": self._design_queue,
                        "max_iterations": self._max_iterations,
                    },
                )
        except Exception as e:
            logger.warning(f"Failed to persist autopilot running state: {e}")

    def clear_persisted_state(self) -> None:
        """Remove the persisted run state (deliberate stop — don't auto-resume)."""
        try:
            from src.autopilot.orchestrator.state import (
    _delete_project_context,
    _running_state_key,
)
            from src.core.database import get_db

            with get_db() as db:
                _delete_project_context(db, _running_state_key(self.project_id))
        except Exception as e:
            logger.warning(f"Failed to clear persisted autopilot state: {e}")

    def load_persisted_state(self) -> Optional[Dict[str, Any]]:
        """Read persisted run params, if any (used to auto-resume on startup)."""
        try:
            from src.autopilot.orchestrator.state import (
    _get_project_context,
    _running_state_key,
)
            from src.core.database import get_db

            with get_db() as db:
                return _get_project_context(db, _running_state_key(self.project_id))
        except Exception as e:
            logger.warning(f"Failed to read persisted autopilot state: {e}")
        return None

    @staticmethod
    def enumerate_persisted_states() -> List[Tuple[str, Dict[str, Any]]]:
        """All (project_id, state) pairs with a persisted "was running"
        marker, for server.py's startup auto-resume across every project.

        Migrates the pre-multi-project bare key in place on first read:
        resolves its project_id from its own persisted project_path, writes
        it under the namespaced key, deletes the old one. Idempotent -- a
        second call after migration just sees the namespaced key like any
        other project. Without this, a pipeline that was running before
        this change deployed would silently stop auto-resuming on the next
        backend restart.
        """
        from src.autopilot.orchestrator.state import (
            _RUNNING_STATE_KEY_LEGACY,
            _RUNNING_STATE_KEY_PREFIX,
            _delete_project_context,
            _get_project_context,
            _get_project_contexts_by_prefix,
            _resolve_project_id,
            _running_state_key,
            _set_project_context,
        )
        from src.core.database import get_db

        results: List[Tuple[str, Dict[str, Any]]] = []
        try:
            with get_db() as db:
                legacy = _get_project_context(db, _RUNNING_STATE_KEY_LEGACY)
                if legacy and legacy.get("project_path"):
                    project_id = _resolve_project_id(legacy["project_path"])
                    if project_id:
                        logger.info(
                            f"[MIGRATE] Namespacing legacy running-pipeline "
                            f"state to project {project_id}"
                        )
                        _set_project_context(db, _running_state_key(project_id), legacy)
                        _delete_project_context(db, _RUNNING_STATE_KEY_LEGACY)
                        db.commit()
                        results.append((project_id, legacy))
                    else:
                        logger.warning(
                            "[MIGRATE] Legacy running-pipeline state's "
                            f"project_path {legacy['project_path']!r} no "
                            "longer resolves to a known project -- leaving "
                            "it in place, not auto-resuming"
                        )

                by_prefix = _get_project_contexts_by_prefix(db, _RUNNING_STATE_KEY_PREFIX)
                seen = {pid for pid, _ in results}
                for key, state in by_prefix.items():
                    project_id = key[len(_RUNNING_STATE_KEY_PREFIX):]
                    if project_id not in seen:
                        results.append((project_id, state))
                        seen.add(project_id)
        except Exception as e:
            logger.warning(f"Failed to enumerate persisted autopilot state: {e}")
        return results

    async def stop(self) -> Dict[str, Any]:
        """Stop the autopilot pipeline.

        Returns:
            Dict with 'stopped' key and stats
        """
        if not self.running:
            return {"stopped": True, "message": "Pipeline was not running"}

        self._stop_event.set()
        self._running = False
        self.clear_persisted_state()

        # Wait for task to finish (with timeout)
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass

        elapsed = int(time.time() - self._start_time) if self._start_time else 0
        logger.info(f"Autopilot service stopped after {elapsed}s")

        return {
            "stopped": True,
            "elapsed_seconds": elapsed,
            "designs_processed": self._designs_processed,
            "designs_succeeded": self._designs_succeeded,
            "designs_failed": self._designs_failed,
        }

    async def pause_for_restart(self) -> Dict[str, Any]:
        """Pause the pipeline for a backend restart -- same stop-signal and
        bounded-wait-then-cancel mechanics as stop(), but deliberately does
        NOT call clear_persisted_state(). stop() clears it because an
        explicit user Stop means "don't auto-resume"; a restart means the
        opposite -- _resume_interrupted_workflows must still find the
        persisted "was running" marker on the next startup. See
        docs/SAFE_RESTART_DESIGN.md §3.1.

        Longer timeout than stop()'s 10s: there's no impatient CLI caller
        waiting on this one (it runs from shutdown_event(), not a user
        command), and run_continuous_pipeline's loop can be mid a blocking
        dispatch sequence (e.g. the ~25s "waiting for pi agent to
        initialize" step) that doesn't check _should_stop() at all, on top
        of the interruptible-sleep polling interval itself -- 10s risked
        almost always hitting the cancel fallback instead of the clean
        exit path this exists to give the loop a chance to reach.
        """
        if not self.running:
            return {"paused": True, "message": "Pipeline was not running"}

        self._stop_event.set()
        self._running = False

        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=45.0)
            except asyncio.TimeoutError:
                logger.warning(
                    f"[PAUSE-FOR-RESTART] Project {self.project_id}: pipeline "
                    "did not exit cleanly within 45s, cancelling"
                )
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass

        elapsed = int(time.time() - self._start_time) if self._start_time else 0
        logger.info(
            f"[PAUSE-FOR-RESTART] Project {self.project_id}: paused after "
            f"{elapsed}s, persisted state kept for auto-resume"
        )

        return {"paused": True, "elapsed_seconds": elapsed}

    def status(self) -> Dict[str, Any]:
        """Get current pipeline status.

        Returns:
            Dict with pipeline state
        """
        elapsed = (
            int(time.time() - self._start_time)
            if self._start_time and self._running
            else 0
        )

        return {
            "running": self.running,
            "project_path": self._project_path,
            "current_design": self._current_design,
            "designs_processed": self._designs_processed,
            "designs_succeeded": self._designs_succeeded,
            "designs_failed": self._designs_failed,
            "elapsed_seconds": elapsed,
            "error": self._error,
        }

    async def _run_pipeline(self):
        """Main pipeline loop running in an asyncio task.

        This wraps the synchronous orchestrator logic in a thread executor
        to avoid blocking the event loop.
        """
        try:
            logger.info("=" * 70)
            logger.info("AUTOPILOT SERVICE - PIPELINE STARTED")
            logger.info(f"Project: {self._project_path}")
            logger.info(f"Design Queue: {self._design_queue}")
            logger.info(f"Max Iterations: {self._max_iterations}")
            logger.info("=" * 70)

            # Import here to avoid circular imports
            import argparse

            # Create args namespace matching what run_continuous_pipeline expects
            args = argparse.Namespace(
                project_path=self._project_path,
                design_queue=self._design_queue,
                max_iterations=self._max_iterations,
                drop_db=False,
                workflow="autopilot",
                cycle=False,
                cycle_on_failure=False,
                description="",
                max_hours=0,
                # Tells run_continuous_pipeline it's executing inside this
                # already-running backend process (as opposed to the
                # standalone `python -m src.autopilot.orchestrator` CLI path,
                # which builds its own argparse.Namespace without this flag)
                # -- see sdk.start()'s assume_backend_running.
                in_process=True,
                # self.project_id was already reliably resolved by start()
                # (via _get_or_create_project_id) -- pass it through instead
                # of making run_continuous_pipeline re-derive it from a
                # fresh, independently-fallible DB lookup. Without this, a
                # transient failure in that lookup leaves current_project_id
                # None for the run's entire duration, which silently turns
                # _should_stop(None) into a permanent no-op (see
                # _should_stop's "no project_id, don't guess" guard).
                project_id=self.project_id,
            )

            # Run the synchronous pipeline in a thread executor
            # The pipeline handles its own polling loop
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._run_pipeline_sync, args)

        except asyncio.CancelledError:
            logger.info("Pipeline task cancelled")
        except Exception as e:
            logger.error(f"Pipeline error: {e}", exc_info=True)
            self._error = str(e)
        finally:
            import traceback

            logger.warning(
                f"[SERVICE] Pipeline task finished. _running was {self._running}. Traceback: {traceback.format_stack()[-3:]}"
            )
            self._running = False
            logger.info("Pipeline task finished")

    def _run_pipeline_sync(self, args):
        """Synchronous pipeline runner (called from thread executor).

        This is a thin wrapper that sets up the stop callback.
        """
        # Register this service's stop event under its own project_id so
        # the pipeline can check it via _should_stop(project_id) -- keyed,
        # not a single bare module global, so a second project starting
        # can't silently steal control of this project's stop signal.
        import src.autopilot.orchestrator as orch_module
        from src.autopilot.orchestrator import run_continuous_pipeline

        orch_module._stop_events[self.project_id] = self._stop_event

        try:
            run_continuous_pipeline(args)
        except KeyboardInterrupt:
            logger.info("Pipeline interrupted")
        finally:
            logger.warning(
                f"[SERVICE] _run_pipeline_sync finally: _running={self._running}"
            )
            self._running = False


class AutopilotServiceRegistry:
    """Per-project AutopilotService instances, replacing the old single
    global singleton -- see docs/MULTI_PROJECT_CONCURRENCY_DESIGN.md.

    threading.Lock, not asyncio.Lock: run_continuous_pipeline executes
    inside loop.run_in_executor(None, ...) -- a real OS thread, not a
    coroutine -- while AutopilotService.start()/stop() are coroutines on
    the event loop. This dict is touched from both, so it needs a
    primitive safe across that boundary (matches OrchestratorLogger._lock's
    same reasoning, orchestrator.py).
    """

    def __init__(self, max_concurrent: int):
        self._services: Dict[str, AutopilotService] = {}
        self._max_concurrent = max_concurrent
        self._lock = threading.Lock()
        # project_ids in the process of starting, between try_reserve()
        # and their AutopilotService actually becoming .running -- closes
        # the check-then-act gap plain can_start() has on its own (see
        # try_reserve's docstring).
        self._pending: Set[str] = set()

    def get_or_create(self, project_id: str) -> AutopilotService:
        with self._lock:
            if project_id not in self._services:
                self._services[project_id] = AutopilotService(project_id)
            return self._services[project_id]

    def get(self, project_id: str) -> Optional[AutopilotService]:
        return self._services.get(project_id)

    def running(self) -> List[AutopilotService]:
        return [s for s in self._services.values() if s.running]

    def _occupied_slots(self) -> Set[str]:
        """project_ids currently counted against the cap: genuinely running
        services plus ones with an in-flight reservation. Must be read
        under self._lock -- callers hold it already (try_reserve) or don't
        need atomicity (can_start's best-effort read for display)."""
        return {s.project_id for s in self._services.values() if s.running} | self._pending

    def can_start(self, project_id: str) -> Tuple[bool, str]:
        """Best-effort cap check for display/logging -- NOT atomic, see
        try_reserve() for the version that actually closes the race between
        checking the cap and registering as running."""
        with self._lock:
            occupied = self._occupied_slots()
        if project_id in occupied:
            return True, ""  # restarting/already-tracked project, not a new slot
        if len(occupied) >= self._max_concurrent:
            names = ", ".join(sorted(occupied))
            return False, (
                f"Max concurrent projects ({self._max_concurrent}) reached: "
                f"{names}. Stop one before starting another."
            )
        return True, ""

    def try_reserve(self, project_id: str) -> Tuple[bool, str]:
        """Atomically check the concurrency cap and, if it passes, reserve
        a slot for project_id -- closes the TOCTOU window plain can_start()
        has: two concurrent callers checking can_start() before either has
        actually started could otherwise both pass the same cap check and
        land N+1 concurrent pipelines against a cap of N.

        Every caller that gets (True, "") back MUST call
        release_reservation(project_id) exactly once afterward (success or
        failure) -- typically in a try/finally around the actual
        service.start() call. Safe to call repeatedly for a project already
        running or already reserved (never counts as a second slot).
        """
        with self._lock:
            occupied = self._occupied_slots()
            if project_id in occupied:
                return True, ""
            if len(occupied) >= self._max_concurrent:
                names = ", ".join(sorted(occupied))
                return False, (
                    f"Max concurrent projects ({self._max_concurrent}) reached: "
                    f"{names}. Stop one before starting another."
                )
            self._pending.add(project_id)
            return True, ""

    def release_reservation(self, project_id: str) -> None:
        """Release a try_reserve() slot once the caller's own start()
        attempt has resolved (successfully or not) -- a successful start
        continues occupying a slot via running(), not _pending, from then
        on; safe to call even if nothing was reserved."""
        with self._lock:
            self._pending.discard(project_id)


_registry: Optional[AutopilotServiceRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> AutopilotServiceRegistry:
    """Get the global AutopilotServiceRegistry instance (one per backend
    process, tracking every project's AutopilotService)."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                from src.core.simple_config import get_config

                _registry = AutopilotServiceRegistry(get_config().autopilot.max_concurrent_projects)
    return _registry


def get_autopilot_service(project_id: str) -> AutopilotService:
    """Get (or create) the AutopilotService instance for this project."""
    return get_registry().get_or_create(project_id)
