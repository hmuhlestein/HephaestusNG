# Architectural Review Report

**Reviewer:** Architecture Design Agent (Phase 5)  
**Date:** 2026-07-21  
**Phase:** architectural_review  
**Branch:** `feature/des-91c8-cost-derivation`  
**Reviewed Commit:** `da1da35` (phase 4 development complete)  
**Architecture Reference:** `docs/architecture.md`  
**Requirements Reference:** `docs/requirements_analysis.md`

---

## Executive Summary

The Phase 4 development is **substantially compliant** with the architecture. All core backend components (cost derivation, collection service, budget enforcement, API endpoints) are correctly implemented. The main gap is **frontend integration** — components exist but are not wired into any page. One **data integrity issue** was found in the budget resume flow. Overall verdict: **CONDITIONAL PASS** — the BLOCKER and FIX items must be addressed before Phase 6.

| Severity | Count | Summary |
|----------|-------|---------|
| BLOCKER | 1 | Frontend components exist but are never rendered in any page |
| FIX | 4 | `paused_at` not cleared; `_run_one_feature` returns "failed"; `task_id` not threaded for system-wide LLM calls; early `return None` in `pick_next_design` |
| DEFER | 4 | Missing pagination, test coverage gaps, deprecated `utcnow()` usage |

---

## BLOCKER Findings

### B-1: Frontend Cost Components Exist But Are Never Rendered

**Severity:** BLOCKER  
**Location:** `frontend/src/pages/` (all pages), `frontend/src/components/` (parent components)  
**Architecture Ref:** Section 2.7 (Frontend), Task T7

**Description:** The developer created 6 well-structured cost components:
- `CostDisplay.tsx` — ✅ Good implementation with progress bar, color coding, responsive
- `ProjectCostSummary.tsx` — ✅ Includes budget status, configure link
- `FeatureCostBadge.tsx` — ✅ Color-coded by cost threshold
- `DesignCostRow.tsx` — ✅ Clean list item display
- `BudgetPausedLabel.tsx` — ✅ Red badge with icon
- `index.ts` barrel export — ✅ Proper exports

However, **NONE of these components are imported or rendered** in any actual page:
- `frontend/src/pages/Dashboard.tsx` — does not import `ProjectCostSummary`
- No `DesignScreen.tsx`, `FeatureCard.tsx`, or `DesignList.tsx` files exist to integrate into

The components are effectively dead code. Users cannot see cost data in the UI.

**Architecture Violation:** Section 2.7.1 states these components must be integrated:
- CostDisplay on design screen
- FeatureCostBadge on feature cards
- DesignCostRow in design list
- ProjectCostSummary on dashboard

**Recommended Fix:**  
Import and render at least `ProjectCostSummary` in `Dashboard.tsx`. Even if a full design screen doesn't exist, the dashboard is the primary overview surface. Add `BudgetPausedLabel` rendering for workflows with `paused_by === "budget"` in the workflow status display.

---

## FIX Findings

### F-1: `paused_at` Not Cleared in Budget Resume Flow

**Severity:** FIX  
**Location:** `src/mcp/autopilot_api.py:1861-1865`  
**Architecture Ref:** Section 3.3 (Budget Resume Flow)

**Description:** The PUT `/projects/{id}` handler correctly clears `wf.paused_by`, `wf.status`, and `wf.status_reason` when raising the limit. However, `wf.paused_at` is **NOT** cleared:

```python
for wf in budget_paused:
    wf.paused_by = None
    wf.status = "active"
    wf.status_reason = None
    # Missing: wf.paused_at = None
```

Meanwhile, `cost_derivation.py:326` sets `wf.paused_at = datetime.utcnow()` when pausing. The `paused_at` column (defined at `database.py:435`) retains the stale timestamp. This causes:
1. Data inconsistency — active workflows show a `paused_at` timestamp
2. Potential logic issues in orchestrator code that checks `paused_at` (e.g., `orchestrator.py:2902` uses `paused_at` for timeout detection)

**Recommended Fix:** Add `wf.paused_at = None` to the budget resume loop:
```python
for wf in budget_paused:
    wf.paused_by = None
    wf.status = "active"
    wf.status_reason = None
    wf.paused_at = None
```

### F-2: `_run_one_feature` Returns "failed" for Budget-Blocked Features

**Severity:** FIX  
**Location:** `src/autopilot/orchestrator.py:7059`  
**Architecture Ref:** Section 2.5 (Budget Enforcement), Task T2

**Description:** When a feature is blocked by budget, `_run_one_feature` returns `"failed"`:

