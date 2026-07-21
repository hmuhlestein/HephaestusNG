# Architectural Review Report

**Feature ID:** cost-tracking-database-schema  
**Phase:** architectural_review (Phase 5 of 12)  
**Date:** 2026-07-21  
**Reviewer:** Architect (re-invoked after development)  
**Commit reviewed:** `246a54d` — "Implemented cost tracking database schema with all required"  
**Architecture spec:** `docs/architecture.md` (12 tasks, T1–T12)  

---

## Executive Summary

**Implementation completeness: ~35% — all data-layer components are solid; all collection and enforcement wiring is absent.**

The developer delivered Phases 1–2 (T1 Schema + T2 Cost Derivation) faithfully and added comprehensive tests. However, **no data actually flows into the `cost_entries` table** in production: there's no collector module, no API endpoint, no task-completion wiring, no LangChain integration, and no Pi extension. The built ledger is an inert artifact.

Additionally, the developer's commit **removes significant unrelated code** (self-heal guard in `scan_design_queue`, duplicate-agent guard in `manager.py`, spec.py context manager pattern) and their corresponding test suites — none of which relate to cost tracking and none of which are noted as intentional scope changes.

---

## BLOCKER — Must Fix Before Merge

### B-1: `paused_by` Guards NOT Generalized

**Spec:** T5 (Architecture §4.2) — change all `== "user"` guards to `is not None` except in `start()`.

**Reality:** All three guards still use `== "user"`:

| Location | Line | Status |
|----------|------|--------|
| `_try_auto_resume_paused_workflow` | orchestrator.py:3710 | ❌ `== "user"` |
| `_create_corrective_task` | orchestrator.py:5534 | ❌ `== "user"` |
| `attempt_recovery` stuck-wf check | orchestrator.py:5718 | ❌ `== "user"` |

**Impact:** A `"budget"`-paused workflow sails through every one of these guards as if it were a normal active workflow. Self-heal will silently resume a pipeline the user explicitly capped by budget. This defeats the entire purpose of budget enforcement.

**Fix:** Change all three lines from `wf.paused_by == "user"` to `wf.paused_by is not None`. Keep `start()` at `== "user"`.

---

### B-2: No Budget Guards on New Work Dispatch

**Spec:** T4 (Architecture §4.2) — add budget checks to `pick_next_design()` and `_run_one_feature()` to block new work for over-budget projects.

**Reality:** Grep for `check_budget_before_new_work` in `orchestrator.py` returns zero results. New designs will be picked and new features launched even for projects already past their cost limit.

**Impact:** Budget enforcement is incomplete — it pauses *existing* workflows but never blocks *new* ones. An over-budget project can keep draining money indefinitely through new feature launches.

**Fix:** Add guard at top of `pick_next_design()` project loop and before `run_single_workflow` in `_run_one_feature()`:

```python
# pick_next_design, inside project iteration:
from src.core.cost_derivation import check_budget_before_new_work
if not check_budget_before_new_work(db, project.id):
    logger.info(f"Project {project.name} over budget — skipping")
    continue

# _run_one_feature, before run_single_workflow:
if not check_budget_before_new_work(db, project_id):
    return "budget_blocked"
```

---

### B-3: `cost_collection_service.py` NOT Created

**Spec:** T3 (Architecture §4.3) — new `src/services/cost_collection_service.py` with `CostCollector` ABC, `PiJsonlCollector`, `CodexStubCollector`, `collect_task_cost()` entry point, and `SessionCostCheckpoint` usage.

**Reality:** File does not exist. `SessionCostCheckpoint` table exists in schema but is never read or written anywhere.

**Impact:** There is **no mechanism** to get cost data from any CLI session into the `cost_entries` table. The `SessionCostCheckpoint` is dead schema.

**Fix:** Implement the collector module per T3 spec.

---

### B-4: `task_completion_service.py` NOT Wired

**Spec:** T10 (Architecture §4.10) — call `collect_task_cost(task_id)` on task completion inside `update_task_status(done)` handler path.

