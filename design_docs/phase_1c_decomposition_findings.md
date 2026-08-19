# Phase 1c — `src/mcp/server.py` decomposition findings

Execution findings for `design_docs/phase_1c_server_decomposition.md`. Plan
approved and executed 2026-08-19.

## What was done

1. **Step 0 (hazard fix).** Deleted the duplicate, dead rate-limit block
   (`_rate_limit_store`/`RATE_LIMIT_WINDOW`/`RATE_LIMIT_MAX`/`_check_rate_limit`
   defined twice; the first copy had zero callers and no lock). Verified the
   three live OAuth call sites (`oauth_register`, `oauth_authorize`,
   `oauth_token`) all resolve to the remaining, thread-safe copy.
2. **Mechanical split.** `scripts/split_server.py` (kept permanently, matching
   `scripts/split_manager.py`/`scripts/split_autopilot_api.py`'s precedent)
   derives every top-level symbol's span via `ast.parse`, assigns each to one
   of 9 target modules by name (not by stale line numbers, since the file was
   under concurrent edit), and verifies losslessness before writing anything.
   `src/mcp/server.py` is deleted; `src/mcp/server/` now holds `_shared.py`,
   `lifecycle.py`, `background_loops.py`, `agent_task_routes.py`,
   `task_admin_routes.py`, `oauth_routes.py`, `workflow_execution_routes.py`,
   `mcp_protocol.py`, `devtools_tools.py`, `__init__.py`.
3. **Route decorator rewrite.** Every extracted module that owns routes gets
   its own `router = APIRouter()`; `@app.<verb>(` decorator lines are
   rewritten to `@router.<verb>(` (matched anchored at column 0, so it can't
   touch an `app.` reference inside a function body). `lifecycle.py` is the
   deliberate exception — see the hazard below.
4. **God-function decomposition.** `create_task` (~601 lines) and
   `update_task_status` (~423 lines) are now named-step orchestrators. The
   steps live in two new internal modules — `_create_task_steps.py` (13
   steps) and `_update_task_status_steps.py` (9 steps) — because decomposing
   in place still left `agent_task_routes.py` over the ~800-line budget.
   `agent_task_routes.py` is now 307 lines; both orchestrators are under 150.
5. **`mcp_protocol.py` further split.** The 14 `_tool_*` handlers +
   `MCPToolSpec` + `MCP_TOOL_REGISTRY` (~1236 lines' worth combined with the
   rest of the file) moved into a new `_mcp_tool_registry.py` (828 lines).
   `mcp_protocol.py` re-exports `MCP_TOOL_REGISTRY`/`_MCP_TOOLS`/
   `MCP_TOOL_NAMES` for `tests/test_mcp_tool_registry.py`, the only consumer.
6. **Route-set guardrail.** `tests/test_server_route_set_guardrail.py` pins
   the exact 38 `(method, path)` pairs that existed before the split and
   asserts none are missing and none are registered more than once.

## Hazards found and how they were handled

**`@router.on_event` double-fires — verified empirically, not assumed.**
The obvious move for `lifecycle.py`'s `startup_event`/`shutdown_event` was to
give it a router too. A direct test (`APIRouter.on_event` + `app.include_router`)
showed the handler firing **twice** — a real FastAPI/Starlette quirk in the
installed version (0.141.1), not a hypothetical. Registering lifecycle this
way would have silently double-run startup/shutdown side effects (duplicate
DB writes, duplicate agent-restart notifications). `lifecycle.py` instead
keeps `@app.on_event(...)` bound directly to `app` (imported from `_shared`),
confirmed to fire exactly once. This is the kind of thing a plan document
can't catch from reading code alone — it had to be run.

**`FastAPI` 0.141.1's `app.routes` doesn't flatten `include_router(...)`
calls.** The route-set guardrail's first draft assumed `app.routes` held
`Route`/`WebSocketRoute` objects directly reachable after `include_router`.
In this FastAPI version, each included router becomes a lazy
`fastapi.routing._IncludedRouter` wrapper; the real routes live on
`.original_router.routes`. Found by direct introspection of a live `app`
object rather than trusting the assumption — the guardrail test now walks
through `_IncludedRouter` wrappers explicitly, with a comment recording why.

**Local-import shadowing produced a false circular-import.** The
cross-module dependency deriver (ast-walking each top-level function for
`Name(Load)` references to another module's top-level symbols) initially
flagged `lifecycle.py` as needing `workflow_execution_routes.resume_workflow`
and vice versa — a real cycle. The actual cause: `lifecycle.py`'s
`_resume_interrupted_workflows` has a *locally scoped* `from
src.autopilot.orchestrator.engine_client import resume_workflow` inside one
function body, shadowing the *different*, same-named `resume_workflow` route
handler for that function's whole scope (Python hoists local bindings). The
deriver was fixed to exclude any name locally bound (import or assignment)
anywhere in a top-level node's subtree before treating a reference as
cross-module. The fabricated cycle disappeared entirely once fixed — the real
dependency graph has none. The same false-positive pattern existed in
`task_admin_routes.py` (a dead top-level import of `resume_workflow`, never
actually reached because a local import always shadows it first) — left as
ruff-trimmed dead code, not a functional issue since nothing read it.

**`_check_for_duplicate_task`'s `get_config()` moved modules twice.** First
during the initial split (`agent_task_routes.py` inherited it), then again
during the god-function decomposition (`_create_task_steps.py` inherited it
from `agent_task_routes.py`). Each move broke a different set of
`@patch("...get_config", ...)` test targets — module-level imports bind once,
so "patch where it's used" tracks the *current* home of the call site, not
where it used to live. Both breaks were caught immediately by running
`tests/integration/test_task_deduplication_flow.py` after each move, not
assumed fixed.

**Two behavior-preservation bugs caught during the god-function
decomposition, before landing:**
- `_dispatch_agent_for_task` initially dropped the `enriched_data`/
  `dispatch_context` arguments to `AgentDispatchService.dispatch(...)`
  (typo'd a placeholder expression in their place while restructuring). Would
  have silently broken every real dispatch. Caught by re-reading the diff
  against the original before running anything, not by a test failure.
- `_maybe_fire_spec_gate`'s condition was written as `task.status == "done"`
  instead of the original's `request.status == "done"`. These diverge
  exactly when validation is spawned (`task.status` becomes `"under_review"`,
  but `request.status` is still `"done"`) — the original fires the spec gate
  in that case; the rewritten version would have silently stopped firing it.
  Caught the same way, before running anything.

## Production bugs this split surfaced and fixed

Three files outside `src/mcp/server/` had `from src.mcp.server import X`
where `X` now lives in a submodule — these are real runtime breakage, not
just test breakage, found by a full-repo grep after the split (not assumed
scoped to `tests/`):
- `src/mcp/agents_api.py` — `process_queue` (a function-local, deferred
  import inside `terminate_agent_endpoint`, to avoid a circular import) →
  `src.mcp.server.background_loops`.
- `src/mcp/memory_api.py` — `_touch_agent_activity` and 3× `process_queue`
  (all function-local deferred imports) → `_shared`/`background_loops`
  respectively.
- `src/mcp/autopilot/project_routes.py` — module-level `KNOWN_SYSTEM_AGENTS`,
  `verify_agent_authentication` (→ `_shared`) and `_check_rate_limit` (→
  `oauth_routes`).

## Test migration

~70 `@patch(...)`/`monkeypatch.setattr(...)`/import references across 21
test files were re-pointed to the correct submodule. The recurring judgment
call: a module-level import binds once (needs the patch target updated to
wherever the name is imported *now*), while a function-local/deferred import
re-resolves on every call (the patch target can be the name's *defining*
module, since the shadow always looks it up fresh). Getting this wrong either
silently no-ops a mock (module-level case) or raises `AttributeError` at
patch setup (function-local case, if the name never lived where patched) —
both were hit and fixed by actually running each affected test file, not by
inferring from the import style alone.

## Full-suite verification

Ran once, post-decomposition: **2,536 passed, 13 failed, 54 skipped**
(`pytest tests/`, 47m17s). All 13 failures confirmed unrelated to this split:

- **7 failures** (`test_phase_advancement_sweep.py` ×5,
  `test_background_queue_processor.py` ×2) trace to the same root cause:
  both files do `server.server_state.shutdown_event = asyncio.Event()` — a
  **direct attribute assignment** on the shared `ServerState` singleton, not
  a `monkeypatch.setattr(...)` call, so nothing restores it after the test.
  `server_state` is exactly as much a cross-test singleton in the new package
  as it was in the old flat file (same object, same lifetime) — this bug is
  independent of the split. Once one of these tests runs and `.set()`s its
  replacement Event, `background_loops.py`'s `while not
  server_state.shutdown_event.is_set():` loop guards see it permanently set
  for the rest of the process, so any *later* test exercising the real
  `background_queue_processor`/`background_phase_advancement_sweep` sees the
  loop exit immediately.
- **5 failures** (`test_safe_restart.py`, all of
  `TestNotifyAndPauseForRestart`) are downstream victims of the same
  poisoned singleton — `_notify_and_pause_for_restart` also reads
  `server_state.shutdown_event`.
- **1 failure** (`test_heal_orphaned_agent_branches.py::test_fast_forwards_orphaned_branch_with_no_live_worktree`)
  has no reference to `src.mcp.server` at all — a real-filesystem git
  worktree test, unrelated to this split by construction.

All 13 pass reliably run standalone or in small groups (confirmed directly,
not assumed) — the failures only manifest under the full suite's specific
ordering, consistent with a pre-existing, already-partially-documented class
of "order-dependent artefact" / "one fixture leaking global app state" this
codebase has been separately working down (see
`design_docs/remaining_test_failures.md`). Not fixed as part of this task —
out of scope for a decomposition, and the fix (give these tests their own
`ServerState`/event, or restore via `monkeypatch` instead of direct
assignment) belongs to whoever owns that test-hygiene effort.

Interestingly, fixing `tests/integration/test_task_deduplication_flow.py`'s
own `_shared.get_config` patch gap (see below) also fixed 4 failures that
`design_docs/remaining_test_failures.md` had previously catalogued as a
*separate*, unexplained "dispatch not happening" mystery (items #4-7 in that
doc) — they turned out to share the same root cause as the two genuine
split-caused regressions this task set out to fix, not a distinct dispatch-guard
bug as that doc's own speculation suggested.

## Genuine regressions found and fixed

**`tests/integration/test_task_deduplication_flow.py`** — `ServerState.initialize()`
lives in `_shared.py`, which does its own `from src.core.simple_config import
get_config` (module-level, bound once). The test's fixture patched only
`src.mcp.server.agent_task_routes.get_config`, missing `_shared`'s separate
binding — a direct consequence of the split (one `get_config` import became
several). Fixed by patching both. 6 of the file's 7 tests flipped from
failing to passing; the 7th (`test_deduplication_performance`) was already
passing.

## Not done as part of this task

- The 13 full-suite-only pre-existing test-isolation failures above (out of
  scope, documented for whoever picks up that effort).
- Any further consolidation of `task_admin_routes.py` (837 lines, accepted
  exception — see the plan-doc completion note).

## Verification commands

```
ruff check src/mcp/server/                     # clean except 3 pre-existing E402 in oauth_routes.py
pytest tests/test_server_route_set_guardrail.py  # route set pinned, no drops/dupes
pytest tests/  # 2536 passed, 13 failed (all pre-existing/unrelated), 54 skipped
```
