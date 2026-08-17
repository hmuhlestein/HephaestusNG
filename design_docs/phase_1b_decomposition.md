# Phase 1b Decomposition Plan

**Status: ready for execution.** Written 2026-08-16, after both `backend_module_decomposition.md` targets shipped (`src/autopilot/orchestrator.py` → `orchestrator/` package, and `src/mcp/autopilot_api.py` → `src/mcp/autopilot/` package, the latter executed via `scripts/split_autopilot_api.py`). This document covers the four remaining god-objects `docs/AUTOPILOT_REFACTOR_PLAN.md` §3.2 named "Phase 1b" — sequenced after those two precisely because they proved the scripted-extraction methodology works, twice. That precondition is now satisfied.

## 1. Problem statement

`docs/AUTOPILOT_REFACTOR_PLAN.md` §3.2 flags four more oversized modules for the same treatment `backend_module_decomposition.md` gave `orchestrator.py`/`autopilot_api.py`:

1. **`src/mcp/api.py`** (3,225 lines, 42 routes) — no consistent `FrontendAPI`-vs-inline-closure delegation pattern.
2. **`src/agents/manager.py`'s `create_agent_for_task`** (~985 lines) — a god function with logic overlapping `restart_agent`'s.
3. **`src/monitoring/monitor.py`'s `MonitoringLoop`** (~3,500 lines) — fuses scheduling, 13 mechanical detectors, Guardian dispatch, orphan cleanup, and a diagnostic-agent state machine.
4. **`src/services/task_completion_service.py`** (1,125 lines, 11 static methods) — memory persistence, verification hard floors, ticket auto-creation, spec-gate firing, validator spawning, git commit/link.

Each was researched independently and in full against the live codebase on 2026-08-16 (not carried over from the parent plan's draft estimates, several of which turned out to be stale — see each section's own freshness check). The four are independent of each other (no shared files, no ordering dependency between them) except where noted in §5.

## 2. What already exists (don't recreate)

- **`backend_module_decomposition.md`** — the methodology template. §3.3's scripted-extraction approach (`ast.parse()`-based line-range extraction, byte-identical reassembly verification, import-superset-then-`ruff F401`-trim) is proven twice now and should be reused here, not reinvented.
- **`scripts/split_autopilot_api.py`** — a working, second real-world example of the scripted approach (the first, for `orchestrator.py`, predates this file and wasn't itself scripted the same way — this is the reusable template).
- **`docs/AUTOPILOT_REFACTOR_PLAN.md`** — the parent plan. §3.2's table is this document's origin; §3.3's exit criteria (route-count/path-set guardrail tests, full call-site sweep including `tests/`, live smoke check) apply to every target below unchanged.
- **The two prior splits' own retrospectives**, folded into `backend_module_decomposition.md`'s "Key Deviations" section and `AUTOPILOT_REFACTOR_PLAN.md`'s Exception-1/2 status notes — see §3 below for what carries forward.

## 3. Lessons from the two prior splits — apply these to all four targets below

These are not repeated per-section below; they're cross-cutting and each target's own research already checked whether they apply.

1. **Test files are a first-class part of the call-site migration, not an afterthought.** The `orchestrator.py` split's own production call-site migration (`a2905e8`) updated production code only and missed test files entirely — 39 stale `@patch(...)` targets and imports in `tests/test_advance_phases.py` alone, found only in a later review. Every target section below includes an explicit, swept (not guessed) test call-site table.
2. **Patch targets must point at where a name is *looked up*, not where it's *defined*.** Two distinct failure modes exist, both encountered on the prior splits:
   - A **module-level** `from X import name` binds a stale reference at import time — patching `X.name` afterward has no effect on the importer; patch the *importer's* module instead. (This broke `test_phase0_idempotency.py`'s `_create_integration_worktree` patches on the `orchestrator.py` split.)
   - A **function-scoped** `from X import name` (re-executed on every call) re-resolves fresh each time — patching the *importer's* module has no effect; patch `X` (the defining module) instead. (This is `task_completion_service.py`'s `fire_spec_gate_if_ready` retargeting, §4.4 below — the exact mirror image of the first case.)
   
   Check which import style each call site actually uses before writing the patch-retarget table; don't assume either direction by default.
3. **`DatabaseManager()` called with no arguments silently bypasses `HEPHAESTUS_TEST_DB`.** Its default parameter is the literal string `"hephaestus.db"`, not `None` — only `DatabaseManager(None)` checks the env var, matching `get_db()`'s already-correct pattern. All 16 known instances of this bug were found and fixed 2026-08-16 (commits `df8ce2b`, `105589c`); if any new `DatabaseManager()` bare call is encountered while executing this plan (extraction sometimes surfaces code nobody had read closely in a while), fix it to `DatabaseManager(None)` in the same commit, don't defer it.
4. **A "lower risk" or "smaller" characterization in the parent plan is not guaranteed to hold** — both `MonitoringLoop` (71% bigger than estimated) and `create_agent_for_task`'s duplication-with-`restart_agent` (smaller and messier than "duplicated logic" implied) turned out to need correcting once actually read. Trust this document's own line ranges and findings over the parent plan's §3.2 table, which is now superseded by this one for these four targets.
5. **Byte-identical / behavior-preserving verification is not optional.** Every extraction here follows the same zero-behavior-change default `AUTOPILOT_REFACTOR_PLAN.md` §3 states for six of the orchestrator's eight submodules — these four targets get the same default (no exceptions carved out here, unlike `worktree_integration.py`/`phase_transitions.py` were). If an extraction surfaces a live bug (several are logged below), log it for Phase 3 — don't fix it inline.

## 4. Design

### 4.1 `src/mcp/api.py` → `src/mcp/frontend/` package (shared module + 4 route files)

