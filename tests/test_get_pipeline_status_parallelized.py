"""Regression test: get_pipeline_status's independent DB-bound resolvers
(state, queue_depth, last_event, active_agents, running_project,
review_mode) must run concurrently via asyncio.gather, not as a chain of
sequentially-awaited run_in_executor calls.

Found live diagnosing a user report that the Autopilot page's hero
"Pipeline Status" card visibly populated its real data last, after every
other page element, on a hard page refresh. Real network-waterfall
diagnostics showed /autopilot/status taking 8+ seconds on a project with
substantial workflow/task history -- 14-40x slower than sibling endpoints
polled in the same batch -- while Autopilot.tsx polls this endpoint every
3s, shorter than its own 2s cache TTL, meaning nearly every poll paid the
full serialized cost of 6+ separate thread-pool round trips, each opening
and closing its own DB session.

This test proves the fix's actual property -- concurrency, not just
correctness -- by making two independent resolvers each take a fixed,
measurable delay and asserting the endpoint's total wall-clock time is
close to ONE delay period, not the sum of both. It fails against the
pre-fix sequential-await code (total time ~= sum of delays) and passes
against the gather-based fix (total time ~= max of delays).
"""

import asyncio
import time
from unittest.mock import MagicMock

import pytest

_DELAY = 0.3


@pytest.mark.asyncio
async def test_independent_resolvers_run_concurrently_not_sequentially(
    db_manager, monkeypatch, tmp_path
):
    import src.mcp.autopilot.control_routes as routes
    from src.autopilot.orchestrator.state import PipelineState

    # Force cache misses for the whole-response cache and the state cache
    # so execution reaches the resolvers under test.
    monkeypatch.setattr(routes, "_cached", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_store", lambda *a, **k: a[1] if len(a) > 1 else None)

    # No project_id -> get_registry().running() drives service_status;
    # empty means running=False and no DB-bound per-service name lookups.
    mock_registry = MagicMock()
    mock_registry.running.return_value = []
    monkeypatch.setattr(routes, "get_registry", lambda: mock_registry, raising=False)

    # _resolve_state's delay: PersistentPipelineState.load runs in the
    # executor's thread pool, so a real sleep here occupies a worker
    # thread without blocking the event loop -- exactly like the real
    # slow DB round-trip it stands in for.
    def _slow_load(self):
        time.sleep(_DELAY)
        return PipelineState(), set()

    monkeypatch.setattr(
        "src.autopilot.orchestrator.state.PersistentPipelineState.load", _slow_load
    )

    # _resolve_queue_depth's delay (project_id=None branch): filesystem
    # glob via _get_effective_queue_dir, independently patchable and
    # unrelated to the state resolver above -- proves TWO DIFFERENT
    # resolvers overlap, not just that one offloaded call is non-blocking.
    def _slow_queue_dir():
        time.sleep(_DELAY)
        return str(tmp_path)

    monkeypatch.setattr(routes, "_get_effective_queue_dir", _slow_queue_dir)

    start = time.monotonic()
    await routes.get_pipeline_status(project_id=None, project_path=None)
    elapsed = time.monotonic() - start

    assert elapsed < _DELAY * 1.7, (
        f"get_pipeline_status took {elapsed:.2f}s with two independent "
        f"{_DELAY}s resolvers -- expected them to run concurrently "
        f"(~{_DELAY}s total), not sequentially (~{_DELAY * 2}s total)"
    )
