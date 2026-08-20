"""Guardrail test for the frontend API route set (Phase 0 gate).

Asserts that src.mcp.frontend.router exposes exactly 39 routes with the
exact {(method, path)} set from the doc §4.1 tables. The set is hardcoded,
not computed, so a missing or extra route fails immediately.

Baseline dropped from 42 to 40 in Phase 4 (docs/AUTOPILOT_REFACTOR_PLAN.md):
GET /api/agents and GET /api/agents/{agent_id}/output were deleted from
agent_routes.py -- src.mcp.agents_api registers identically-pathed routes
at import time (src/mcp/server/_shared.py), strictly before this frontend
router is included in lifecycle.py's startup_event, so FastAPI's
registration-order route matching meant these two were permanently
unreachable dead code, not a live, redundant surface.

Baseline dropped from 40 to 39, 2026-08-20 (SOLID review 1.18): POST
/api/workflows/{workflow_id}/stop deleted along with FrontendAPI.stop_workflow.
It force-failed every in-flight task, while the dashboard's actual "Stop"
button hits the differently-named-but-similarly-shaped
/api/workflow-executions/{workflow_id}/stop (a distinct implementation in
src/mcp/server/workflow_execution_routes.py) which pauses the workflow and
resets tasks to "pending" instead -- a silent, destructive-vs-resumable
divergence between two endpoints both plausibly meaning "stop". This route
had exactly one real caller in the whole codebase, the CLI (repointed to
the workflow-execution endpoint instead); the dashboard never called it.
"""

from unittest.mock import MagicMock

from src.mcp.frontend import create_frontend_routes, router

# ── 39-route baseline (hardcoded from doc §4.1 cluster tables) ────────────
EXPECTED_ROUTES = {
    # agent_routes.py — 1 route
    ("GET", "/api/phases/{phase_id}/agents"),
    # task_routes.py — 6 routes
    ("GET", "/api/tasks"),
    ("GET", "/api/tasks/{task_id}"),
    ("GET", "/api/tasks/{task_id}/full-details"),
    ("GET", "/api/blocked-tasks"),
    ("GET", "/api/blocked-tasks/{task_id}/blockers"),
    ("POST", "/api/sync-blocking-status"),
    # phase_routes.py — 15 routes
    ("GET", "/api/phases/{phase_id}/yaml"),
    ("PATCH", "/api/phases/{phase_id}"),
    ("POST", "/api/phases/{phase_id}/reset"),
    ("GET", "/api/phases/{phase_id}/prompt/versions"),
    ("GET", "/api/phases/{phase_id}/prompt/versions/{version}"),
    ("POST", "/api/phases/{phase_id}/prompt/versions"),
    ("POST", "/api/phases/{phase_id}/prompt/versions/{version}/publish"),
    ("POST", "/api/phases/{phase_id}/prompt/versions/{version}/restore"),
    ("GET", "/api/phases/{phase_id}/prompt/preview"),
    ("POST", "/api/phases/{phase_id}/prompt/preview"),
    ("GET", "/api/phases/{phase_id}/prompt/diff"),
    ("GET", "/api/tasks/{task_id}/prompt"),
    ("GET", "/api/tasks/{task_id}/prompt/overrides"),
    ("PUT", "/api/tasks/{task_id}/prompt/overrides"),
    ("DELETE", "/api/tasks/{task_id}/prompt/overrides"),
    # dashboard_routes.py — 17 routes
    ("GET", "/api/dashboard/stats"),
    ("GET", "/api/memories"),
    ("GET", "/api/graph"),
    ("GET", "/api/workflow"),
    ("GET", "/api/phases"),
    ("GET", "/api/workflow-definitions/{definition_id}/phases"),
    ("GET", "/api/guardian-analyses/{agent_id}"),
    ("GET", "/api/conductor-analyses"),
    ("GET", "/api/conductor-analyses/latest"),
    ("GET", "/api/steering-interventions"),
    ("GET", "/api/system-overview"),
    ("GET", "/api/results"),
    ("GET", "/api/results/{result_id}/content"),
    ("GET", "/api/results/{result_id}/validation"),
    ("GET", "/api/results/{result_id}/extra-files/{file_index}"),
    ("GET", "/api/results/{result_id}/download"),
    ("GET", "/api/results/{result_id}/validation/download"),
}


def _collect_routes(rtr, prefix=""):
    """Extract {(method, path)} from a FastAPI router, recursing into included routers."""
    routes = set()
    for route in rtr.routes:
        # Direct APIRoute
        if hasattr(route, "methods") and hasattr(route, "path"):
            for method in route.methods:
                if method == "HEAD":
                    continue
                routes.add((method.upper(), prefix + route.path))
        # Included router (from router.include_router())
        if hasattr(route, "original_router"):
            sub_prefix = prefix
            if hasattr(route, "include_context") and hasattr(route.include_context, "prefix"):
                sub_prefix = prefix + (route.include_context.prefix or "")
            routes.update(_collect_routes(route.original_router, sub_prefix))
    return routes


def _count_routes(rtr):
    """Count all APIRoute objects, recursing into included routers."""
    count = 0
    for route in rtr.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            count += 1
        if hasattr(route, "original_router"):
            count += _count_routes(route.original_router)
    return count


class TestFrontendAPIRoutesGuardrail:
    """Phase 0 gate: exact route count and path set must match baseline."""

    def test_route_count_is_39(self):
        count = _count_routes(router)
        assert count == 39, (
            f"Expected 39 routes on src.mcp.frontend.router, got {count}"
        )

    def test_exact_route_set_matches_baseline(self):
        actual = _collect_routes(router)
        assert actual == EXPECTED_ROUTES, (
            f"Route set mismatch.\n"
            f"  Missing from router: {EXPECTED_ROUTES - actual}\n"
            f"  Extra in router:     {actual - EXPECTED_ROUTES}"
        )

    def test_create_frontend_routes_returns_same_router(self):
        dummy_dm = MagicMock()
        dummy_am = MagicMock()
        result = create_frontend_routes(dummy_dm, dummy_am)
        # Must return the module-level aggregate router
        assert result is router

    def test_expected_set_has_exactly_39_entries(self):
        """Self-check: the hardcoded baseline itself has 39 entries."""
        assert len(EXPECTED_ROUTES) == 39