```python
if not check_budget_before_new_work(budget_db, project_id):
    logger.warning(f"[BUDGET] Cannot launch feature ...")
    return "failed"
```

This is semantically incorrect — the feature isn't "failed", it's budget-blocked. The `"failed"` return value may trigger incorrect state transitions in callers (e.g., marking the feature as permanently failed in the database, triggering corrective action, or incrementing failure counters).

**Recommended Fix:** Return a distinct status or handle more gracefully. Options:
1. Return `"skipped"` with a reason field
2. Add a `budget_blocked` return value that callers can handle
3. At minimum, do NOT set the feature's DB status to "failed" — just return early without state mutation

### F-3: `task_id` Not Threaded to System-Wide LLM Calls

**Severity:** FIX  
**Location:** `src/interfaces/langchain_llm_client.py`  
**Architecture Ref:** Task T5 (Thread `task_id` into LLM Methods)

**Description:** The architecture specifies that `_invoke_and_record()` accepts `task_id` for cost attribution. However, several methods call it without passing `task_id`:

| Method | `task_id` passed? | Impact |
|--------|-------------------|--------|
| `classify_complexity()` | ❌ No (design-level) | Acceptable — not task-scoped |
| `enrich_task()` | ❌ No (has `task_id` available from context) | Cost not attributed to task |
| `resolve_ticket_clarification()` | ❌ No | Cost not attributed |
| `analyze_agent_state()` | ❌ No | Cost not attributed (task_info available) |
| `generate_agent_prompt()` | N/A — no LLM call | Acceptable |
| `analyze_agent_trajectory()` | ❌ No | Cost not attributed (task_info available) |
| `analyze_system_coherence()` | ❌ No (system-wide) | Acceptable — no task scope |
| `review_qa_report()` | ❌ No | Cost not attributed to task |

**Specific gap:** `enrich_task()`, `analyze_agent_state()`, `analyze_agent_trajectory()`, and `review_qa_report()` all have task context available but don't pass `task_id` to `_invoke_and_record()`. This means these costs go into the `cost_entries` table with `task_id=NULL`, attributed to "overhead" rather than the specific task.

**Recommended Fix:** Thread `task_id` through at least `enrich_task()` (add `task_id: Optional[str] = None` parameter) and the methods that receive `task_info` dict. For `analyze_system_coherence()`, the system-wide attribution is acceptable.

### F-4: `pick_next_design` Returns `None` Immediately on Budget Check

**Severity:** FIX  
**Location:** `src/autopilot/orchestrator.py:2019-2021`  
**Architecture Ref:** Task T2 (Wire Budget Checks into Orchestrator)

**Description:** The budget guard in `pick_next_design()` returns `None` immediately when the project is over budget:

```python
if not check_budget_before_new_work(db, project.id):
    logger.info(f"[BUDGET] pick_next_design: project {project.id[:8]} over budget — skipping")
    return None
```

This is correct if there's only one project. However, if there are multiple active projects (up to `MAX_PARALLEL_FEATURES=4`), this early `return None` will **block ALL designs**, even those from projects under budget. The function should `continue` to check other projects, not `return None`.

Looking at the broader function structure, the budget check is inside the per-project loop, but the `return None` exits the entire function. This means an over-budget project poisons the entire design selection process.

**Recommended Fix:** Change `return None` to `continue` to allow checking other projects:
```python
if not check_budget_before_new_work(db, project.id):
    logger.info(f"[BUDGET] pick_next_design: project {project.id[:8]} over budget — skipping")
    continue  # Check next project
```

---

## DEFER Findings

### D-1: No Pagination on Cost Query Endpoints

**Severity:** DEFER  
**Location:** `src/mcp/autopilot_api.py` (all `/costs` endpoints)  
**Architecture Ref:** Task T6

**Description:** The cost query endpoints return all data without pagination. The task cost endpoint limits entries to 100 (`.limit(100)`), but the workflow/feature/design/project endpoints return ALL child entities. For projects with many designs or features, this could produce very large responses.

**Recommended Enhancement:** Add `limit` and `offset` query parameters, consistent with other endpoints in the API.

### D-2: `datetime.utcnow()` Deprecation

**Severity:** DEFER  
**Location:** `src/core/cost_derivation.py:326`  
**Description:** `datetime.utcnow()` is deprecated in Python 3.12+. All 52 tests emit deprecation warnings. While functionally correct, this should be migrated to `datetime.now(datetime.UTC)`.

### D-3: Test Coverage for Orchestrator Integration

