"""Route-set guardrail for the Phase 1c src/mcp/server -> src/mcp/server/
package split (design_docs/phase_1c_server_decomposition.md).

Pins the exact (method, path) set that existed on the flat server.py before
the split, per exit criterion 2: a pinned set, not a bare count -- a count
assertion goes green again the moment a route is legitimately added, and
then stops guarding anything. This must catch a route that's silently
dropped (dead on `@router.` rewrite, or missing from an
app.include_router(...) call) as well as one that's silently added twice.
"""

from src.mcp.server import app

# The 38 (method, path) pairs registered on `app` immediately before the
# split (verified via `git show <pre-split-commit>:src/mcp/server.py` and a
# route-set diff against the split output -- see
# design_docs/phase_1c_server_decomposition_findings.md).
EXPECTED_ROUTES = {
    ("DELETE", "/api/tasks/{task_id}"),
    ("GET", "/"),
    ("GET", "/.well-known/oauth-authorization-server"),
    ("GET", "/.well-known/openid-configuration"),
    ("GET", "/api/queue_status"),
    ("GET", "/api/workflow-definitions"),
    ("GET", "/api/workflow-executions"),
    ("GET", "/api/workflow-executions/{workflow_id}"),
    ("GET", "/api/workflows"),
    ("GET", "/health"),
    ("GET", "/oauth/authorize"),
    ("GET", "/resources"),
    ("GET", "/resources/{resource_uri:path}"),
    ("GET", "/sse"),
    ("GET", "/tools"),
    ("GET", "/userinfo"),
    ("GET", "/validate_agent_id/{agent_id}"),
    ("POST", "/api/autopilot/recover"),
    ("POST", "/api/bump_task_priority"),
    ("POST", "/api/cancel_queued_task"),
    ("POST", "/api/restart_task"),
    ("POST", "/api/tasks/{task_id}/cancel"),
    ("POST", "/api/tasks/{task_id}/complete"),
    ("POST", "/api/tasks/{task_id}/pause"),
    ("POST", "/api/workflow-definitions"),
    ("POST", "/api/workflow-executions"),
    ("POST", "/api/workflow-executions/{workflow_id}/cancel"),
    ("POST", "/api/workflow-executions/{workflow_id}/complete"),
    ("POST", "/api/workflow-executions/{workflow_id}/resume"),
    ("POST", "/api/workflow-executions/{workflow_id}/stop"),
    ("POST", "/create_task"),
    ("POST", "/oauth/authorize"),
    ("POST", "/oauth/register"),
    ("POST", "/oauth/revoke"),
    ("POST", "/oauth/token"),
    ("POST", "/tools/execute"),
    ("POST", "/update_task_status"),
    ("WEBSOCKET", "/ws"),
}


def _route_registration_counts():
    """(method, path) -> how many times it's registered on `app`. Counting
    (not just membership) catches a route silently registered twice --
    e.g. a router accidentally `include_router`'d in both `__init__.py` and
    a leftover call elsewhere -- which membership alone would miss.

    This FastAPI version (0.141.1) doesn't flatten `include_router(...)`
    calls into individual Route objects on `app.routes` at include time --
    each becomes a `fastapi.routing._IncludedRouter` wrapper, and the real
    `APIRoute`/`WebSocketRoute` objects live on its `.original_router.routes`
    instead. Verified directly (introspecting a live `app.routes` entry) --
    do not assume `app.routes` is already flat without checking against the
    installed version, the way an older FastAPI (or this same file's own
    stale assumption, caught here) would have it.
    """
    from collections import Counter

    from fastapi.routing import _IncludedRouter
    from starlette.routing import WebSocketRoute

    counts: Counter = Counter()

    def _tally(route):
        path = getattr(route, "path", None)
        if path is None:
            return
        if isinstance(route, WebSocketRoute):
            counts[("WEBSOCKET", path)] += 1
            return
        for method in getattr(route, "methods", None) or ():
            if method == "HEAD":
                continue
            counts[(method, path)] += 1

    for route in app.routes:
        if isinstance(route, _IncludedRouter):
            for sub in route.original_router.routes:
                _tally(sub)
        else:
            _tally(route)
    return counts


def test_route_set_matches_pre_split_baseline():
    counts = _route_registration_counts()
    missing = {r for r in EXPECTED_ROUTES if counts[r] == 0}
    assert not missing, f"routes dropped by the server package split: {sorted(missing)}"


def test_no_expected_route_is_registered_more_than_once():
    counts = _route_registration_counts()
    duplicated = {r: n for r in EXPECTED_ROUTES if (n := counts[r]) > 1}
    assert not duplicated, f"routes registered more than once: {duplicated}"
