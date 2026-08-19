"""Regression test: get_pipeline_status must not block the event loop
loading persistent pipeline state.

Found live 2026-08-19, continuing the investigation into intermittent
/health timeouts under concurrent agent load: PersistentPipelineState.load()
does two synchronous DB round-trips and deserializes a JSON blob that
grows with every design ever processed (838+ processed-design hashes on
the live DB) -- called directly inside this async endpoint, that blocks
the event loop on every uncached poll. The dashboard hits this endpoint
every 3 seconds (frontend Autopilot.tsx's refetchInterval), and the 2s
cache above the call site limits how often it re-executes but not how
long each execution takes. Confirmed live: even after offloading the two
other blocking cost-recording call sites found in the same investigation,
/health -- a bare dict return with zero I/O of its own -- still hit the
full 8s curl timeout once and repeated 3-4s spikes several more times in
a 30-second sampling window.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_persistent_state_load_runs_off_the_event_loop_thread(db_manager, monkeypatch):
    from src.autopilot.orchestrator.state import PipelineState

    import src.mcp.autopilot.control_routes as routes

    # Force a cache miss and skip the run-specific-state branch so
    # execution falls through to the PersistentPipelineState.load() call
    # under test.
    monkeypatch.setattr(routes, "_cached", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_get_latest_run_dir", lambda: None)
    monkeypatch.setattr(routes, "_store", lambda *a, **k: a[1] if len(a) > 1 else None)

    mock_service = MagicMock()
    mock_service.status.return_value = {"running": False}
    monkeypatch.setattr(
        routes, "get_autopilot_service", lambda project_id: mock_service, raising=False
    )
    mock_registry = MagicMock()
    mock_registry.running.return_value = []
    monkeypatch.setattr(routes, "get_registry", lambda: mock_registry, raising=False)

    main_thread_id = threading.get_ident()
    call_thread_id = {}

    def _fake_load(self):
        call_thread_id["id"] = threading.get_ident()
        return PipelineState(), set()

    with patch(
        "src.autopilot.orchestrator.state.PersistentPipelineState.load", _fake_load
    ):
        await routes.get_pipeline_status(project_id=None, project_path=None)

    assert call_thread_id.get("id") is not None, "PersistentPipelineState.load was never called"
    assert call_thread_id["id"] != main_thread_id, (
        "PersistentPipelineState.load ran on the event loop's own thread -- "
        "it must run in the executor's thread pool instead"
    )