**Freshness check (2026-08-16, `ast.parse()` against the live file, not carried over from `AUTOPILOT_REFACTOR_PLAN.md` §3.2's draft table):** `src/mcp/api.py` is **3,225 lines, 42 routes** — this matches the draft table's baseline exactly, zero drift. Unlike `orchestrator.py` and `autopilot_api.py` (both of which grew between verification passes), this file has not moved since the plan draft was written. Two independent verifications: a grep for def/class/route-decorator lines, and a small `ast.parse()` script walking `create_frontend_routes`'s body for `@router.<method>(...)`-decorated nested functions, both return exactly 42. The two known-bug line numbers the plan cites elsewhere (`src/mcp/api.py:2174`, `:3025` for the agent-termination `terminated_at` gaps) also match the live file exactly (`reset_phase`'s `agent.status = "terminated"` at line 2174, `stop_workflow`'s at line 3025) — a second independent confirmation of no drift.

**Structural finding not captured by the plan draft's one-line description, and the single most consequential fact for this split:** `src/mcp/api.py` is **not shaped like `autopilot_api.py`**. It is exactly two top-level symbols:

- `class FrontendAPI` (lines 35-2786): one monolithic class, 38 methods (4 private helpers + 34 business-logic methods), holding **all** route business logic.
- `def create_frontend_routes(db_manager, agent_manager, phase_manager=None)` (lines 2793-3225): a **factory function** that instantiates `FrontendAPI` into a module-level global (`frontend_api = None` declared at line 2790, rebound via `global frontend_api` inside the factory at line 2799-2800), then defines **all 42 routes as nested closures inside itself**, decorated with `@router.get/post/put/patch/delete(...)`, most of which are one-line delegations (`return await frontend_api.<method>(...)`).

`autopilot_api.py`'s 63 routes were already top-level `@router.<method>`-decorated functions at module scope. `api.py`'s routes are not — every one of them lives inside a factory-function closure, and the module-level `frontend_api` name they reference is `None` until `create_frontend_routes` runs. Splitting this file therefore requires one extra mechanical step `autopilot_api.py`'s split didn't: **un-nesting each route closure into a top-level function** in its target module, decorated the same way, referencing the shared `frontend_api` instance via a module-qualified lookup (`_shared.frontend_api`, not a bare `from ._shared import frontend_api`) — the exact `FEATURES_DIR` mutable-global lesson `backend_module_decomposition.md` §3.2 already learned on the `autopilot_api.py` split, reapplied here for `frontend_api`. This is *not* optional: the closures currently work only because Python looks up `frontend_api` as a global at *call* time, after `create_frontend_routes` has rebound it — a plain `from src.mcp.frontend._shared import frontend_api` in a route file would bind the stale `None` at import time and every route would immediately fail with `AttributeError: 'NoneType' object has no attribute '...'`.

#### Route clusters — verified against the live file, refined from the plan's draft

The plan's draft description reads: *"By route cluster, mirroring `autopilot_api.py`'s split shape: agent-read/control routes, task-read/control routes, phase-definition/prompt-version-editor routes (the largest cluster — 15 routes), dashboard/monitoring-read routes."* This 4-cluster shape holds up against the live file, and the "15 routes" figure for the phase/prompt cluster is **exactly reproducible** — but only with two placements the draft doesn't spell out: `GET /phases/{phase_id}/agents` (`get_phase_agents`) sorts with the **agent**-read cluster, not the phase cluster (it lists agents, not phase definitions), and `GET /workflow-definitions/{definition_id}/phases` (`get_definition_phases`) sorts with the **dashboard**-read cluster (it's workflow-definition browsing, not live phase-execution state or prompt editing). With those two placements, the four clusters are 4 + 6 + 15 + 17 = 42, exactly.

**1. `agent_routes.py` — agent-read/control (4 routes).**

| Method | Path | Route closure name | Current lines | Delegates to |
|---|---|---|---|---|
| GET | `/agents` | `get_agents` | 2818-2821 | `frontend_api.get_agents` (317-429) — **dead code**, see below |
| GET | `/agents/{agent_id}/output` | `get_agent_output` | 2823-2826 | `frontend_api.get_agent_output` (431-438) — **dead code**, see below |
| GET | `/phases/{phase_id}/agents` | `get_phase_agents` | 3089-3092 | `frontend_api.get_phase_agents` (2221-2252) |
| POST | `/workflows/{workflow_id}/stop` | `stop_workflow` | 2995-3071 | **inline**, 77 lines — extracted to `FrontendAPI.stop_workflow` first, see below |

**Dead-code confirmation (verified live, not just carried from the plan):** `src/mcp/agents_api.py` (`router = APIRouter(tags=["agents"])`, absolute paths, no prefix) already registers `GET /api/agents` (line 180) and `GET /api/agents/{agent_id}/output` (line 295). `src/mcp/server.py` includes `agents_router` at line 147, **before** `api_router` (from `create_frontend_routes`) is built and included at line 848-849. FastAPI resolves routes in registration order, so `agents_api.py`'s handlers win for both paths and `FrontendAPI.get_agents`/`get_agent_output` (and their route closures) are permanently unreachable — confirming `AUTOPILOT_REFACTOR_PLAN.md`'s existing characterization exactly. This split is a pure move, not a bug fix (per §7's out-of-scope rule) — the dead routes and their `FrontendAPI` methods still move verbatim into `agent_routes.py`/`_shared.py`; deleting them is a separate, already-logged item, not part of this decomposition.

**2. `task_routes.py` — task-read/control (6 routes).**

| Method | Path | Route closure name | Current lines | Delegates to |
|---|---|---|---|---|
| GET | `/tasks` | `get_tasks` | 2807-2816 | `frontend_api.get_tasks` (237-315) |
| GET | `/tasks/{task_id}` | `get_task` | 2887-2890 | `frontend_api.get_task` (883-921) |
| GET | `/tasks/{task_id}/full-details` | `get_task_full_details` | 2892-2895 | `frontend_api.get_task_full_details` (923-1212) |
| GET | `/blocked-tasks` | `get_blocked_tasks` | 2980-2983 | `frontend_api.get_blocked_tasks` (1980-1989) |
| GET | `/blocked-tasks/{task_id}/blockers` | `get_task_blocker_details` | 2985-2988 | `frontend_api.get_task_blocker_details` (1991-2009) |
| POST | `/sync-blocking-status` | `sync_blocking_status` | 2990-2993 | `frontend_api.sync_blocking_status` (2011-2020) |

**3. `phase_routes.py` — phase-definition/prompt-version-editor (15 routes, largest cluster, confirmed).**

| Method | Path | Route closure name | Current lines | Delegates to |
|---|---|---|---|---|
| GET | `/phases/{phase_id}/yaml` | `get_phase_yaml` | 2882-2885 | `frontend_api.get_phase_details` (863-881) |
| PATCH | `/phases/{phase_id}` | `update_phase` | 3075-3078 | `frontend_api.update_phase` (2024-2105) |
| POST | `/phases/{phase_id}/reset` | `reset_phase` | 3080-3087 | `frontend_api.reset_phase` (2107-2219) |
| GET | `/phases/{phase_id}/prompt/versions` | `get_phase_prompt_versions` | 3094-3097 | `frontend_api.get_phase_prompt_versions` (2254-2298) |
| GET | `/phases/{phase_id}/prompt/versions/{version}` | `get_phase_prompt_version` | 3099-3102 | `frontend_api.get_phase_prompt_version` (2300-2331) |
| POST | `/phases/{phase_id}/prompt/versions` | `create_phase_prompt_version` | 3104-3107 | `frontend_api.create_phase_prompt_version` (2333-2455) |
| POST | `/phases/{phase_id}/prompt/versions/{version}/publish` | `publish_phase_prompt_version` | 3109-3112 | `frontend_api.publish_phase_prompt_version` (2457-2501) |
| POST | `/phases/{phase_id}/prompt/versions/{version}/restore` | `restore_phase_prompt_version` | 3114-3117 | `frontend_api.restore_phase_prompt_version` (2503-2594) |
| GET | `/phases/{phase_id}/prompt/preview` | `get_phase_prompt_preview` | 3119-3132 | `frontend_api.get_phase_prompt_preview` (2596-2612) |
| POST | `/phases/{phase_id}/prompt/preview` | `post_phase_prompt_preview` | 3134-3190 | **inline**, 57 lines — see note below |
| GET | `/phases/{phase_id}/prompt/diff` | `get_phase_prompt_diff` | 3192-3197 | `frontend_api.get_phase_prompt_diff` (2614-2656) |
| GET | `/tasks/{task_id}/prompt` | `get_task_prompt` | 3199-3208 | **inline**, calls `assemble_task_prompt` from `src.prompts.assembler` directly — see note below |
| GET | `/tasks/{task_id}/prompt/overrides` | `get_task_prompt_overrides` | 3210-3213 | `frontend_api.get_task_prompt_overrides` (2658-2681) |
| PUT | `/tasks/{task_id}/prompt/overrides` | `set_task_prompt_overrides` | 3215-3218 | `frontend_api.set_task_prompt_overrides` (2683-2769) |
| DELETE | `/tasks/{task_id}/prompt/overrides` | `clear_task_prompt_overrides` | 3220-3223 | `frontend_api.clear_task_prompt_overrides` (2771-2786) |

Note the two `/tasks/{task_id}/prompt...` routes belong here, not in `task_routes.py` — they're part of the prompt-assembly/override system, not task CRUD, matching the cluster's actual theme ("prompt-version-editor") over its most common URL prefix.

**`post_phase_prompt_preview` also carries a known bug `AUTOPILOT_REFACTOR_PLAN.md` already logs** (Phase 3 Tier 2 item 14: hardcoded `DatabaseManager("hephaestus.db")` at line 3141 instead of reusing `frontend_api.db_manager`, silently bypassing `HEPHAESTUS_TEST_DB` isolation the same way the 16 sites §3 point 3 above found were). **Do not fix it in this split** — it's already a tracked Phase 3 item; moving it verbatim (bug included) into `phase_routes.py` is correct per the zero-behavior-change rule.

**4. `dashboard_routes.py` — dashboard/monitoring-read (17 routes).**

| Method | Path | Route closure name | Current lines | Delegates to |
|---|---|---|---|---|
| GET | `/dashboard/stats` | `get_dashboard_stats` | 2802-2805 | `frontend_api.get_dashboard_stats` (145-235) |
| GET | `/memories` | `get_memories` | 2828-2836 | `frontend_api.get_memories` (440-497) |
| GET | `/graph` | `get_graph_data` | 2838-2841 | `frontend_api.get_graph_data` (499-706) |
| GET | `/workflow` | `get_workflow` | 2843-2846 | `frontend_api.get_workflow_info` (708-856) |
| GET | `/phases` | `get_phases` | 2848-2851 | `frontend_api.get_phases` (858-861) — itself calls `self.get_workflow_info` |
| GET | `/workflow-definitions/{definition_id}/phases` | `get_definition_phases` | 2853-2880 | **inline**, 28 lines, queries `WorkflowDefinition` directly |
| GET | `/guardian-analyses/{agent_id}` | `get_guardian_analyses` | 2897-2902 | `frontend_api.get_guardian_analyses` (1214-1268) |
| GET | `/conductor-analyses` | `get_conductor_analyses` | 2904-2907 | `frontend_api.get_conductor_analyses` (1270-1318) |
| GET | `/conductor-analyses/latest` | `get_latest_conductor_analysis` | 2909-2912 | `frontend_api.get_latest_conductor_analysis` (1320-1323) — calls `self.get_conductor_analyses` |
| GET | `/steering-interventions` | `get_steering_interventions` | 2914-2919 | `frontend_api.get_steering_interventions` (1325-1355) |
| GET | `/system-overview` | `get_system_overview` | 2921-2924 | `frontend_api.get_system_overview` (1357-1483) — calls `self.get_latest_conductor_analysis`, `self.get_steering_interventions`, `self.get_workflow_info` |
| GET | `/results` | `get_results` | 2926-2945 | `frontend_api.get_results` (1485-1717) — calls `self._deduplicate_results` |
| GET | `/results/{result_id}/content` | `get_result_content` | 2947-2950 | `frontend_api.get_result_content` (1719-1742) |
| GET | `/results/{result_id}/validation` | `get_result_validation` | 2952-2955 | `frontend_api.get_result_validation` (1744-1850) |
| GET | `/results/{result_id}/extra-files/{file_index}` | `get_extra_file_content` | 2957-2960 | `frontend_api.get_extra_file_content` (1852-1915) |
| GET | `/results/{result_id}/download` | `download_result_markdown` | 2962-2969 | `frontend_api.download_result_markdown` (1917-1943) |
| GET | `/results/{result_id}/validation/download` | `download_validation_report` | 2971-2978 | `frontend_api.download_validation_report` (1945-1978) |

**`get_task_prompt` and `get_definition_phases` are also un-delegated inline logic, like `stop_workflow`, but the plan's explicit callout names only `stop_workflow` for extraction — leave the other two as inline closures-turned-top-level-functions.** This is a real, deliberate asymmetry, not an oversight to "fix while in there" (per this repo's minimal-touch rule): `stop_workflow` and `reset_phase` are the two routes `AUTOPILOT_REFACTOR_PLAN.md`'s own agent-termination-primitive work (its §4.2, not this document's) will need to patch symmetrically in Phase 2 — both terminate agents and both currently have the same `terminated_at`-missing bug. Giving `stop_workflow` the same `FrontendAPI`-method shape `reset_phase` already has is what makes that future consolidation a same-shape edit in both places; `get_definition_phases` and `get_task_prompt` aren't part of that consolidation and have no such forcing function. Flag this inconsistency in review so a future pass doesn't "clean it up" unprompted; don't act on it here.

#### The `stop_workflow` / `reset_phase` deviation — verified, and the exact extraction shape

Confirmed by reading both directly: `reset_phase` (`FrontendAPI.reset_phase`, lines 2107-2219) is `async def reset_phase(self, phase_id: str, target_status: str, force: bool = False) -> Dict[str, Any]`, using `self.db_manager`/`self.agent_manager`-style access throughout, with its route closure (3080-3087) doing only request-body parsing before delegating. `stop_workflow`'s current route closure (2995-3071, 77 lines) has no such split: it's the entire implementation — session open/query/mutate/commit/error-handling — written directly in the closure, referencing `frontend_api.db_manager` (the global, not `self`) since there's no enclosing instance.