**Reality:** `task_completion_service.py` has zero references to `CostEntry`, `record_cost`, or any cost-related import.

**Impact:** Even if the collector existed, there's no trigger to run it. Cost collection never fires.

**Fix:** Add to `task_completion_service.py` in the done-handler path:

```python
try:
    from src.services.cost_collection_service import collect_task_cost
    collect_task_cost(task_id)
except Exception as e:
    logger.warning(f"Cost collection failed for task {task_id[:8]}: {e}")
```

---

### B-5: No `POST /cost-entries` API Endpoint

**Spec:** T6 (Architecture §4.6) — add `POST /cost-entries` endpoint to `autopilot_api.py`.

**Reality:** Endpoint does not exist. No `CostEntryCreate` Pydantic model.

**Impact:** The Pi extension (T9) and any external callers have no way to record cost entries through the API.

**Fix:** Add endpoint per T6 spec.

---

## FIX — Design Deviation

### F-1: Budget Pause Doesn't Terminate Active Agents

**Spec:** `_pause_project_workflows` must "terminate active agents (with `terminated_at` set)" (Architecture §4.4, design.md Budget Enforcement section).

**Reality:** `cost_derivation.py:309–327` only sets `wf.status = "paused"` and `wf.paused_by`. No `Agent` query, no `terminated_at` assignment.

**Impact:** Already-running agents continue consuming tokens even after the budget is exceeded.

**Fix:** Add agent termination inside `_pause_project_workflows`:

```python
active_agents = db.query(Agent).filter(
    Agent.workflow_id == wf.id,
    Agent.status.in_(["active", "working"]),
).all()
for agent in active_agents:
    agent.status = "terminated"
    agent.terminated_at = datetime.utcnow()
```

---

### F-2: `langchain_llm_client.py` Changes NOT Made

**Spec:** T7 (Architecture §4.7) — add `_invoke_and_record` helper, `usage.include=true` in `extra_body`, wire all 9 call sites.

**Reality:** No changes to `langchain_llm_client.py` at all.

**Impact:** Backend's own OpenRouter calls (task enrichment, guardian, conductor — ~9 call sites) generate no `cost_entries`. These are the calls that happen on every task regardless of CLI type.

**Fix:** Implement per T7 Part A spec.

---

### F-3: `ProjectUpdate` Model NOT Extended for `cost_limit_usd`

**Spec:** T6 (Architecture §4.2, §4.6) — extend `ProjectUpdate` Pydantic model with `cost_limit_usd`, wire to `PUT /projects/{id}` handler, add budget-pause clearing logic.

**Reality:** `ProjectUpdate` has no `cost_limit_usd` field. No logic to clear `"budget"`-paused workflows when limit raised.

**Impact:** Users cannot set project budgets via the API. Schema column (`cost_limit_usd`) exists but there's no way to write to it.

**Fix:** Extend `ProjectUpdate`, wire to handler, add budget-pause clearing per T5/T6 spec.

---

### F-4: Incidental Code Removal Not in Architecture Scope

The commit removes substantive functionality unrelated to cost tracking:

| File | Removed | Lines |
|------|---------|-------|
| `manager.py` | Duplicate-agent creation guard | lines 214–231 |
| `orchestrator.py` | Self-heal: re-queue designs with all-pending features | lines 1902–1933 |
| `orchestrator.py` | `_cap_out_review_phase` silent-fallthrough guard (`return None` → `return False`) | lines 5198–5241 |
| `test_orchestrator_helpers.py` | `TestCreatePhaseTaskReviewCap` (5 tests) | lines 4225–4413 |
| `test_autopilot_spec.py` | `TestGetMaxReviewRuns` + `TestReviewFindingsHistory` (8 tests) | lines 406–511 |

**Impact:** Removing the duplicate-agent guard can allow double-agent spawning. Removing the self-heal block means designs with all-pending features won't be re-queued after a crash. These are regressions unrelated to cost tracking.

**Fix:** Restore all removed code paths and their tests, or document intentional removals with rationale.

---

## DEFER — Nice to Have

