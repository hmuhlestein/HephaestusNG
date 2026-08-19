# Phase 4 — dead code deletion findings

Execution findings for `docs/AUTOPILOT_REFACTOR_PLAN.md` §6 (Phase 4).
Executed 2026-08-19, one item at a time, each independently re-verified
fresh against current HEAD before deletion and landed as its own commit
with the verification evidence in the commit message — not trusted from
the plan's original grep evidence, including items the plan doc's own
prior "correctness review" pass had already corrected.

## Method

For each of the 11 items: confirm the claimed dead code still exists in
the claimed shape (some plan-doc file paths were stale after Phase 1c's
`server.py` split, or after an even earlier `api.py` split); fresh
`grep -rn` across all of `src/` and `tests/` for every real caller; only
delete once the evidence genuinely showed zero live callers. Deletions
that left tests exercising only the dead code removed those tests
together with the code, rather than leaving them to bit-rot. Deletions
that orphaned an import, a docstring reference, or a stale comment cleaned
those up in the same commit.

## Outcome: 10 of 11 deleted, 1 deliberately left alone

| # | Item | Outcome |
|---|------|---------|
| 1 | `EmbeddingService` | Deleted, 2 misleading type hints repointed first |
| 2 | `TrajectoryContext` | Deleted (dead *state*, not dead *symbol* — see below) |
| 3 | `SteeringIntervention` | **Left alone** — not actually dead, see below |
| 4 | `engine_client.api_get` | Deleted; `api_post` kept (2 live callers) |
| 5 | `SWEEP_ENABLED` sweep | Deleted |
| 6 | `MAX_WORKFLOW_TIME`/`MAX_PHASE0_TIME` | Deleted |
| 7 | `_archive_and_cleanup` | Deleted |
| 8 | `check_executors.py` | Deleted (whole file) |
| 9 | `MemoryIngestion` / `MonitoringLoop.rag_system` | Both deleted, separately (see below) |
| 10 | `FrontendAPI.get_agents`/`get_agent_output` | Deleted (file path was stale, re-derived) |
| 11 | `Guardian._should_steer_agent` | Deleted |

## The one item not deleted: `SteeringIntervention`

The plan characterized this as dead code with the same confidence as the
other ten items. Fresh verification found the write side genuinely dead
(zero constructors anywhere in `src/` or `tests/`), but the read side is
not: `FrontendAPI.get_steering_interventions` is wired to a live route
(`GET /api/steering-interventions`), and the **frontend actively consumes
it** — `frontend/src/pages/Overview.tsx` renders a `SteeringEventsCard`
from `systemData.recent_steering_events`, and
`frontend/src/components/TaskDetailModal.tsx` runs a `useQuery` against
the endpoint on a 10-second `refetchInterval`. This is a live, reachable,
currently-working (if permanently empty, since nothing ever populates the
table) UI feature — not unreachable code. Deleting the backend route would
have 404'd real, currently-firing frontend requests.

Caught before any deletion happened: `git checkout`'d the two files
already edited (`database.py`, `_shared.py`) back to clean, then asked the
user directly rather than guessing at scope. Explicit answer: leave it
alone. The write-path being unimplemented is a product gap (something
could call `Guardian.steer_agent` and have it actually write a
`SteeringIntervention` row so the existing UI shows real data), not dead
code to clean up.

## Real corrections found beyond what the plan doc already had

**`TrajectoryContext`'s dead-state chain has a third hop the plan's prior
correction missed.** The existing "corrected 2026-08-19" note on this item
already caught that `monitor.py` imports and constructs the class (so
deleting the class alone would break startup) — accurate. What it didn't
mention: `monitor.py` also passes `self.trajectory_context` into
`GuardianDispatcher.__init__` (`guardian_dispatch.py`), which stores it as
its own `self.trajectory_context` attribute and never reads it either. All
three sites — the import, the construction, and the pass-through parameter
— had to be removed together with the module, confirmed by re-verifying
`grep -rn 'trajectory_context\.' src/` returned zero reads *after*
accounting for this third site, not just the two the existing note named.

The real, live trajectory-monitoring feature is unaffected:
`Guardian.analyze_agent_with_trajectory` (`guardian.py:115`), called from
the actual per-agent dispatch path (`guardian_dispatch.py:181`), builds its
own context via `Guardian._build_accumulated_context` (`guardian.py:291`)
— `TrajectoryContext.build_accumulated_context` was a parallel,
never-called reimplementation of the same idea, exactly matching the
plan's "Guardian reimplements a simpler version inline" framing. Verified
this distinction directly (traced the call chain) rather than assuming it
from the plan's prose, since the user asked specifically whether the
trajectory monitoring feature itself was being lost.