**Extraction, to be done as its own step before the module split** (matching the plan's phrasing "first"):

1. Add `async def stop_workflow(self, workflow_id: str) -> Dict[str, Any]:` to `FrontendAPI` (natural placement: immediately after `sync_blocking_status`, i.e. after current line 2020, keeping it adjacent to `reset_phase` which follows at 2107 — or directly before `reset_phase` for cluster-locality; either is fine, it's an internal reordering with zero behavior change).
2. Move the current closure body (2998-3069) into that method verbatim, with exactly one search-and-replace: `frontend_api.db_manager` → `self.db_manager` (the only external-state reference in the body; `Agent`, `Task`, `Workflow`, `HTTPException`, `datetime` are all already imported at module top-of-file and will be in `_shared.py`'s import block after the split — no other changes).
3. The route closure becomes a two-line delegation, identical in shape to `reset_phase`'s:
   ```python
   @router.post("/workflows/{workflow_id}/stop")
   async def stop_workflow(workflow_id: str):
       """Stop a running workflow and terminate its agents."""
       return await _shared.frontend_api.stop_workflow(workflow_id)
   ```
4. This is a same-file, zero-net-line-count-change refactor (function body relocates from a closure to a method) that should land as its own commit, **before** the module-split commits — per the plan's own sequencing ("extract... first so the split has a clean per-route unit to move").

#### Proposed module layout

```
src/mcp/frontend/
├── __init__.py          # aggregator: create_frontend_routes(...) (same signature — see below)
├── _shared.py            # FrontendAPI (all 38 methods) + the frontend_api global + shared imports
├── agent_routes.py        # 4 routes
├── task_routes.py         # 6 routes
├── phase_routes.py        # 15 routes
└── dashboard_routes.py     # 17 routes
```

Names chosen to match `src/mcp/autopilot/`'s existing convention exactly (`_shared.py`, `<cluster>_routes.py`, aggregator `__init__.py`) — no new naming pattern introduced. `frontend` (not `api`) as the package name because `api.py`'s own docstring calls it "API endpoints for the frontend dashboard" and `FrontendAPI` is already the class's own name; `frontend` reads more specifically than a generic `api` directory sitting next to `autopilot`, `agents_api.py`, `tickets_api.py`.

**`_shared.py` contents:** the entire `FrontendAPI` class (lines 35-2786, unmodified except for the `stop_workflow` insertion above) plus the module-level `frontend_api = None` global (line 2790) plus the full top-of-file import block (lines 3-28) plus `logger` (line 30). Function-scoped imports already inside individual methods travel with their host method's body unchanged, per this repo's established local-import convention — don't hoist them to module level as a drive-by cleanup.

**Why `FrontendAPI` stays one class, not four (a real design choice, not the only option):** `api.py`'s 38 `FrontendAPI` methods have real intra-class call edges, but **all of them stay inside the dashboard cluster** — there is no cross-cluster method call anywhere in the class. That means splitting `FrontendAPI` itself along the same 4-cluster boundary (mixins, one per cluster) is mechanically possible and would mirror the route split more tightly, but adds an indirection layer (MRO resolution across 4 files) for a class whose only external consumer is `create_frontend_routes`/the route closures. Given zero cross-cluster call edges exist to justify mixin boundaries, and per this repo's "no abstractions for single-use code" principle — **keep `FrontendAPI` as one class in `_shared.py`.**

**Each `*_routes.py` file:** its own `router = APIRouter()`, its own set of top-level `@router.<method>(...)`-decorated functions (converted from the current nested closures), each importing `from src.mcp.frontend import _shared` and calling `_shared.frontend_api.<method>(...)` — never a bare `frontend_api` name, and never `from ._shared import frontend_api`.

**`__init__.py` (aggregator):**
```python
from fastapi import APIRouter

from . import _shared
from .agent_routes import router as agent_router
from .task_routes import router as task_router
from .phase_routes import router as phase_router
from .dashboard_routes import router as dashboard_router

router = APIRouter(prefix="/api", tags=["Frontend API"])
router.include_router(agent_router)
router.include_router(task_router)
router.include_router(phase_router)
router.include_router(dashboard_router)


def create_frontend_routes(db_manager, agent_manager, phase_manager=None):
    """Configure the shared FrontendAPI instance and return the aggregate router."""
    _shared.frontend_api = _shared.FrontendAPI(db_manager, agent_manager, phase_manager)
    return router
```
This preserves `create_frontend_routes(db_manager, agent_manager, phase_manager)`'s exact signature and return value, so **`src/mcp/server.py`'s one call site needs zero changes beyond the import path**.

#### Import dependency analysis — circular-import risk

**None found, and structurally none is possible under the proposed layout.** Every one of the four route files imports only from `_shared`; `_shared.py` imports nothing from any route file; the aggregator `__init__.py` imports `_shared` plus all four route files, never the reverse. This is a strict two-level DAG — cleaner than `autopilot_api.py`'s split, which needed one cross-route-file edge (`project_routes.py → feature_routes.py`).

**What `_shared.py` must hold, beyond `FrontendAPI` itself:** the `frontend_api` mutable global (this split's `FEATURES_DIR`-equivalent) and nothing else — there are no Pydantic request/response models, per-project directory caches, or response-cache layer in `api.py` the way `autopilot_api.py`'s `_shared.py` needed. `api.py`'s routes take raw `Dict[str, Any]` request bodies.

#### External call sites (production and test — both swept, not just production)

**Production:** exactly one call site outside `api.py` itself.

| Importing file | Symbol imported | New home | Change needed |
|---|---|---|---|
| `src/mcp/server.py:40` | `create_frontend_routes` | `src/mcp/frontend/__init__.py` | `from src.mcp.api import create_frontend_routes` → `from src.mcp.frontend import create_frontend_routes` |
| `src/mcp/server.py:848` | (calls `create_frontend_routes(...)`) | — | **no change** — call-site arguments and return value both unchanged |

No other production file imports from `src.mcp.api` — confirmed by a repo-wide grep returning only `server.py:40`.

**Tests — swept explicitly per lesson 1 in §3 above (test call sites are a first-class part of the migration):**

| Test file | What it references | New home | Change needed |
|---|---|---|---|
| `tests/test_frontend_api_workflow_selection.py:19` | `from src.mcp.api import FrontendAPI` | `src/mcp/frontend/_shared.py` | `from src.mcp.api import FrontendAPI` → `from src.mcp.frontend._shared import FrontendAPI` |
| `tests/test_frontend_api_workflow_selection.py:31` etc. | `FrontendAPI(db_manager=db, agent_manager=None)` instantiation, calls `.get_workflow_info(...)` | — | no change beyond the import line |

That is the **entire** test-side surface — confirmed by four independent sweeps (import grep, symbol-name grep, `@patch(...)` grep — zero results, no string-patch targets exist for this module at all — and a per-route-path grep across all 42 paths, finding no test hits any of them via `TestClient`).

#### Testing

**No dedicated route-level test coverage exists for `src/mcp/api.py`'s HTTP surface today.** `tests/test_frontend_api_workflow_selection.py` (4 test methods) exercises exactly one method — `FrontendAPI.get_workflow_info` — by instantiating `FrontendAPI` directly, never through `TestClient`/HTTP. **No route-count/path-set guardrail test exists for this router**, unlike `autopilot_api.py`'s route-count test. `AUTOPILOT_REFACTOR_PLAN.md`'s own Phase 0 (§2, item 1) already calls for this exact gap to be closed before any split starts. **This split cannot proceed under the parent plan's own Phase 0 gate until that guardrail test is written** — with zero existing coverage of the route surface, a guardrail test is the only thing that would catch a route silently failing to make it into the aggregator's `include_router()` chain. Recommended shape: assert `len(router.routes)` and the exact `{(method, path)}` set from `src.mcp.frontend.router` match a hardcoded baseline — the 42-row list in the four cluster tables above is that baseline, ready to hardcode directly.

**Live verification after the split:** `heph restart`, then hit at minimum one route per cluster (`GET /api/dashboard/stats`, `GET /api/tasks`, `GET /api/phases/{id}/prompt/versions`, `GET /api/system-overview`) to confirm the aggregator's wiring actually serves requests. Given the total absence of existing HTTP-level tests, this manual check carries more weight here than it did for `autopilot_api.py`'s split (which had 205 passing HTTP-level tests as a safety net going in).

---

### 4.2 `src/agents/manager.py` — `create_agent_for_task` × `restart_agent` (decompose + deduplicate)

**Verified against:** `src/agents/manager.py`, 3,631 lines, re-derived by direct read and grep on 2026-08-16 — not carried over from the plan's own numbers.

#### Freshness check

The plan's line estimates hold almost exactly:

| Symbol | Plan's claim | Actual (verified) | Body span |
|---|---|---|---|
| `create_agent_for_task` | starts 259, body to ~1243, ~970 lines | starts **259**, next sibling def (`_wait_for_shell_ready`) at **1245** | **259–1243** (985 lines, including 51-line docstring) |
| `restart_agent` | not line-ranged by the plan | starts **2257**, next sibling def (`get_agent_output`) at **2657** | **2257–2655** (399 lines) |
| `terminate_agent` | not in scope, but sibling to `AUTOPILOT_REFACTOR_PLAN.md`'s own §4.2 (agent-termination-primitive dedup phase, not this document's §4.2) | starts **1939**, next def (`_commit_wip_in_shared_worktree`) at **2210** | **1939–2209** (270 lines) |
| `_create_tmux_session` | referenced as a shared helper | **1281–1428** (used by both `create_agent_for_task` and `restart_agent`) | — |

One correction to the plan: `create_agent_for_task`'s recursive fallback-retry call (line 1135) and cleanup-on-failure block are inside a single `try/except` that wraps almost the entire method body (`try:` at 440, `except Exception as e:` at 1095) — the plan's phase list implies fallback-retry/cleanup is a late, separable step, but structurally the entire launch pipeline (worktree → prompt → env → launch → launch-failure detection → prompt delivery) sits *inside* that one try block, so any extraction has to either keep the whole pipeline inside one big `try`, or have each extracted step raise and let a single wrapping `try` in the caller (or a step-orchestrator) catch it. This matters for the split design below.

#### Internal structure of `create_agent_for_task` (line ranges as of this write-up)

| Phase | Lines | What it does |
|---|---|---|
| Signature + docstring | 259–310 | incl. the `assign_to_task` race-closing kwarg |
| Guard: `task is None` | 311–314 | raises `ValueError` |
| Guard: `git_commit_push` review-mode block | 316–341 | phase-scoped `PermissionError` if `AutopilotProject.review_mode` and phase name is `git_commit_push` |
| Guard: duplicate-active-agent check | 343–361 | queries `Agent.current_task_id == task.id AND status IN (working, idle)`; returns the existing agent instead of creating a second one |
| `agent_id` + `wt_mgr` init | 363–364 | `self._scoped_worktree_manager(task.workflow_id)` |
| Phase-config fallback derivation | 366–392 | pulls `phase_cli_tool/model/glm_token_env/thinking_level` + `fallback_cli_tool/model` from `Phase` row when caller didn't pass them |
| `cli_type` / `fallback_cli_tool` resolution | 394–408 | phase → global-default fallback chain |
| Stub `Agent` row insert (+ optional task claim) | 410–438 | commits a placeholder `Agent` **before** the slow worktree/tmux work — closes the "process dies mid-dispatch" race documented in the docstring |
| **`try:` block starts** | 440 | wraps everything below through 1093 |
| Gather worktree context | 441–444 | `self._gather_worktree_context(task)` |
| **Worktree resolution** | 446–500 | shared-worktree lookup (`.worktrees/` in `Workflow.working_directory`, fail loudly if missing) vs. isolated `wt_mgr.create_agent_worktree(...)` |
| System-prompt generation | 502–526 | resolves `phase_name`, calls `self.llm_provider.generate_agent_prompt(...)` |
| **Env/model resolution** | 528–588 | `cli_agent = get_cli_agent(cli_type)`; model resolution (phase → global → CLI default); `self._build_glm_env_vars(...)`; `self._resolve_mcp_timeout_ms(...)`; `HEPHAESTUS_*` env injection |
| Tmux session creation | 589–607 | `cli_agent.prepare_working_directory`; `self._ensure_codegraph_initialized`; phase output dir; `self._create_tmux_session(...)` |
| Phase-name/thinking-level + complexity classification | 609–697 | re-derives `phase_name`/`phase_order`; `thinking_level` resolution; complexity-adaptive downscaling (LLM call, cached per workflow) |
| **`session_id` generation** | 699–770 | deterministic session key from `(project_id, design_slug, phase_name, model)`, excluded for `validator/result_validator/diagnostic/arbitration` |
| Instructions file + pointer | 777–795 | `self._format_initial_message`, `self._write_task_instructions`, `self._build_instructions_pointer` |
| **Launch** | 796–841 | `cli_agent.get_launch_command(...)`; `self._export_env_vars_and_verify(...)`; echoes; `pane.send_keys(launch_command)` |
| Stub → full `Agent` row update | 843–878 | `session.merge(Agent(...))`, task assignment, `AgentLog` "created", commit |
| Wait + termination-race check | 880–973 | `asyncio.sleep(25)`; re-checks `Agent.status`/`Task.status`/`assigned_agent_id` for a termination or reassignment that happened *during* the sleep — **only in `create_agent_for_task`**, see below |
| Tmux-alive check | 975–980 | raises if session died during the wait |
| **Launch-failure detection** | 1008–1047 | `capture-pane -S -15` + two `re.search` calls: (a) the growing shared regex (`"command not found\|No such file or directory\|model.{0,60}not found"`), (b) a separate, Claude-specific `"Bypass Permissions mode"` check — **only in `create_agent_for_task`** |
| Confirmation-key dismissal | 1049–1055 | `cli_agent.post_launch_confirmation_keys()` |
| Goal command | 1057–1060 | `self._send_goal_command(...)` |
| **Prompt delivery** | 1062–1086 | `self._send_initial_prompt_with_retry(...)`, `self._record_cli_session(...)`, `self._verify_instructions_file_read(...)` |
| Return | 1088–1093 | ad hoc `AgentInfo` wrapper class (defined inline, twice — also at 969–973) |
| **`except Exception as e:`** | 1095–1243 | fallback-retry (recursive self-call with `fallback_cli_tool`) + cleanup-on-failure (kill tmux, mark agent terminated + task failed, session-limit → pause workflow) |

The plan's high-level phase list (guard checks → worktree resolution → prompt generation → env/model resolution → launch → launch-failure detection → prompt delivery → fallback-retry cleanup) is basically right, but it collapses three things that are actually distinct and independently reusable: (1) the **session_id generation block** (699–770) is its own ~70-line, non-trivial piece of logic, (2) **launch-failure detection is two independent checks**, not one, and (3) the entire pipeline after the stub-Agent-row commit is one giant `try` — there's no natural early-return boundary today; both funnel into the same fallback/cleanup handler.

#### Comparison against `restart_agent` (2257–2655)

`restart_agent` is not a "~970-line duplicate" of `create_agent_for_task` — it's **399 lines**, and a nontrivial fraction of that is restart-specific bookkeeping with no analog in the create path.

**Genuinely shared already (both call the same private helper methods — not duplicated source, just duplicated *call sites*):** `self._build_glm_env_vars`, `self._resolve_mcp_timeout_ms`, `self._create_tmux_session`, `self._write_task_instructions`/`_build_instructions_pointer`, `self._export_env_vars_and_verify`, `self._send_goal_command`, `self._send_initial_prompt_with_retry`, `self._record_cli_session`, `self._verify_instructions_file_read`, `cli_agent.get_launch_command(...)` call shape, the `HEPHAESTUS_*` env-var injection block (byte-for-byte identical structure).

So the previous two decompositions' "shared helper" pattern is *already applied* here — the plan's framing ("~460-line god function with logic duplicated in `restart_agent`," from the original SOLID review) undersells how much is already factored out. What's actually duplicated is the **orchestration/glue code that calls these helpers in the same order**, plus a few genuinely inline-duplicated blocks:

**Duplicated inline (near-identical source, not calling a shared helper) — real extraction targets:**
- **`session_id` generation**: create 699–770 (72 lines) vs. restart 2454–2500 (47 lines). Same shape, same excluded-agent-types set **except** restart's exclusion list is missing `"arbitration"` (create excludes `validator/result_validator/diagnostic/arbitration`; restart excludes only `validator/result_validator/diagnostic`) — a real discrepancy, not a copy-paste artifact to just merge silently; confirm with the arbitration design intent before unifying, since an arbitration agent should almost certainly never be restarted with a resumed session either.
- **Model resolution**: create 549–555 vs. restart 2336–2345. Same "global model only if `cli_type == default_cli_tool`" logic, but create resolves from `phase_cli_model`/`phase_cli_tool` (fresh phase lookup), restart resolves from `agent.cli_model`/`agent.cli_type` (the already-launched agent's own frozen values) — genuinely different inputs, same formula. A shared helper needs both as parameters, not one canonical source.
- **Phase-name (+ thinking-level, for restart) resolution via `Phase` query**: create 612–638 vs. restart 2409–2434 — same `task.phase_id.isdigit()` branch logic, different session-scoping boilerplate around it.
- **Phase-output-directory creation**: create 599–602 vs. restart 2437–2440 — identical `.hephaestus/<phase_name>` mkdir, one line different (restart guards on `restart_wd` being non-None).

**Not shared — genuinely restart-specific, needs to stay a caller branch, not a shared-helper parameter:**
- **Restart-loop cap** (2273–2291): `agent.restart_count >= 3` → terminate + fail task. No analog in create.
- **`restart_count` increment** (2293).
- **Working-directory resolution is fundamentally different, not just parameterized differently**: create either uses an existing shared worktree or **creates a new isolated worktree**; restart never creates anything — it resolves the **existing** worktree or falls back to `branch_manager.get_agent_branch_path(agent_id)` and raises nothing if both are absent (`restart_wd` just stays `None`, no `.worktrees/` existence check, no fail-loudly guard). This is the single largest genuine behavioral divergence — a shared `_resolve_worktree` step would need a `create_if_missing: bool` parameter, and the "fail loudly if a shared worktree is expected but absent" guard probably should NOT silently degrade to `None` for restart the way it does today, but that's a behavior change, not a refactor — flag for Phase 3, don't fold into this split.
- **Session-name suffixing**: `f"{prefix}_{agent_id[:8]}_r"` (restart) vs. `f"{prefix}_{agent_id[:8]}"` (create) — trivial but a required parameter.
- **System-prompt handling**: create generates a fresh prompt via an LLM call; restart reuses `agent.system_prompt` and just prepends a restart-context banner — no LLM call. A shared step can't unify these; restart's "prompt source" needs to be a parameter/hook.
- **Kill-existing-tmux-session step** (2304–2330): restart tears down its own prior session first; create has no prior session to kill.
- **`Agent` DB update shape**: create does a `session.merge(Agent(...))` full replace plus a separate `AgentLog` "created" entry; restart mutates the existing `agent` ORM object's fields in place plus an `AgentLog` "restarted" entry with different `details`.
- **No launch-failure detection at all in `restart_agent`.** This is not a duplicated-then-diverged check — restart has **zero** equivalent of create's 1008–1047 capture-pane regex checks. A relaunched CLI that fails the exact same way (missing binary, model-not-found, stuck permissions dialog) goes completely undetected on restart today. This is a real, live gap — the proposed `get_launch_rejection_patterns()` hook (below) should be designed as a genuinely shared step precisely because unifying it is what would close this gap, not because it's currently duplicated.
- **Termination-race re-check during the post-launch sleep** (create's 911–973): restart has no equivalent check after its own `asyncio.sleep(25)` before delivering the resume prompt — same class of gap as the missing launch-failure check.

#### Proposed split — named steps, with what's actually shared vs. caller-specific

Given the finding above that the whole pipeline runs inside one `try/except`, the cleanest shape is a set of step **methods** (not free functions — everything already depends on `self.db_manager`, `self.config`, `self.branch_manager`, `self.tmux_server`, `self.llm_provider`) called from two thin orchestrators (`create_agent_for_task`, `restart_agent`), each keeping its own `try/except` at the top level for fallback-retry / cleanup-on-failure (those two behaviors are genuinely different per caller, so they stay un-shared).

| Step | Proposed signature | Raw material (current lines) | Shared? |
|---|---|---|---|
| `_check_duplicate_active_agent` | `(self, task: Task) -> Optional[Agent]` | create 343–361 | **create-only** — restart has no analogous guard (it already knows its agent) |
| `_resolve_phase_config` | `(self, task: Task) -> PhaseConfig` (tool/model/glm_env/thinking_level/fallback\*) | create 366–392 | **create-only** — restart reads frozen `agent.cli_type`/`agent.cli_model` instead |
| `_resolve_worktree` | `(self, task: Task, wt_mgr: WorktreeManager, *, create_if_missing: bool) -> WorktreeResolution` | create 446–500 + restart 2376–2403 | **Shared with a real behavioral parameter.** Not a pure move — restart's silent-`None`-on-missing needs an explicit decision (keep as-is per zero-behavior-change default, or flag as a Phase 3 fix) rather than inheriting create's fail-loudly guard by accident |
| `_resolve_env_and_model` | `(self, cli_type, model_source, task, agent_id, label) -> Dict[str,str]` | create 528–588 + restart 2332–2369 | **Shared** — already calls the same two helpers; this step just unifies the surrounding glue |
| `_resolve_phase_name_and_thinking` | `(self, task, phase_thinking_override) -> Tuple[str, str, str]` | create 609–638 + restart 2409–2434 | **Shared** |
| `_resolve_session_id` | `(self, task, agent_type, phase_name, model) -> str` | create 699–770 + restart 2454–2500 | **Shared, but fix the `arbitration` exclusion-list mismatch first** (see above) as a separate, narrowly-scoped Phase 3 fix, not silently inside this move |
| `_prepare_launch_environment` | `(self, session_name, working_directory, env_vars, task, phase_name) -> TmuxSession` | create 589–607 + restart 2437–2444 | **Shared** |
| `_build_and_send_launch_command` | `(self, cli_agent, tmux_session, pane, *, system_prompt, task, model, thinking_level, phase_name, agent_id, session_id, working_directory, instructions_pointer, env_vars, label) -> LaunchResult` | create 796–841 + restart 2531–2561 | **Shared** |
| `_detect_launch_failure` | `(self, pane, cli_type, session_name) -> None` (raises on match) | create 1008–1047 | **New shared step — currently create-only, genuinely missing from restart (a real gap, not a dedup).** This is where `get_launch_rejection_patterns()` plugs in |
| `_deliver_initial_prompt` | `(self, pane, cli_agent, cli_type, message, agent_id, task_id, *, instructions_rel_path=None) -> None` | create 1049–1086 + restart 2609–2636 | **Shared** — already calls the shared prompt-delivery helpers; this step just unifies the confirmation-key loop + call ordering |
| `_check_termination_race` | `(self, agent_id, task_id) -> bool` | create 911–973 | **New shared step — currently create-only, real gap for restart** |
| Fallback-retry-on-primary-failure | recursive self-call | create 1099–1156 | **create-only**, no restart analog |
| Cleanup-on-failure | n/a | create 1157–1242 | **create-only** as written; restart's `except` block (2651–2655) is a two-line `log + rollback`, much thinner — **don't force these into one shared cleanup handler**, the failure semantics genuinely differ |

**Bottom line on shared-vs-caller-specific**: 8 of the ~13 steps above are cleanly shared once given the right parameters; 2 (`_check_duplicate_active_agent`, `_resolve_phase_config`) are genuinely create-only; the fallback-retry and cleanup-on-failure blocks are also create-only and should **not** be forced to also serve restart. This is messier than "one shared pipeline, two thin callers" — it's closer to "8 shared steps, 2 create-only pre-steps, 2 restart-only pre/post-steps, and 2 deliberately-not-unified failure handlers."

#### Launch-failure-detection / regex-growth issue

Confirmed at manager.py 1008–1047. Git history confirms the plan's characterization exactly: `e9c47f7` (still the tip of `manager.py`'s own file history) is precisely the commit that widened `"command not found|No such file or directory"` to add `"|model.{0,60}not found"`. There's also a **second, separate** check immediately after (1038–1047) for Claude Code's "Bypass Permissions mode" first-run dialog — hardcoded to Claude only, not part of the growing shared regex, but the same "detected here rather than handled polymorphically" shape.

**`CLIAgentInterface` precedent** (`src/interfaces/cli_interface.py`): `get_stuck_patterns()`/`get_health_check_pattern()` are both declared `@abstractmethod` — every concrete subclass is *required* to implement its own. This differs from the file's other polymorphic-hook pattern (`recovery_keystrokes`, `mcp_reconnect_instructions`, `fallback_model`, `get_tui_status_patterns`, `strip_tui_chrome`), which are all concrete on the base class with a safe empty/no-op default, explicitly commented "Concrete with a safe default so the monitor stays harness-agnostic; override per CLI (polymorphic)." `CLI_AGENTS` registry: `{"claude": ClaudeCodeAgent, "opencode": OpenCodeAgent, "droid": DroidAgent, "codex": CodexAgent, "pi": PiAgent, "swarm": SwarmCodeAgent}` — 6 CLIs.

**Which pattern fits `get_launch_rejection_patterns()`?** The current single shared regex mixes two genuinely different concerns that argue for the **safe-default** style (like `get_tui_status_patterns`), not the abstract-required style:
1. Generic shell-level rejection (`"command not found"`, `"No such file or directory"`) — fires identically for *any* CLI whose binary is missing from `PATH`; belongs on the base class as the default return value.
2. CLI-specific rejection wording (`"model.{0,60}not found"` — confirmed by the comment at manager.py:997-999 to be **pi's** own error string) — this is exactly the kind of thing that should be declared per-subclass, the same way `get_stuck_patterns()` is.

Recommended shape (mirrors `get_tui_status_patterns`'s base-default + `get_stuck_patterns`'s per-subclass-override precedent):
```python
def get_launch_rejection_patterns(self) -> List[str]:
    """Regex fragments (ORed together) indicating THIS CLI's own launch command
    was rejected and control fell back to a bare shell -- e.g. an invalid --model
    flag, a missing dependency, a first-run confirmation gate. The generic
    "binary not found" cases are already covered by the base default; override to
    ADD this CLI's own launch-time error wording, not replace the base list."""
    return [r"command not found", r"No such file or directory"]
```
with each subclass **extending** (`return super().get_launch_rejection_patterns() + [...]`) rather than replacing, since the shell-not-found case applies universally.

**Class-by-class values needed:**
- **Base default** (all CLIs inherit): `command not found`, `No such file or directory`.
- **`PiAgent`**: add `model.{0,60}not found` — this is the alternative `e9c47f7` added, and it should move from the shared regex to pi's own override, not stay global (a `claude` or `codex` binary that's present but misconfigured prints a differently-worded error, and globally matching against every CLI's output risks a false positive on legitimate output, e.g. an agent's own task description echoed back).
- **`ClaudeCodeAgent`**: add the `Bypass Permissions mode` check — this becomes `get_launch_rejection_patterns()`'s first genuinely-CLI-specific addition once unified, though note it currently raises a *differently worded* exception ("stuck on an unhandled first-run confirmation dialog" vs. "CLI failed to start") — preserve that distinction (two raise sites, or an enum/reason returned by `_detect_launch_failure` instead of a single boolean) rather than collapsing both into one generic message.
- **`OpenCodeAgent`, `DroidAgent`, `CodexAgent`, `SwarmCodeAgent`**: no CLI-specific rejection wording is currently hardcoded anywhere in `manager.py` for these four — they'd inherit the base default only, unless/until a real incident surfaces CLI-specific wording for one of them (don't invent patterns with no evidence).

#### External call sites

**Production (7 files, 8 call sites for `create_agent_for_task`, 2 for `restart_agent`):**
- `src/mcp/agents_api.py:556` — `create_agent_for_task_endpoint` (the direct HTTP route)
- `src/mcp/autopilot/feature_routes.py:912` — feature-pipeline dispatch
- `src/monitoring/monitor.py:410, 562, 1655` — three separate `create_agent_for_task` call sites inside the monitor's own recovery/retry paths
- `src/monitoring/monitor.py:2799` (inside `_handle_missing_tmux_session`) — **real** `restart_agent` call. **Do not confuse this with `monitor.py:2512`'s `_auto_restart_agent`**, which is a *third*, independent "kill and let a later sweep re-dispatch" implementation that does **not** call `AgentManager.restart_agent` at all — it kills the tmux session and marks the `Agent` row `terminated` directly, relying on a separate retry sweep to eventually create a *new* agent via `create_agent_for_task`. This is a relevant adjacent finding for whoever scopes the dispatch-pipeline consolidation (`AUTOPILOT_REFACTOR_PLAN.md` §4.3) but is out of scope for this split — flagging so it isn't mistaken for a third caller of the two functions being split.
- `src/autopilot/orchestrator/engine_client.py:442` — inside `create_agent_for_task_direct`'s async wrapper (itself one of the three independent dispatch implementations `AUTOPILOT_REFACTOR_PLAN.md`'s own §4.3 addresses, not this document's §4.3)
- `src/services/agent_dispatch_service.py:153` — `AgentDispatchService.dispatch`
- `src/validation/validator_agent.py:157` — `spawn_validator_agent`
- `src/mcp/server.py:775` — startup-resume path (`restart_agent`, "server restarted — resuming interrupted work")

**Tests — exhaustive, per lesson 1 in §3 above (not to repeat the prior split's missed-test-migration mistake):**
- `tests/test_agent_manager.py` — the primary suite. `create_agent_for_task` called at 17 sites across `TestCreateAgentForTask` (110), `TestCreateAgentForTaskMissingSharedWorktree` (697), `TestProjectScopedWorktreeManager` (748), `TestCreateAgentForTaskFallback` (851), `TestCreateAgentForTaskSessionLimitPause` (1067). `restart_agent` called at 4 sites in `TestRestartAgent` (1175). Also relevant: `TestCodexTmuxLifecycle` (1303) and `TestSendInitialPromptSessionLimitCheck` (1442) exercise shared helper methods this split's shared steps would call.
- `tests/test_prompt_delivery_cleanup.py` — 3 `create_agent_for_task` call sites (76, 169, 252), specifically testing the cleanup-on-failure path — this is exactly the block flagged create-only above, so these tests gate that step directly.
- `tests/test_worktree_integration.py` — 4 `create_agent_for_task` call sites (153, 209, 241, 287) — exercises the worktree-resolution step specifically.
- `tests/test_monitor.py` — no direct source calls, but `TestHandleMissingTmux::test_restarts_agent` (2786) mocks `mock_agent_manager.restart_agent` and asserts it's called by `_handle_missing_tmux_session` — a call-site test for the *monitor* side that would break if `restart_agent`'s signature changes.

No other test files reference either symbol.

#### Testing — what currently covers this behavior

- **`TestCreateAgentForTask`** (test_agent_manager.py:110): guard checks, successful creation, `assign_to_task` race-closing behavior, termination-race abort during CLI init, transcript autoflush, session-id derivation, agent-log entry, working-directory pass-through.
- **`TestCreateAgentForTaskMissingSharedWorktree`** (697): the fail-loudly guard at 446–475.
- **`TestProjectScopedWorktreeManager`** (748): `_scoped_worktree_manager` resolution.
- **`TestCreateAgentForTaskFallback`** (851): fallback-cli-tool retry, including the direct characterization test for the launch-failure regex (`test_falls_back_when_cli_rejects_its_own_launch_model`, 948) — must stay green through the split.
- **`TestCreateAgentForTaskSessionLimitPause`** (1067): the session-limit → workflow-pause cleanup path.
- **`TestRestartAgent`** (1175): not-found handling, max-restart-count, tmux-session-kill-on-restart, restart-count increment. Notably thin compared to `create_agent_for_task`'s coverage — **no existing test exercises restart's env/model resolution, session-id generation, or prompt delivery in detail**, meaning several "shared step" candidates above are currently characterized mainly through `create_agent_for_task`'s tests, not `restart_agent`'s. **Any consolidation needs new restart-side characterization tests for these paths before extraction**, not just a rerun of the existing thin `TestRestartAgent` suite — otherwise a behavior change in the shared step could silently break restart while `TestCreateAgentForTask` stays green.

**Coverage gap to flag, not fix here**: `restart_agent` has no test analogous to `test_falls_back_when_cli_rejects_its_own_launch_model`, because it has no launch-failure detection at all. If `_detect_launch_failure` becomes a shared step, a new restart-side regression test is needed asserting a relaunch that fails the same way is actually caught.

---

### 4.3 `src/monitoring/monitor.py`'s `MonitoringLoop`

**Verified against:** `src/monitoring/monitor.py` @ HEAD, via `ast.parse()` for every line range below — nothing in this section is carried over from `AUTOPILOT_REFACTOR_PLAN.md`'s or `SOLID_OO_REVIEW.md`'s own numbers without re-checking.

#### Headline correction to the existing plan

`AUTOPILOT_REFACTOR_PLAN.md` §3.2's table row characterizes this class as "~2,050-line class fusing scheduling, two heuristic detectors, Guardian dispatch, orphan cleanup, and a full diagnostic-agent state machine" and rates it "lower risk than the two God-files above." Both halves of that need correcting before anyone scopes work off it:

- **Size:** the file is 3,676 lines total; `MonitoringLoop` runs from line 175 to line 3676 — **3,502 lines**, i.e. the class is now the entire file minus a 174-line module-level prelude. That's 71% larger than the ~2,050 the plan cites (which was itself carried from an earlier snapshot taken *before* the `1998a11` `OrphanSessionReaper` extraction shrank it, and the class has grown substantially since on top of that net shrink). The two specific methods the plan does cite exact sizes for are still accurate: `_mechanical_recovery_for_agent` is 256-714 (**459 lines**, plan said ~460) and `_monitoring_cycle` is 1984-2274 (**291 lines**, plan said ~290) — so the plan's fine-grained claims hold, only its whole-class estimate is stale.
- **"Two heuristic detectors" undercounts by ~6x.** That phrase refers specifically to `_mechanical_recovery_for_agent` + `_detect_repetition_loop`, the two *original* no-LLM heuristics. The class now has **13** `_detect_*`/mechanical-recovery methods called in sequence from `_monitoring_cycle`'s "Phase 0" block, each anchored to a specific CLI failure signature discovered in production, most maintaining their own per-agent state dict. This is a materially bigger, more heterogeneous cluster than "two detectors" suggests.
- **"Lower risk... proven template" is directionally right but the template doesn't fully generalize.** `1998a11`'s `OrphanSessionReaper` extraction worked because that method was almost fully self-contained (own DB reads, own state, one caller). Several of the 13 detector methods share a single instance attribute (`self._stuck_state`) across method boundaries, and `_auto_restart_agent`/`_log_agent_event` are shared helpers reached by detectors *and* by the Guardian-dispatch cluster — extracting those correctly requires more than a single mechanical per-method move.

#### Precedent: how `1998a11` actually did the `OrphanSessionReaper` extraction

`git show 1998a11` (2026-07-01, "refactor: extract OrphanSessionReaper from MonitoringLoop (SOLID review 3.4)"):

- **New file**, `src/monitoring/orphan_reaper.py` (171 lines), a standalone `OrphanSessionReaper` class — not a nested/inner class, not a mixin.
- **Constructor takes only what it needs**, not a reference to the parent: `OrphanSessionReaper(db_manager, agent_manager)`. It does not hold a back-reference to `MonitoringLoop`.
- **`MonitoringLoop.__init__` composes it** as an instance attribute: `self._orphan_reaper = OrphanSessionReaper(db_manager, agent_manager)`, with a comment citing the SOLID review finding by number.
- **The old public method name is kept on `MonitoringLoop` as a thin delegator**, specifically *because tests call it directly on the `MonitoringLoop` instance*: `async def _cleanup_orphaned_tmux_sessions(self): ...` now just two-way-syncs one piece of legacy instance state onto the reaper's own attribute, calls the reaper, and syncs state back. This is a deliberate, commented exception to this repo's "no compat shims" rule — it's preserving the class's own existing public method contract that ~3 tests depend on.
- **The extracted class does its own deferred imports.**
- **Docstring on the new module explains the extraction rationale** and points at the SOLID review finding by section number, and notes explicitly why the delegator still exists.
- **Verification**: targeted regression (3 tests) + the full `test_monitor.py` suite run, not just the specific tests.

This is the exact template Phase 1b should reuse for each extraction below: standalone collaborator class taking exactly the constructor args it needs, composed as an instance attribute on `MonitoringLoop`, old method names kept as thin delegators wherever tests call them by name directly on the `MonitoringLoop` instance — which, per the External call sites section below, is the common case here.

#### `_monitoring_cycle`'s own Phase 0/1/2/3 comments, mapped to exact ranges

`_monitoring_cycle` is 1984-2274 (291 lines). Its own inline comments only label 4 of its ~9 logically distinct blocks — the plan's "extract along the already-commented phase boundaries" undersells how much of this method is *not* commented as a phase:

| Range | Label in source | What it does |
|---|---|---|
| 1984-2001 | (none — setup) | Debug logging of `phase_manager` state, fetch `agents = self.agent_manager.get_active_agents()` |
| 2003-2056 | **"Phase 0: cheap mechanical recovery (no LLM)"** | Sequentially calls all 13 detector methods per agent, building a `mechanically_intervened` id set |
| 2057-2107 | **"Phase 1: Guardian Analysis (Parallel)"** | Builds `guardian_tasks` (skipping agents already mechanically intervened this cycle), `asyncio.gather`s them, filters exceptions |
| 2109-2139 | **"Phase 2: Conductor Analysis (if we have summaries)"** | Calls `self.conductor.analyze_system_state`, `_save_conductor_analysis`, `execute_decisions`, optionally `generate_detailed_report` on low coherence |
| 2141-2145 | "Clean up orphaned tmux sessions" (own comment, not "Phase") | `await self._cleanup_orphaned_tmux_sessions()` |
| 2147-2202 | **unlabeled** | Workflow-tracking auto-discovery and workflow-switch bookkeeping (opens/closes a *raw* `session = self.db_manager.get_session()` inline rather than `session_scope()` — already flagged in `AUTOPILOT_REFACTOR_ANALYSIS.md`) |
| 2204-2205 | comment only, no code | "Phase progression is now handled by the orchestrator... monitor no longer creates tasks" |
| 2207-2214 | **unlabeled** | Diagnostic-check pre-logging |
| 2216-2220 | **"Phase 3: System Health Audit"** | `await self._audit_system_health()` |
| 2222-2255 | **unlabeled** | A second raw `session = self.db_manager.get_session()` block, purely diagnostic logging (no writes) |
| 2257-2274 | **unlabeled** | Conditionally calls `await self._check_workflow_stuck_state()` if a workflow is tracked |

So "Phase 0/1/2/3" only covers 4 of the method's ~11 blocks by line count (roughly 170 of 291 lines); the remaining ~120 lines are workflow-switch bookkeeping, two ad-hoc raw-DB-session debug blocks, and the diagnostic-trigger dispatch, none of which carry a "Phase N" label despite being real, distinct concerns. A clean extraction of `_monitoring_cycle` itself needs to name these too — this method is the eventual coordinator and should stay in `monitor.py`, but its *body* should become a short sequence of named calls into the extracted collaborators rather than the current single flat function.

#### Full method inventory (`ast.parse()`, current HEAD)

All 32 methods on `MonitoringLoop`, in file order:

| Method | Lines | Size |
|---|---|---|
| `__init__` | 178-231 | 54 |
| `start` | 233-249 | 17 |
| `stop` | 251-254 | 4 |
| `_mechanical_recovery_for_agent` | 256-714 | 459 |
| `_detect_cli_model_fallback` | 716-980 | 265 |
| `_verify_cli_model_fallback` | 982-1076 | 95 |
| `_log_agent_event` | 1078-1100 | 23 |
| `_detect_repetition_loop` | 1102-1185 | 84 |
| `_detect_dangerous_command_confirmation` | 1187-1249 | 63 |
| `_detect_max_token_limit_error` | 1251-1300 | 50 |
| `_detect_unconfirmed_task_completion` | 1312-1419 | 108 |
| `_detect_mcp_disconnected` | 1421-1536 | 116 |
| `_detect_connection_errors` | 1538-1731 | 194 |
| `_detect_bad_model_error` | 1733-1781 | 49 |
| `_detect_orphaned_idle_agent` | 1783-1820 | 38 |
| `_detect_credit_exhausted` | 1822-1886 | 65 |
| `_detect_agent_never_started` | 1897-1982 | 86 |
| `_monitoring_cycle` | 1984-2274 | 291 |
| `_guardian_analysis_for_agent` | 2276-2510 | 235 |
| `_auto_restart_agent` | 2512-2593 | 82 |
| `_get_past_summaries_for_agent` | 2595-2653 | 59 |
| `_update_agent_health_from_trajectory` | 2655-2725 | 71 |
| `_save_conductor_analysis` | 2727-2788 | 62 |
| `_handle_missing_tmux_session` | 2790-2801 | 12 |
| `_write_agent_tmux_log` | 2803-2869 | 67 |
| `_audit_system_health` | 2871-3061 | 191 |
| `_cleanup_orphaned_tmux_sessions` | 3063-3075 | 13 (already a delegator, see above) |
| `_check_workflow_stuck_state` | 3077-3336 | 260 |
| `_log_diagnostic_status_report` | 3338-3392 | 55 |
| `_create_diagnostic_agent` | 3394-3415 | 22 |
| `_gather_diagnostic_context` | 3417-3567 | 151 |
| `_generate_diagnostic_prompt` | 3569-3676 | 108 |

#### Clustering by responsibility

**Cluster A — scheduling/composition root (stays in `monitor.py` as `MonitoringLoop`):** `__init__`, `start`, `stop`, `_monitoring_cycle` (rewritten to call into the collaborators below instead of inlining their logic).

**Cluster B — mechanical/heuristic detectors (Phase 0 block), 13 methods, ~1,730 lines (256-1982, minus `_log_agent_event`'s 23 which is a shared helper, not itself a detector):** `_detect_orphaned_idle_agent`, `_detect_credit_exhausted`, `_detect_agent_never_started`, `_mechanical_recovery_for_agent`, `_detect_cli_model_fallback`, `_verify_cli_model_fallback`, `_detect_repetition_loop`, `_detect_dangerous_command_confirmation`, `_detect_max_token_limit_error`, `_detect_unconfirmed_task_completion`, `_detect_mcp_disconnected`, `_detect_connection_errors`, `_detect_bad_model_error` — called in this exact order from `_monitoring_cycle`'s Phase 0 loop. By far the largest cluster and the one the "two heuristic detectors" phrase most undersells.

**Cluster C — Guardian dispatch + supporting helpers, ~590 lines:** `_guardian_analysis_for_agent` (235), `_get_past_summaries_for_agent` (59), `_update_agent_health_from_trajectory` (71), `_handle_missing_tmux_session` (12), `_write_agent_tmux_log` (67) — all called only from `_guardian_analysis_for_agent`'s body. Plus `_auto_restart_agent` (82), which is **shared** — called from both this cluster and cluster B; see "Shared state" below.

**Cluster D — Conductor dispatch:** `_save_conductor_analysis` (62). Thin — `_monitoring_cycle`'s Phase 2 block calls `self.conductor.analyze_system_state`/`execute_decisions`/`generate_detailed_report` directly on the existing `Conductor` collaborator; the only `MonitoringLoop`-owned logic here is persisting the analysis result.

**Cluster E — orphan cleanup:** `_cleanup_orphaned_tmux_sessions` (13 lines) — already a delegator to `OrphanSessionReaper`. Nothing left to do here.

**Cluster F — system health audit:** `_audit_system_health` (191 lines) — a distinct concern from Guardian trajectory analysis: DB-driven checks plus a call into `run_health_audit` (which itself now lives in `src/mcp/autopilot/control_routes.py` post the already-completed API split — `monitor.py:2876` is the call site `backend_module_decomposition.md` §4 tracked as "function-scoped import inside `_audit_system_health`"). Owns `self._stuck_task_nudges` and writes `self._health_findings` (write-only within the class — no other reader found anywhere in `src/`/`tests/`, so it's effectively dead state today, not a real cross-method dependency).

**Cluster G — diagnostic-agent state machine, ~600 lines:** `_check_workflow_stuck_state` (260, entry point), `_log_diagnostic_status_report` (55), `_create_diagnostic_agent` (22), `_gather_diagnostic_context` (151), `_generate_diagnostic_prompt` (108). This is the "full diagnostic-agent state machine" the plan names — confirmed as its own clean cluster with only internal cross-calls.

**Cross-cutting helper, not cleanly in one cluster:** `_log_agent_event` (23 lines) — called from 6 sites across cluster B. Needs only `self.db_manager` — cheap to give every cluster-B collaborator its own copy, or keep as a tiny shared logging helper.

#### Proposed module/class names and method-by-method moves

Following this repo's existing convention (`src/monitoring/orphan_reaper.py` / `OrphanSessionReaper`, `src/monitoring/conductor.py` / `Conductor`, `src/monitoring/guardian.py` / `Guardian` — one collaborator class per file, composed by `MonitoringLoop`):

| New file | New class | Methods moved | Constructor args |
|---|---|---|---|
| `src/monitoring/mechanical_recovery.py` | `MechanicalRecoveryDetector` | all 13 cluster-B detectors + `_log_agent_event` | `db_manager`, `agent_manager` — plus a reference to whatever `_auto_restart_agent` becomes (see "Shared state" below) |
| `src/monitoring/guardian_dispatch.py` | `GuardianDispatcher` | `_guardian_analysis_for_agent`, `_get_past_summaries_for_agent`, `_update_agent_health_from_trajectory`, `_handle_missing_tmux_session`, `_write_agent_tmux_log` | `db_manager`, `agent_manager`, `guardian`, `phase_manager` — plus the same `_auto_restart_agent` reference |
| `src/monitoring/health_audit.py` | `SystemHealthAuditor` | `_audit_system_health` | `db_manager`, `agent_manager` — owns `_stuck_task_nudges` and `_health_findings` as its own instance attributes instead of `MonitoringLoop`'s |
| `src/monitoring/diagnostic_agent.py` | `WorkflowStuckDiagnostics` | `_check_workflow_stuck_state`, `_log_diagnostic_status_report`, `_create_diagnostic_agent`, `_gather_diagnostic_context`, `_generate_diagnostic_prompt` | `db_manager`, `agent_manager`, `phase_manager` |
| `src/monitoring/monitor.py` (stays) | `MonitoringLoop` | `__init__`, `start`, `stop`, `_monitoring_cycle` (rewritten as a coordinator), `_save_conductor_analysis`, plus delegator stubs | — |

**Delegator stubs on `MonitoringLoop`, matching `1998a11`'s pattern exactly:** because ~180 test call sites (see "External call sites" below) invoke these methods *by name, directly on a `MonitoringLoop` instance*, every moved method needs a same-named async/sync delegator left on `MonitoringLoop` that forwards to the new collaborator:

```python
async def _detect_repetition_loop(self, agent) -> bool:
    return await self._mechanical_recovery.detect_repetition_loop(agent)
```

(Method names on the new collaborator classes can drop the leading underscore since they're now public methods of a purpose-built class, while the delegator on `MonitoringLoop` keeps the old underscored name for test compatibility, same asymmetry `1998a11` used.)

This is a real cost worth being explicit about: 12 detector methods + 5 diagnostic-state-machine methods + 5 Guardian-cluster methods + 1 health-audit method = **23 delegator stubs**, versus `1998a11`'s single one. Each stub is 2-4 lines, so ~70-90 lines of pure forwarding code stays in `monitor.py` — small relative to the ~3,200 lines actually moved, but it means `MonitoringLoop` does not shrink to a bare coordinator the way "thin per-cycle scheduler" framing implies; it shrinks to a coordinator plus a forwarding table.

#### Shared state — what threading each extraction actually needs

- **Set in `__init__`, needed everywhere:** `db_manager`, `agent_manager`, `phase_manager`, `llm_provider`, `rag_system`, `config`, `running`.
- **Set in `__init__`, collaborator objects:** `guardian`, `conductor`, `trajectory_context`, `_orphan_reaper`.
- **Set in `__init__`, cluster-specific:** `guardian_summaries_cache` (dict, cluster C), `_stuck_task_nudges` (dict, cluster F, also read directly by tests).
- **Lazily created on first use, one dict/set per detector, cluster B only — genuinely private to their owning method, safe to move as the new class's own `__init__`-declared attributes instead of lazy:** `_switched_to_fallback_model`, `_fallback_attempt_count`, `_pending_fallback_verification` (all three: `_detect_cli_model_fallback` writes, `_verify_cli_model_fallback` reads/writes the same three — these two methods must move together, which the proposed grouping already does), `_rep_loop_state`, `_denied_dangerous_cmds`, `_nudged_token_limit`, `_nudged_unconfirmed_completion`/`_unconfirmed_completion_state`, `_nudged_mcp_disconnected`/`_mcp_disconnect_nudge_count`, `_connection_error_warned`, `_fixed_bad_model`, `_paused_credit_exhausted`, `_never_started_handled`.
- **The one genuinely cross-method shared state within cluster B — the extraction's real hazard:** `self._stuck_state` (dict keyed by `agent.id`, tracks frozen-output-duration tracking). Owned/written primarily by `_mechanical_recovery_for_agent` but also **read and mutated by `_detect_cli_model_fallback`** and **popped by `_detect_connection_errors`, `_detect_bad_model_error`**. All of these land in the same proposed `MechanicalRecoveryDetector` class, so this is survivable as one shared instance attribute on the new class — but it means these 5+ methods cannot be split across two different new classes without also splitting `_stuck_state` ownership. Keep the whole detector cluster in one class specifically because of this.
- **`_health_findings`:** write-only — no cross-method read found; not a real threading concern, just note it moves with `_audit_system_health`.
- **`_last_orphan_check_time`:** already handled by the existing `1998a11` delegator — no new work needed, don't touch.

**Shared-but-cross-cluster helper, the one piece that doesn't fit cleanly into any single new class:** `_auto_restart_agent` (82 lines) is called from cluster B (1 site) *and* cluster C (2 sites), and itself needs `db_manager`, `agent_manager`, *and* `self.guardian.record_auto_restart(...)`. Three options: (a) leave it on `MonitoringLoop` itself, called back into via a constructor-injected reference to the parent; (b) duplicate the ~15 lines of DB-write logic into both new classes (cheap, but reintroduces exactly the "N-th independent implementation" pattern `AUTOPILOT_REFACTOR_PLAN.md` §4.2 is already tracking as a consolidation target — bad timing to add a duplicate right before that phase); (c) extract `_auto_restart_agent` itself into a third, tiny shared collaborator, injected into `GuardianDispatcher` too. **(c) is recommended** — it's a single well-scoped method with a clear existing docstring (a detailed "observed live" incident writeup) worth preserving verbatim, and giving it its own home avoids both the back-reference in (a) and the duplication in (b).

**Bug noticed while tracing this (not to be fixed in Phase 1b, log only, per the zero-behavior-change rule and CLAUDE.md's `agent-termination` invariant):** `_auto_restart_agent` sets `db_agent.status = "terminated"` and clears `db_agent.current_task_id = None` (2580-2581) but never sets `terminated_at` — a real violation of the project's own `agent-termination` critical invariant, distinct from the already-tracked gap `AUTOPILOT_REFACTOR_PLAN.md` §4.2 names ("the `current_task_id` gap in `monitor.py`", which is a *different* site: `_detect_orphaned_idle_agent` at line 1809-1811 sets `status` and `terminated_at` correctly but leaves `current_task_id` pointing at the task it just failed). So `monitor.py` has **two** separate termination-invariant gaps in two different methods, not one — §4.2's call-site list should include `_auto_restart_agent`'s `terminated_at` gap explicitly alongside `_detect_orphaned_idle_agent`'s `current_task_id` gap when that phase starts.

#### External call sites

**Production code:** exactly one production instantiation site — `run_monitor.py:116`, which only calls `.start()`/`.stop()` on it. No other production module reaches into a `MonitoringLoop` instance's internals. This is the good news: the production blast radius of this extraction is limited to `run_monitor.py`'s one constructor call, which needs zero changes as long as `__init__` still composes the new collaborators internally. `src/core/constants.py:17` has a comment referencing `src.monitoring.monitor._create_diagnostic_agent` by dotted path — not a functional dependency, just a stale-comment risk if `_create_diagnostic_agent` moves; update the comment in the same commit as that method's move.

**Test code — this is where essentially all of the real risk lives:**

| Test file | How it uses `MonitoringLoop` | Test count |
|---|---|---|
| `tests/test_monitor.py` | Instantiates directly via `make_monitoring_loop` fixture, then calls private methods **by underscored name directly on the instance** across 28 test classes, and reaches directly into instance dict state (e.g. `audit_monitor._stuck_task_nudges.get(...)`) | 134 |
| `tests/test_diagnostic_agent.py` | Instantiates `MonitoringLoop` directly, calls `_check_workflow_stuck_state()`, `_gather_diagnostic_context()`, `_generate_diagnostic_prompt()` directly | 10 |
| `tests/test_diagnostic_integration.py` | Same pattern, calls `_check_workflow_stuck_state()` directly (6 call sites) | 6 |
| `tests/test_steering_fix.py` | Instantiates `MonitoringLoop` directly, calls `_monitoring_cycle()` directly (3 call sites) | 6 |
| `tests/test_orphan_reaper.py` | Tests `OrphanSessionReaper` directly (already-extracted collaborator) — a working example of what the *new* per-class test files should look like post-extraction | 9 |

Total: **~156 tests across 4 files** exercise `MonitoringLoop` by direct instantiation and direct private-method calls; none of them use `@patch("src.monitoring.monitor....")` string-based patching — so there is no `patch()`-target migration risk analogous to the orchestrator/API splits' biggest post-mortem finding. The risk here is structurally different: it's "does the delegator forward correctly and does state read through it," not "does a string patch target the right module." This is exactly why the delegator-stub approach (verbatim copy of `1998a11`'s pattern) matters more here than a from-scratch redesign would — with ~156 tests calling private methods by name, **not** keeping delegators would mean rewriting all ~156 call sites, a much larger and riskier diff than the ~23 delegator stubs this section proposes.

#### Testing

- `tests/test_monitor.py` (134 tests, 28 classes) is the primary regression suite — organized almost 1:1 by method, which conveniently means each class's tests can move wholesale into new per-collaborator test files, constructing the new collaborator classes directly instead of going through `MonitoringLoop` and its delegators (mirroring how `tests/test_orphan_reaper.py` already tests `OrphanSessionReaper` standalone). That test-file split is optional relative to just keeping delegators and leaving `test_monitor.py` as-is (both keep the suite green); moving the tests is the more thorough version that actually validates the new classes' own constructors work standalone.
- `tests/test_diagnostic_agent.py`, `tests/test_diagnostic_integration.py`, `tests/test_steering_fix.py` — all call methods through a live `MonitoringLoop` instance; these should keep working unchanged against the delegator stubs and don't strictly need touching.
- `tests/test_orphan_reaper.py` — unaffected, already covers the one prior extraction.
- Per this repo's stated test-running preference (targeted, not full-suite), run `pytest tests/test_monitor.py tests/test_diagnostic_agent.py tests/test_diagnostic_integration.py tests/test_steering_fix.py tests/test_orphan_reaper.py` after each extraction.
- No `@patch("src.monitoring.monitor....")` retargeting is needed anywhere — the single biggest source of silent breakage in the two prior splits doesn't apply to this file's test suite by construction. The equivalent risk here is a missed or incorrectly-forwarding delegator stub, which **would** fail loudly (`AttributeError` or a wrong-instance `AssertionError`) rather than silently.

---

### 4.4 `src/services/task_completion_service.py` → split + partial migration into `phase_transitions.py`

**Verified against the live file, 2026-08-16:** **1,125 lines, 11 `@staticmethod` methods** — the plan's baseline is **exact, not stale**, unusual for this codebase's rate of churn (contrast §4.1/§4.3's targets, which had both grown materially since their own baselines were recorded).

#### Exhaustive symbol table (line ranges verified against the live file)

| # | Method | Lines | Size | Kind |
|---|---|---|---|---|
| 1 | `record_learnings` | 23–64 | 42 | `async @staticmethod` |
| 2 | `verify_output_artifact` | 65–300 | 236 | `@staticmethod` |
| 3 | `verify_gate_result_schema` | 301–358 | 58 | `@staticmethod` |
| 4 | `verify_no_open_tickets` | 359–436 | 78 | `@staticmethod` |
| 5 | `_parse_forensics_recommendations` | 437–498 | 62 | `@staticmethod` (private helper) |
| 6 | `create_tickets_from_forensics_report` | 499–568 | 70 | `async @staticmethod` |
| 7 | `fire_spec_gate_if_ready` | 569–814 | 245¹ | `async @staticmethod` |
| 8 | `spawn_validation` | 815–909 | 95 | `async @staticmethod` |
| 9 | `verify_output_survived_commit` | 910–1023 | 114 | `@staticmethod` |
| 10 | `commit_and_link_ticket` | 1024–1108 | 85 | `async @staticmethod` |
| 11 | `collect_cost_on_completion` | 1109–1125 | 17 | `@staticmethod` |

¹ Decorator-to-next-decorator span is 569–814 (246 lines incl. trailing blank); the function body itself is exactly **245 lines**, matching the plan's figure precisely.

#### `fire_spec_gate_if_ready` migration into `phase_transitions.py`

**Current orchestrator imports (all already point at the new package — nothing stale):**
```
622:  from src.autopilot.orchestrator.phase_transitions import _claim_phase_task_creation
683:  from src.autopilot.orchestrator.phase_transitions import _trigger_arbitration
717:  from src.autopilot.orchestrator.phase_transitions import _create_phase_task
801:  from src.autopilot.orchestrator.phase_transitions import _create_phase_task
```
These are exactly the rows `backend_module_decomposition.md` §4 already lists for this file, and confirm the orchestrator split's own call-site migration (`a2905e8`) already retargeted them correctly — no drift.

**Circular-import risk: none found.** Grepped `phase_transitions.py` and every other orchestrator submodule for `task_completion_service`/`TaskCompletionService` — the only hit is a comment (prose reference, not an import). `fire_spec_gate_if_ready`'s full body never calls any `TaskCompletionService.*` sibling method — it's self-contained aside from the 4 imports above (which become **unnecessary and should be deleted**, not retargeted, once the function is physically inside `phase_transitions.py`: `_claim_phase_task_creation`, `_trigger_arbitration`, and `_create_phase_task` are already module-level siblings there — lines 758, 1958, 2499 respectively — so the merged function calls them as bare names with no import at all). This is a one-directional move with zero new edges in the import graph.

**What else the move needs, checked against `phase_transitions.py`'s current top-of-file imports:**
- Already present, reusable as-is: `GATED_PHASES`, `build_phase_output`, `Phase`, `PhaseExecution`, `Task`, `Workflow`, `DatabaseManager`, `get_db`, `PhaseManager`, `Path`.
- Missing, must be added: `asyncio` (module has none currently), `functools` (currently function-scoped in the method — keep it function-scoped per this file's existing convention), `set_log_context` from `src.core.log_context` (currently function-scoped — keep it that way).
- `DatabaseManager as _DbMgr` (an alias existing only to avoid a hypothetical name clash in `task_completion_service.py`, which doesn't exist there either — but there's no clash in `phase_transitions.py` since it already imports plain `DatabaseManager`) — drop the alias, use the existing import directly.

**Call sites needing retarget (both in `src/mcp/server.py`, both function-scoped imports per this codebase's convention):**

| Site | Current | After move |
|---|---|---|
| `server.py:2578` (import) + `:2929` (call), inside `update_task_status` | `from src.services.task_completion_service import TaskCompletionService` ... `await TaskCompletionService.fire_spec_gate_if_ready(session, task)` | `from src.autopilot.orchestrator.phase_transitions import fire_spec_gate_if_ready` ... `await fire_spec_gate_if_ready(session, task)` |
| `server.py:3354` (import) + `:3404` (call), inside the human-complete-task recovery route | same pattern | same pattern |

Both `server.py` route handlers already import `TaskCompletionService` locally for their *other* calls — only the `fire_spec_gate_if_ready` line moves to a second, separate function-scoped import from `phase_transitions`; the rest of each handler's `TaskCompletionService.*` calls are unaffected.

**Test call sites — this is the one place this migration touches the most surface:**

- `tests/test_task_completion_service.py::TestFireSpecGateIfReadyGoto` (lines 926–1149, 224 lines, 4 test methods) calls `TaskCompletionService.fire_spec_gate_if_ready(session, task)` directly at lines 1019, 1073, 1101, 1142. Since the function is leaving the class entirely (no facade — see below), this whole test class should **physically relocate** to wherever `phase_transitions.py`'s own tests live (`tests/test_orchestrator_helpers.py` or a new `tests/test_phase_transitions_spec_gate.py`), with its 4 call sites rewritten to `fire_spec_gate_if_ready(session, task)` (plain function call). Its own internal `@patch(...)` targets already target `phase_transitions.py` directly — **need no change**, a small confirming signal this test class was written with the eventual destination in mind.
- `tests/test_update_task_status_ordering.py` — **7 string-patch occurrences** across 3 test methods plus a shared `tcs` prefix variable pattern. All 7 must retarget to `"src.autopilot.orchestrator.phase_transitions.fire_spec_gate_if_ready"`. **Why the source module, not `src.mcp.server.fire_spec_gate_if_ready`:** `server.py` imports the name with a function-scoped import — re-resolved fresh on every route call, per lesson 2 in §3 above.
- `tests/test_goto_reconvergence.py:303` and `tests/test_project_scoped_repo_resolution.py:10-11` reference `fire_spec_gate_if_ready`/`task_completion_service.py` only in prose comments — no functional migration needed, though the comments become stale pointers (optional touch-up while in the file).

#### Remaining ~880 lines: cluster into modules

**Call-pattern finding that shapes the whole split:** every production caller is `src/mcp/server.py`, and every one of them uses the **class-namespaced static-method form** — `TaskCompletionService.verify_output_artifact(...)`, etc. (12 call sites across two route handlers). Nothing imports individual methods as bare functions. This is the opposite of the two `backend_module_decomposition.md` targets, where callers imported free functions/routers — so the router/package-driver shape doesn't fit here.

**Recommended shape: keep `TaskCompletionService` as a real (not compatibility-shim) thin facade class in `src/services/task_completion_service.py`, whose 10 remaining `@staticmethod`s each delegate to a function in a new per-concern module.** This is the minimal-touch option: `server.py`'s 12 call sites need **zero changes**, and every existing test's `@patch("src.services.task_completion_service.TaskCompletionService.<method>")` string target (there are several beyond the `fire_spec_gate_if_ready` ones catalogued above) **keeps working unmodified**, because `patch()` replaces the class attribute in place regardless of which module the attribute's implementation was originally defined in. The alternative — deleting the class and having `server.py` import 10 individual functions from 4-5 new modules — would force retargeting all 12 production call sites plus every test patch/import in `tests/test_task_completion_service.py` (1,149 lines) and the 5 other test files that reference `TaskCompletionService`, repeating exactly the kind of test-patch-migration pain the prior splits' retrospectives flagged as the bulk of the work — for a class that, unlike `orchestrator.py`'s flat-file `def`s, was never meant to be imported as bare functions in the first place. The facade isn't a compatibility shim for something being phased out; it's the actual, permanent namespaced API this class was designed around (per its own module docstring: "Each method here corresponds 1:1 to a step of that handler" in `server.py`) — `fire_spec_gate_if_ready` is the one deliberate exception, because it moves to a different subsystem's namespace entirely.

**Proposed new files** (a `src/services/task_completion/` sub-package is warranted since 5 new files is enough to want one, mirroring how `orchestrator/` became a package once it hit 8 submodules):

| New file | Symbols | Lines moved | Notes |
|---|---|---|---|
| `src/services/task_completion/memory.py` | `record_learnings` | 42 | No dependents among the other clusters. |
| `src/services/task_completion/verification.py` | `verify_output_artifact`, `verify_gate_result_schema`, `verify_no_open_tickets`, `verify_output_survived_commit` | 486 | See below on why these four (not three) belong together. |
| `src/services/task_completion/tickets.py` | `_parse_forensics_recommendations`, `create_tickets_from_forensics_report` | 132 | Private helper stays paired with its only caller. |
| `src/services/task_completion/validation.py` | `spawn_validation` | 95 | Single method; kept as its own file to match the plan's explicit "validator spawning" cluster name. |
| `src/services/task_completion/git_link.py` | `commit_and_link_ticket` | 85 | |
| **unassigned** | `collect_cost_on_completion` | 17 | **Gap in the plan's own taxonomy** — see below. |
| `src/services/task_completion_service.py` (facade, in place) | `TaskCompletionService` with 10 delegating `@staticmethod`s | ~40-60 | `fire_spec_gate_if_ready` is **not** among these 10 — it has no facade method at all after the move. |

**Gap: `collect_cost_on_completion` doesn't fit the plan's five named groups (memory/verification/tickets/validator/git).** It's cost-collection bookkeeping (delegates to `src.services.cost_collection_service.collect_task_cost`), called from `server.py:2890` — positioned in the call sequence *between* `spawn_validation` (2860) and `commit_and_link_ticket` (2895), not adjacent to the git cluster despite reading like it might belong there. At 17 lines it's too small to justify its own file by the "5+ symbols wants a package" heuristic above, but it's also not semantically memory/verification/tickets/validation/git. Two reasonable options, not resolved here: (a) a sixth tiny `cost.py` file for taxonomic cleanliness, or (b) fold it into `git_link.py` since both are "final side effects triggered once a task reaches done" even though the domains differ. Flagging as an explicit decision point for whoever executes this split, not defaulting silently to either.

#### The "three verification hard floors" — what they are, and why they don't split cleanly along the module boundary above

The parent plan's "three hard floors" are, per each method's own docstring:

1. **Output-existence** — implemented as **two methods, not one**: `verify_output_artifact` (pre-commit: does the phase's declared output file exist, with valid OKF frontmatter) and `verify_output_survived_commit` (post-commit: re-check the same paths didn't vanish between the pre-commit check and `commit_and_link_ticket` running — a real observed-live gap, per the docstring, where an agent's shell cwd drifted and the last write landed outside the worktree). These two share the same `_old_name_map` backward-compat dict for legacy filenames (defined **twice**, byte-for-byte identical, at lines 203–214 and 960–971 — a pre-existing duplication this split doesn't need to fix per the zero-behavior-change rule, but worth flagging since both copies land in the same new `verification.py` file, making the duplication newly visible/adjacent instead of 850 lines apart).
2. **Gate-result-schema** (`verify_gate_result_schema`) — for gated phases only, checks the phase's structured JSON result has the keys its `score_*` function actually reads.
3. **No-open-tickets** (`verify_no_open_tickets`) — for `development`/`git_commit_push` phases, blocks "done" while unresolved bug tickets exist for the workflow.

**They are independent checks with no shared state** — each takes `(session, task, phase=None)`, queries fresh, returns either `None` or a rejection dict; none calls another. The only thing they share is the `Phase` re-fetch-if-not-passed pattern, duplicated identically across all four methods (again pre-existing, not introduced by the split). **This independence means splitting them into separate files would be defensible**, but grouping all four into one `verification.py` is recommended anyway: `server.py:3371-3373` already iterates three of them as a list in the human-complete-task route, and `verify_output_survived_commit` is called immediately after `commit_and_link_ticket` in both routes as the fourth, complementary check — keeping all four in one file matches how they're actually consumed (as an ordered battery of gates) even though nothing in their implementation forces that grouping.

#### External call sites (production + tests, exhaustive)

**Production — `src/mcp/server.py` only** (12 call sites, both via local function-scoped imports at 2578 and 3354):

| Line | Method |
|---|---|
| 2693 | `record_learnings` |
| 2759 | `verify_output_artifact` |
| 2796 | `verify_gate_result_schema` |
| 2815 | `verify_no_open_tickets` |
| 2835 | `create_tickets_from_forensics_report` |
| 2860 | `spawn_validation` |
| 2890 | `collect_cost_on_completion` |
| 2895 | `commit_and_link_ticket` |
| 2903 | `verify_output_survived_commit` |
| 2929 | `fire_spec_gate_if_ready` |
| 3371–3373 | `verify_output_artifact`, `verify_gate_result_schema`, `verify_no_open_tickets` (as a tuple, iterated) |
| 3394 | `commit_and_link_ticket` |
| 3397 | `verify_output_survived_commit` |
| 3404 | `fire_spec_gate_if_ready` |

No other production file references `TaskCompletionService`.

**Tests** (6 files reference `TaskCompletionService`/`task_completion_service`):
- `tests/test_task_completion_service.py` (1,149 lines) — the primary suite; imports the class once, calls every method as `TaskCompletionService.<method>(...)`. See table below for its 10 test classes.
- `tests/test_update_task_status_ordering.py` — 7 string `@patch(...)` targets on `fire_spec_gate_if_ready` (need retarget, above) plus `@patch("...TaskCompletionService.commit_and_link_ticket", ...)` (×4) and `@patch("...TaskCompletionService.record_learnings", ...)` (×1) — **these do not need retargeting** under the facade approach, since the class attribute stays patchable at the same path regardless of where the delegated implementation lives.
- `tests/test_self_review_hook.py:283` — `@patch("...TaskCompletionService.spawn_validation", ...)` — no change needed (facade).
- `tests/test_goto_reconvergence.py:303`, `tests/test_project_scoped_repo_resolution.py:10-11`, `tests/test_update_task_status_response_shape.py:2` — prose comments only, no code reference.

**No test imports an individual method as a bare function** — every reference is either `TaskCompletionService.<method>` (direct call or `@patch` string target) or a comment. This uniformity is what makes the facade shape low-risk: as long as `TaskCompletionService.<method>` keeps resolving for every method except `fire_spec_gate_if_ready`, no test file needs edits beyond the `fire_spec_gate_if_ready`-specific ones already catalogued above.

#### Existing test coverage (`tests/test_task_completion_service.py`, 1,149 lines, 10 classes)

| Class | Lines | Covers |
|---|---|---|
| `TestParseForensicsRecommendations` | 10–78 | `_parse_forensics_recommendations`'s heading/priority parsing, malformed input fallback to "medium" |
| `TestVerifyOutputArtifact` | 79–353 | `verify_output_artifact`'s missing-file / invalid-OKF / optional-phase-skip / security-review-ash-scan-section paths |
| `TestVerifyOutputArtifactWorktreeRecovery` | 354–441 | The `AgentWorktree`-recovery fallback when `wf.working_directory` is unset |
| `TestVerifyOutputSurvivedCommit` | 442–525 | Post-commit re-check, including the git-history fallback |
| `TestVerifyGateResultSchema` | 526–645 | Gated-phase schema validation, non-gated/ungated-phase no-ops |
| `TestVerifyNoOpenTickets` | 646–748 | Open-bug-ticket blocking, phase-name allowlist |
| `TestRecordLearnings` | 749–801 | Embedding + `Memory` row creation per learning |
| `TestCreateTicketsFromForensicsReport` | 802–846 | End-to-end forensics-report → ticket creation, best-effort swallow-on-failure |
| `TestCommitAndLinkTicket` | 847–925 | Commit message formatting, ticket auto-link on success, silent no-op when nothing dirty |
| `TestFireSpecGateIfReadyGoto` | 926–1149 | The goto-regression class — **this is the class that relocates to `phase_transitions.py`'s test coverage**, leaving 9 classes (925 lines) behind. |

No test class currently covers `collect_cost_on_completion` in this file.

## 5. Sequencing

**Recommended order, by increasing risk/complexity, all independent of each other:**

1. **`task_completion_service.py` first.** Cleanest of the four — exact line-count match to the plan's baseline (no drift to resolve), no circular-import risk, small and fully-mapped test surface, and the `fire_spec_gate_if_ready` migration target (`phase_transitions.py`) already exists and is stable.
2. **`src/mcp/api.py` second.** Medium risk, but the risk is concentrated and known upfront: the closure-unnesting mechanical step (new relative to the two prior splits) and the total absence of HTTP-level test coverage. **Write the route-count/path-set guardrail test (Phase 0 precondition) before starting** — the 42-row cluster tables in §4.1 above are the ready-to-hardcode baseline.
3. **`MonitoringLoop` third.** Bigger than `api.py` in lines moved (~3,200) and needs 23 delegator stubs, but the production blast radius is small (one constructor call site, no other production reader) and the test-migration risk is structurally different (loud `AttributeError` on a missing delegator, not a silent string-patch miss) — lower *surprise* risk even though it's a larger diff. Do the `_auto_restart_agent` extraction (§4.3's option (c)) as part of this pass, not deferred.
4. **`create_agent_for_task`/`restart_agent` last.** The most complex: the shared-vs-caller-specific split is genuinely uneven (8 shared, 2+2 not), it's explicitly bundled with Phase 2's §4.2 agent-termination-primitive dedup work per the parent plan (not a pure Phase 1b move), and it needs new restart-side characterization tests written *before* extraction (§4.2's testing section) since several proposed shared steps are currently characterized only through `create_agent_for_task`'s tests. This is the one target where "decompose" and "deduplicate" should land as one coordinated set of commits, similar in spirit to how `phase_transitions.py`'s own exception was scoped in `AUTOPILOT_REFACTOR_PLAN.md` §3.1 (though that exception wasn't actually honored when executed — see that document's own corrected status note; don't repeat that outcome here by treating this as a pure move under time pressure).

## 6. Testing

Each target section above (§4.1–§4.4) specifies its own exact test-file list and command. Cross-cutting requirements, per `AUTOPILOT_REFACTOR_PLAN.md`'s own testing strategy (§8):

1. **Per-commit:** run only the targeted test files for whatever module just changed, not the full suite, immediately after each commit.
2. **Per-target:** an adversarial review pass before moving to the next of the four targets — the same 8-parallel-finder-pass method `AUTOPILOT_REFACTOR_PLAN.md` §8 specifies for the orchestrator/API splits.
3. **Live verification:** `heph restart` plus a manual smoke check specific to each target (hit one route per cluster for `api.py`; confirm the monitor loop still ticks for `MonitoringLoop`; confirm a real agent dispatch still succeeds for `create_agent_for_task`).
4. **Before merging each target:** the full `pytest` run, not just targeted files.
5. **After all four:** re-run `AUTOPILOT_REFACTOR_PLAN.md` §3.3's exit criteria checklist, extended to cover these four targets the same way it covers the original two — both flat structures gone, call-site sweep (production and test) returns nothing outside the new locations, guardrail/characterization tests green.

## 7. Out of scope

- **Deleting `FrontendAPI.get_agents`/`get_agent_output`'s dead code** (§4.1) — confirmed unreachable, but deletion is a separate, already-logged item, not part of this decomposition.
- **Fixing the `session_id` exclusion-list mismatch** between `create_agent_for_task`/`restart_agent` (§4.2) — log for Phase 3, don't fix inline during the split.
- **Fixing `restart_agent`'s silent-`None`-on-missing-worktree behavior** (§4.2) — a behavior change, not a refactor; flag for Phase 3.
- **Fixing `_auto_restart_agent`'s missing `terminated_at`** (§4.3) — log as a correction to `AUTOPILOT_REFACTOR_PLAN.md` §4.2's existing call-site list; fix when that phase executes, not here.
- **Fixing `post_phase_prompt_preview`'s hardcoded `DatabaseManager("hephaestus.db")`** (§4.1) — already a tracked Phase 3 item (Tier 2 item 14); move verbatim.
- **Resolving the `collect_cost_on_completion` taxonomy gap** (§4.4) — an explicit decision point left for whoever executes the split, not defaulted.
- **Any of Phase 2's actual consolidation work** — this document is decomposition (Phase 1b) only. Where a target's own research surfaced a Phase 2-relevant finding (e.g. `create_agent_for_task`'s shared-step design directly informs `AUTOPILOT_REFACTOR_PLAN.md`'s own §4.2 agent-termination primitive), that's noted in place but not executed here.
