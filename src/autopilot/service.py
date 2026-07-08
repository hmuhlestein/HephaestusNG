"""In-process Autopilot service.

Replaces the subprocess-based orchestrator with an asyncio task that runs
inside the backend process. The CLI and API both call this service;
neither spawns a subprocess.

This fixes:
- B5: Liveness disagreement (one PID convention, not three)
- B6: Duplicate HephaestusSDK (service uses the existing backend services)
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from src.core.constants import AUTOPILOT_STATE_DIR, DESIGN_CONTEXT_SUBDIR, DESIGN_SUBDIR

logger = logging.getLogger(__name__)

# Persisted across backend restarts so a running pipeline can resume itself —
# without this, an in-flight pipeline goes silently dead on any backend
# restart/crash (this class lives entirely in-process, see module docstring),
# and nothing ever notices except the separate diagnostic monitor process,
# which only patches over the gap much later and much more crudely.
_RUNNING_STATE_PATH = Path(AUTOPILOT_STATE_DIR) / "running_pipeline.json"


class AutopilotService:
    """Manages the autopilot pipeline as an asyncio task inside the backend.

    Usage:
        service = AutopilotService()
        await service.start(project_path="/path/to/project")
        status = service.status()
        await service.stop()
    """

    def __init__(self):
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

        # Activate the matching project so pick_next_design() finds its designs.
        # Without this, the pipeline queries is_active=True which may point
        # at a completely different project (e.g. Sotto instead of smoke-test).
        #
        # Auto-create the row if none exists (e.g. its project was deleted
        # via the UI, or a pipeline was started against a path never
        # registered through the projects API at all) -- otherwise this
        # path is invisible/unmanageable in the UI even though the pipeline
        # itself runs fine underneath via the file-based design-queue
        # fallback in pick_next_design, which doesn't need a project row.
        try:
            import uuid as _uuid

            from src.core.database import AutopilotProject, get_db

            with get_db() as db:
                proj = (
                    db.query(AutopilotProject)
                    .filter_by(base_dir=str(project))
                    .first()
                )
                if not proj:
                    proj = AutopilotProject(
                        id=f"proj-{_uuid.uuid4().hex[:12]}",
                        name=project.name,
                        base_dir=str(project),
                        is_active=False,
                    )
                    db.add(proj)
                    db.flush()
                    logger.info(
                        f"Auto-created project '{proj.name}' for {project} "
                        "(none registered)"
                    )
                if not proj.is_active:
                    # Deactivate current active project
                    current = db.query(AutopilotProject).filter_by(is_active=True).first()
                    if current:
                        current.is_active = False
                    proj.is_active = True
                    db.commit()
                    logger.info(f"Activated project '{proj.name}' for pipeline")
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
            _RUNNING_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _RUNNING_STATE_PATH.write_text(
                json.dumps(
                    {
                        "project_path": self._project_path,
                        "design_queue": self._design_queue,
                        "max_iterations": self._max_iterations,
                    }
                )
            )
        except Exception as e:
            logger.warning(f"Failed to persist autopilot running state: {e}")

    @staticmethod
    def clear_persisted_state() -> None:
        """Remove the persisted run state (deliberate stop — don't auto-resume)."""
        try:
            _RUNNING_STATE_PATH.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to clear persisted autopilot state: {e}")

    @staticmethod
    def load_persisted_state() -> Optional[Dict[str, Any]]:
        """Read persisted run params, if any (used to auto-resume on startup)."""
        try:
            if _RUNNING_STATE_PATH.exists():
                return json.loads(_RUNNING_STATE_PATH.read_text())
        except Exception as e:
            logger.warning(f"Failed to read persisted autopilot state: {e}")
        return None

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
        # Monkey-patch the stop event into the module so the pipeline can check it
        import src.autopilot.orchestrator as orch_module
        from src.autopilot.orchestrator import run_continuous_pipeline

        orch_module._service_stop_event = self._stop_event

        try:
            run_continuous_pipeline(args)
        except KeyboardInterrupt:
            logger.info("Pipeline interrupted")
        finally:
            logger.warning(
                f"[SERVICE] _run_pipeline_sync finally: _running={self._running}"
            )
            self._running = False


# Global singleton instance
_service: Optional[AutopilotService] = None


def get_autopilot_service() -> AutopilotService:
    """Get the global AutopilotService instance."""
    global _service
    if _service is None:
        _service = AutopilotService()
    return _service