**`MonitoringLoop.rag_system` is unrelated to `server_state.rag_system` —
confirmed, not assumed, before touching anything.** The plan names
"`MonitoringLoop.rag_system`'s unused wiring" specifically, but a less
careful read could conflate this with `rag_system` in general and delete
something live. `server_state.rag_system` (`src/mcp/server/_shared.py`) is
a separate `RAGSystem` instance, built through
`store_factory.create_vector_store()` (reads `VECTOR_STORE_BACKEND`,
default `turbovec`) and actively read by
`src/services/agent_dispatch_service.py` and
`src/services/task_enrichment_service.py` — untouched by this phase.
`MonitoringLoop.rag_system` was fed by a second, entirely separate
construction in `run_monitor.py` (`VectorStoreManager(qdrant_url=...)`,
hardcoded to Qdrant, never routed through `store_factory` at all) that
existed solely to populate this one dead attribute. Deleting the parameter
made that construction (and the `vector_store`/`RAGSystem` local variables
and their imports) fully orphaned, so those were removed too — confirmed
this doesn't affect the turbovec/fastembed default path, since the live
MCP server initializes Qdrant/turbovec collections independently via its
own `ServerState.initialize()`.

Required updating 8 constructor call sites across `run_monitor.py` and 4
test files (`test_monitor.py` ×3, `test_diagnostic_agent.py`,
`test_steering_fix.py` ×3, `test_diagnostic_integration.py`), plus a
shared `mock_rag_system` fixture in `tests/conftest.py` that had zero
remaining consumers once all 4 test files were updated.

**`FrontendAPI.get_agents`/`get_agent_output`'s shadowing was traced
directly, not assumed from path collision.** The plan's file path
(`src/mcp/api.py`) no longer exists — that file was already split into
`src/mcp/frontend/` in an earlier phase, before this one started. Re-located
the equivalent methods in `_shared.py`, then traced the actual FastAPI
registration order rather than assuming two routes with the same path
means one wins arbitrarily: `agents_api.py`'s router is included at
Python *import* time (`src/mcp/server/_shared.py:128`,
`app.include_router(agents_router)`), which runs before any code executes;
the frontend router carrying the dead methods is only included inside
`lifecycle.py`'s `@app.on_event("startup")` handler, which fires strictly
after import-time registration completes. FastAPI matches routes in
registration order, so `agents_api.py`'s versions always win —
structurally guaranteed, not a coincidence of current code.

## Orphans cleaned up along the way (not separate plan items)

- `src/monitoring/prompt_loader.py`'s `format_guardian_prompt` docstring
  credited `TrajectoryContext` as `accumulated_context`'s source. Already
  stale before this phase — `Guardian._build_accumulated_context` was
  always the real source — now doubly stale since the class is gone.
  Repointed.
- `tests/test_frontend_api_routes_guardrail.py`'s pinned 42-route baseline
  (a Phase 0 gate protecting against *accidental* route drops during
  refactoring) updated to 40 after deliberately removing 2 confirmed-dead,
  permanently-shadowed routes.
- `src/autopilot/orchestrator/__init__.py` had a comment block documenting
  the stray-file sweep feature, sitting in the middle of ~80 blank lines —
  itself a doubly-orphaned artifact of an *earlier*, already-completed
  relocation of that code into `features.py`. Deleting the feature
  entirely (item 5) left the comment describing nothing at all; removed it
  and the surrounding dead blank-line gap together.

## Verification

Every deletion: `py_compile` + `ruff check` on every touched file
(comparing against a `git stash`-isolated pre-existing baseline whenever a
finding's origin was ambiguous, never assuming a ruff finding was mine),
targeted `pytest` run for the affected test files, then its own commit.
No full-suite baseline re-run was done for Phase 4 specifically — each
item's blast radius was narrow enough (confirmed via the same grep that
established dead-callers) that targeted runs were sufficient, unlike
Phase 1c's whole-package restructuring.

## Not done

- `SteeringIntervention` (see above) — explicit user decision to leave
  alone, not an oversight. Had this gone forward, note for whoever revisits
  it: this codebase's existing `_migrate_*` functions in `database.py` are
  all additive (add-column only) — there's no established precedent here
  for a destructive migration (e.g. `DROP TABLE`), and writing one against
  a self-hosted production database is a materially different risk
  category than anything else in this phase.
