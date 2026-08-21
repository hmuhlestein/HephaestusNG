"""
Orchestrator run logging and the pipeline-registry self-heal sweep.

Split out of pipeline.py (SOLID review: pipeline.py itself had grown past
the ~800-line-per-module budget the rest of this refactor was held to --
see docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md). This module has no
external string-based test patches keyed to "pipeline.<name>" (verified
by grepping every tests/*.py mock.patch target before moving), so the
move is a pure relocation -- no test updates needed.
"""
import asyncio
import json
import threading
from datetime import datetime
from pathlib import Path

from src.autopilot.orchestrator.runtime_registries import _should_stop
from src.autopilot.orchestrator.state import PipelineState


class OrchestratorLogger:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = log_dir / "orchestrator.log"
        self.events_file = log_dir / "events.jsonl"
        self.state_file = log_dir / "state.json"
        self._lock = threading.Lock()

    def log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] [{level}] {message}"
        try:
            pass
        except OSError:
            pass  # Broken pipe when running as subprocess with DEVNULL
        with self._lock:
            with open(self.log_file, "a") as f:
                f.write(line + "\n")

    def debug(self, message: str):
        # Three call sites already used logger.debug() on this class, which
        # has never had one -- each raised AttributeError instead of logging.
        # The worst was run_single_workflow's, sitting in the handler for a
        # failed pipeline_metrics.json patch: the AttributeError escaped into
        # the enclosing `except Exception`, which reported "Failed to launch
        # workflow" and returned FAILED, turning a cosmetic metrics problem
        # into a dead workflow. Found by mypy once it was unblocked (c38f143).
        self.log(message, "DEBUG")

    def info(self, message: str):
        self.log(message, "INFO")

    def warning(self, message: str):
        self.log(message, "WARNING")

    def error(self, message: str):
        self.log(message, "ERROR")

    def event(self, event_type: str, data: dict):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": event_type,
            **data,
        }
        with self._lock:
            with open(self.events_file, "a") as f:
                f.write(json.dumps(entry) + "\n")

    def save_state(self, state: PipelineState):
        with open(self.state_file, "w") as f:
            json.dump(
                {
                    "designs_processed": state.designs_processed,
                    "designs_succeeded": state.designs_succeeded,
                    "designs_failed": state.designs_failed,
                    "total_elapsed": state.total_elapsed,
                    "current_design": state.current_design,
                    "queue_status": state.queue_status,
                },
                f,
                indent=2,
            )


def _resync_pipeline_registry(logger: OrchestratorLogger, loop: "asyncio.AbstractEventLoop") -> int:
    """Self-heal for a project whose persisted "was running" marker
    (AutopilotService.enumerate_persisted_states) says its pipeline should
    be running, but AutopilotServiceRegistry has no live entry for it --
    the one-shot startup resume (_resume_interrupted_workflows) either
    never ran for it or failed silently. See
    docs/SAFE_RESTART_DESIGN.md §3.5.

    Runs from the same generic, restart-safe background sweep as
    _sync_stale_feature_statuses -- catches whatever the startup resume
    missed, on an ongoing basis instead of only once at boot. Observed
    live: several backend restarts in quick succession left a project's
    pipeline dead (no crash, no error -- it just never got another turn to
    pick up new work) while its own "is this project running" status
    still read healthy, derived from an unrelated still-active workflow
    rather than the pipeline loop itself.

    AutopilotService.start() is async and spawns its own long-lived
    background task (self._task) that must stay tied to the server's
    persistent event loop, not a throwaway one -- asyncio.run(...) (this
    module's usual sync-to-async bridge, see create_agent_for_task_direct)
    would create and then immediately close a temporary loop, silently
    orphaning that task the moment start() itself returns. Scheduling onto
    the real loop via run_coroutine_threadsafe avoids that.
    """
    from src.autopilot.service import AutopilotService, get_registry

    try:
        persisted = AutopilotService.enumerate_persisted_states()
    except Exception as e:
        logger.warning(f"[PIPELINE-RESYNC] Could not enumerate persisted state: {e}")
        return 0

    registry = get_registry()
    resumed = 0
    for project_id, state in persisted:
        project_path = state.get("project_path")
        if not project_path:
            continue

        existing = registry.get(project_id)
        if existing and existing.running:
            continue  # already tracked and alive -- nothing to do

        if _should_stop(project_id):
            # A pause_for_restart() (or an explicit stop()) is already
            # in-flight for this project -- its registry entry can look
            # exactly like "should restart" here (running momentarily
            # False, persisted marker deliberately left intact) while it's
            # still mid-drain. Restarting it now would race the graceful
            # pause itself. Let the NEXT sweep tick re-check once that
            # settles, rather than force a restart mid-shutdown.
            logger.debug(
                f"[PIPELINE-RESYNC] Project {project_id[:8]}: stop already "
                "in flight, skipping this tick"
            )
            continue

        logger.warning(
            f"[PIPELINE-RESYNC] Project {project_id[:8]}: persisted state "
            "says running but no live pipeline found -- restarting"
        )
        try:
            service = registry.get_or_create(project_id)
            future = asyncio.run_coroutine_threadsafe(
                service.start(
                    project_path=project_path,
                    design_queue=state.get("design_queue", ""),
                    max_iterations=state.get("max_iterations", 10),
                ),
                loop,
            )
            future.result(timeout=30.0)
            resumed += 1
        except Exception as e:
            logger.warning(
                f"[PIPELINE-RESYNC] Failed to restart project {project_id[:8]}: {e}"
            )
    return resumed
