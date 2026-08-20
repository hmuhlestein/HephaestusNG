"""In-process runtime registries: which workflows are actively being
polled, and per-project stop signals. Extracted from orchestrator/__init__.py
(SOLID review: that module mixed these small stateful trackers into the
actual pipeline-execution flow -- see
docs/SOLID_OO_REVIEW_UPDATE_2026-08-19.md).
"""

import asyncio
import threading as _threading
import time
from typing import Dict, Optional

# Workflow IDs currently being polled by run_single_workflow.
# The background _advance_phases sweep skips these — the inline
# call in run_single_workflow is the main path for phase advancement;
# the sweep is a fallback for workflows that lost their poll loop
# (e.g. backend restart).
_actively_monitored_lock = _threading.Lock()
_actively_monitored_workflows: set = set()


def _register_monitored_workflow(workflow_id: str) -> None:
    with _actively_monitored_lock:
        _actively_monitored_workflows.add(workflow_id)


def _unregister_monitored_workflow(workflow_id: str) -> None:
    with _actively_monitored_lock:
        _actively_monitored_workflows.discard(workflow_id)


def _is_workflow_monitored(workflow_id: str) -> bool:
    with _actively_monitored_lock:
        return workflow_id in _actively_monitored_workflows


# Per-workflow locks providing true mutual exclusion around _advance_phases
# calls. _actively_monitored_workflows above is an optimization (the sweep
# skips a workflow entirely rather than attempting it) but is advisory
# only -- _is_workflow_monitored() and the caller's subsequent
# _advance_phases() call are two separate, non-atomic steps, so a workflow
# whose run_single_workflow poll loop is just starting up (registered
# after the sweep's check but before its _advance_phases call returns)
# could still race. _try_advance_phases below is the actual guarantee:
# only one caller can be inside _advance_phases for a given workflow_id
# at a time, whether that's the sweep, run_single_workflow, or (in the
# rare case _actively_monitored_workflows didn't already prevent it) both.


# project_id -> the AutopilotService's own asyncio.Event, registered by
# AutopilotService._run_pipeline_sync. Was a single bare module global
# (_service_stop_event) -- a second project starting overwrote the first
# project's reference silently, so whichever project's stop() fired last
# won control of BOTH pipelines' stop signal (project A's "stop" could be
# swallowed, or could incorrectly stop project B). Keyed by project_id,
# not workflow_id: AutopilotService is 1:1 with a project, not a workflow
# (run_continuous_pipeline, one of this function's three call sites, spans
# many workflows over its life and has no single workflow_id to key by).
_stop_events: Dict[str, "asyncio.Event"] = {}


def _should_stop(project_id: Optional[str]) -> bool:
    """Check if the pipeline should stop for this project.

    Returns True if the in-process AutopilotService for this project has
    requested a stop (via _stop_events, keyed by project_id). A caller
    that couldn't resolve its own project_id gets False rather than
    guessing at some other project's stop signal.
    """
    if not project_id:
        return False
    event = _stop_events.get(project_id)
    if event is not None:
        try:
            # Non-blocking check
            return event.is_set()
        except Exception:
            pass
    return False


def _interruptible_sleep(seconds: int, project_id: Optional[str]) -> None:
    """Sleep up to `seconds`, but return early if _should_stop(project_id)
    flips during it. A plain time.sleep(seconds) here means a stop request
    (including AutopilotService.pause_for_restart(), see
    docs/SAFE_RESTART_DESIGN.md §3.3) is invisible to the loop for however
    long the sleep already had left -- up to DESIGN_QUEUE_SCAN_INTERVAL
    (60s) at the two call sites that use this. Checking every second
    instead makes that latency ~1s regardless of where in the sleep the
    stop request lands.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _should_stop(project_id):
            return
        time.sleep(max(0, min(1, deadline - time.time())))
