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
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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
        return self._running and self._task is not None and not self._task.done()

    async def start(
        self,
        project_path: str,
        design_queue: str = "",
        max_iterations: int = 10,
    ) -> Dict[str, Any]:
        """Start the autopilot pipeline.

        Args:
            project_path: Root directory of the project to work on
            design_queue: Directory containing design documents (default: <project>/docs/design-queue)
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

        dq = design_queue or str(project / "docs" / "design-queue")
        Path(dq).mkdir(parents=True, exist_ok=True)

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

        return {"started": True, "project": str(project)}

    async def stop(self) -> Dict[str, Any]:
        """Stop the autopilot pipeline.

        Returns:
            Dict with 'stopped' key and stats
        """
        if not self.running:
            return {"stopped": True, "message": "Pipeline was not running"}

        self._stop_event.set()
        self._running = False

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
        elapsed = int(time.time() - self._start_time) if self._start_time and self._running else 0

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
            from src.autopilot.orchestrator import run_continuous_pipeline
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
            self._running = False
            logger.info("Pipeline task finished")

    def _run_pipeline_sync(self, args):
        """Synchronous pipeline runner (called from thread executor).

        This is a thin wrapper that sets up the stop callback.
        """
        from src.autopilot.orchestrator import run_continuous_pipeline

        # Monkey-patch the stop event into the module so the pipeline can check it
        import src.autopilot.orchestrator as orch_module
        orch_module._service_stop_event = self._stop_event

        try:
            run_continuous_pipeline(args)
        except KeyboardInterrupt:
            logger.info("Pipeline interrupted")
        finally:
            self._running = False


# Global singleton instance
_service: Optional[AutopilotService] = None


def get_autopilot_service() -> AutopilotService:
    """Get the global AutopilotService instance."""
    global _service
    if _service is None:
        _service = AutopilotService()
    return _service