### D-1: Collector Module (T3) vs API Endpoint (T6) Ordering

The architecture recommends implementing T3 before T6 since the collector is the primary data source and the API endpoint is secondary. The developer implemented neither; when picking up, T3 should land first.

### D-2: `reasoning_tokens` Added to CostEntry

The developer added `reasoning_tokens = Column(Integer, default=0)` to `CostEntry` — not in the architecture schema. This is a good addition (matches the pi JSONL schema's `usage.reasoning` field noted in design.md). Not a deviation, just note for future schemas.

### D-3: Extra Index on `cost_entries.recorded_at`

Added `ix_cost_entries_recorded_at` — not in architecture. Useful for time-range queries and dashboard display. Accept.

### D-4: `spec.py` Context Manager → Raw Session

`get_max_review_runs`, `get_review_findings_history`, `record_review_finding` changed from `get_db()` context manager to `DatabaseManager()` + manual session. This is unrelated to cost tracking and changes the session lifecycle pattern (no auto-rollback on exception). Not blocking, but worth noting as a regression risk.

### D-5: Codex Stub Collector (T11) and OpenCode Collector (T12)

Both are not implemented. T11 is trivial (log warning, return empty). T12 is gated on workflow.yaml check. Both are low priority and can be deferred without blocking the core cost tracking pipeline.

---

## Summary Table

| ID | Severity | Component | Brief |
|----|----------|-----------|-------|
| B-1 | BLOCKER | `orchestrator.py` | `paused_by` guards not generalized (`== "user"` → `is not None`) |
| B-2 | BLOCKER | `orchestrator.py` | No budget guards on `pick_next_design` / `_run_one_feature` |
| B-3 | BLOCKER | `cost_collection_service.py` | Module not created (no collector exists) |
| B-4 | BLOCKER | `task_completion_service.py` | No cost collection trigger on task completion |
| B-5 | BLOCKER | `autopilot_api.py` | No `POST /cost-entries` endpoint |
| F-1 | FIX | `cost_derivation.py` | Budget pause doesn't terminate active agents |
| F-2 | FIX | `langchain_llm_client.py` | No OpenRouter direct cost collection |
| F-3 | FIX | `autopilot_api.py` | `ProjectUpdate` not extended for `cost_limit_usd` |
| F-4 | FIX | Multiple | Incidental code removal unrelated to cost tracking |
| D-1 | DEFER | — | Collector-before-API ordering for next phase |
| D-2 | DEFER | `database.py` | `reasoning_tokens` addition (accept) |
| D-3 | DEFER | `database.py` | Extra `recorded_at` index (accept) |
| D-4 | DEFER | `spec.py` | Context manager pattern change |
| D-5 | DEFER | — | Codex stub + OpenCode collector |

**Counts: 5 BLOCKER, 4 FIX, 5 DEFER**

---

## What Was Done Well

- **Schema layer (T1)** is complete and correct: both tables, all rollup columns, migration following the exact `_migrate_*_column` pattern, indexes created.
- **Cost derivation module (T2)** faithfully mirrors `status_derivation.py`: self-healing rollup, `_pause_project_workflows` includes Phase 0 (`definition_id.in_`), budget enforcement check, `check_budget_before_new_work` helper exposed.
- **Test coverage** (31 tests) is comprehensive for what's implemented: model creation, nullable fields, indexes, record→derive flow, self-healing, budget enforcement, idempotent pause, migration column existence.
- All 31 tests pass.

---

## Recommended Fix Order (Critical Path)

1. **B-1**: `paused_by` generalization (3 one-line changes)
2. **F-1**: Add agent termination to `_pause_project_workflows`
3. **B-2**: Add `check_budget_before_new_work` calls in orchestrator
4. **F-4**: Restore removed code paths and tests
5. **B-3**: Create `cost_collection_service.py`
6. **B-5**: Add `POST /cost-entries` endpoint
7. **B-4**: Wire task completion trigger
8. **F-3**: Extend `ProjectUpdate` + handler

Steps 1–4 unblock budget enforcement correctness. Steps 5–8 unblock data flow.