**Severity:** DEFER  
**Location:** `tests/test_budget_enforcement_integration.py`  
**Architecture Ref:** Task T12

**Description:** The test file is comprehensive (516 lines, 9 test classes) and covers the critical paths well. However, the `TestBudgetAutoResumeBlocked` class only asserts on state — it doesn't actually call `_try_auto_resume_paused_workflow()` to verify the guard behavior. The tests verify the `paused_by` value but not the actual guard function behavior.

### D-4: LangChain Response Metadata Smoke Test

**Severity:** DEFER  
**Location:** `src/interfaces/langchain_llm_client.py:356-367`  
**Architecture Ref:** Open Questions (Q2)

**Description:** The `_invoke_and_record()` method extracts cost from `response_metadata.token_usage.cost.total`. The architecture noted this needs a smoke test to verify `usage.include=true` survives LangChain's response parsing. No smoke test was performed. If the field path doesn't match OpenRouter's actual response format, all direct OpenRouter cost recording will silently produce $0.

---

## Compliance Matrix

| Architecture Requirement | Status | Notes |
|-------------------------|--------|-------|
| T1: Generalize `paused_by` guards | ✅ PASS | All 3 locations changed to `is not None`, `start()` correctly kept |
| T2: Wire budget checks into orchestrator | ⚠️ CONDITIONAL | `pick_next_design` has early-return bug (F-4) |
| T3: Limit raise clears budget pause | ⚠️ CONDITIONAL | `paused_at` not cleared (F-1) |
| T4: Wire LangChainLLMClient call sites | ✅ PASS | All methods use `_invoke_and_record()` |
| T5: Thread `task_id` into LLM methods | ❌ FAIL | `task_id` not passed to most methods (F-3) |
| T6: Create API cost query endpoints | ✅ PASS | All 5 endpoints implemented with proper schemas |
| T7: Create frontend cost components | ⚠️ CONDITIONAL | Components created but never rendered (B-1) |
| T8: Create frontend budget config | ✅ PASS | Budget display added to ProjectSettingsModal |
| T9: Create Pi extension | ✅ PASS | Full implementation with TUI display, graceful error handling |
| T10: Create unit tests for cost_derivation | ✅ PASS | Existing `test_cost_tracking.py` (750 lines) |
| T11: Create integration tests for collection | ❌ PARTIAL | Not implemented (skipped in Phase 4) |
| T12: Create integration tests for budget enforcement | ✅ PASS | `test_budget_enforcement_integration.py` (516 lines) |

---

## Architectural Pattern Compliance

| Pattern | Status | Evidence |
|---------|--------|----------|
| Append-only ledger | ✅ | `CostEntry` never mutated; only inserted |
| Self-healing derivation | ✅ | All `derive_*()` functions check and correct mismatches |
| Collection on completion | ✅ | `collect_task_cost()` called from `task_completion_service` |
| Checkpoint by session_id | ✅ | `SessionCostCheckpoint` keyed by `session_id` |
| Idempotent budget pause | ✅ | `_pause_project_workflows` finds nothing on second call |
| Phase 0 included in pause | ✅ | `definition_id.in_(["autopilot", "autopilot-phase0"])` |
| No new dependencies | ✅ | Pure SQLAlchemy + stdlib; only `typescript` dev dependency for extension |

---

## Code Quality Assessment

| Aspect | Score | Notes |
|--------|-------|-------|
| Code organization | ✅ Excellent | Clean separation: derivation, collection, API, frontend |
| Error handling | ✅ Good | Graceful fallbacks, warning logs for unsupported sources |
| Security | ✅ Good | Path traversal prevention in session discovery |
| Naming conventions | ✅ Consistent | `[BUDGET]`, `[COST-HEAL]`, `[COST-COLLECT]` prefixes |
| Documentation | ✅ Good | Docstrings on all public functions, README for extension |
| Test coverage | ⚠️ Adequate | Core derivation well-tested, collection not tested |

---

## Recommended Priority Order for Fixes

1. **B-1** (HIGH): Wire frontend components into Dashboard — makes cost data visible to users
2. **F-1** (MEDIUM): Clear `paused_at` in budget resume — data integrity
3. **F-4** (MEDIUM): Fix `pick_next_design` early return — multi-project correctness
4. **F-3** (LOW): Thread `task_id` through LLM methods — cost attribution accuracy
5. **F-2** (LOW): Fix `_run_one_feature` return value — semantic correctness

---

*Report generated by Architecture Design Agent (Phase 5) on 2026-07-21*
