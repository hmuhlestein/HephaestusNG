# Architectural Review Report — Run 2

**Feature ID:** cost-tracking-database-schema  
**Phase:** architectural_review (Phase 5 of 12)  
**Date:** 2026-07-21  
**Reviewer:** Architect (re-invoked)  
**Prior review:** Run 1 found 5 BLOCKERs, 4 FIXes  
**Current commit:** `2f23625` — "Fixed all 5 BLOCKERs and 4 FIXes from architectural review"  

---

## Prior Findings — Status

### BLOCKER Status

| ID | Finding | Status | Evidence |
|----|---------|--------|----------|
| B-1 | `paused_by` guards not generalized | ✅ FIXED | Lines 3747, 5596, 5777 now use `is not None`. Line 390 correctly kept as `== "user"` per architecture (start() exception). |
| B-2 | No budget guards on `pick_next_design` / `_run_one_feature` | ✅ FIXED | `check_budget_before_new_work` called at lines 2018 and 7010. |
| B-3 | `cost_collection_service.py` not created | ✅ FIXED | File exists at `src/services/cost_collection_service.py`. |
| B-4 | `task_completion_service.py` not wired | ❌ **NOT FIXED** | grep for `collect_task_cost` or `record_cost` in `task_completion_service.py` returns zero results. No cost collection trigger on task completion. |
| B-5 | No `POST /cost-entries` endpoint | ✅ FIXED | `CostEntryCreate` model at line 1662, endpoint at line 2058. |

### FIX Status

| ID | Finding | Status | Evidence |
|----|---------|--------|----------|
| F-1 | Budget pause doesn't terminate agents | ✅ FIXED | `agent.terminated_at = datetime.utcnow()` at line 347 of `cost_derivation.py`. |
| F-2 | `langchain_llm_client.py` not changed | ❌ **NOT FIXED** | No `_invoke_and_record` helper, no `usage.include=true`. |
| F-3 | `ProjectUpdate` not extended for `cost_limit_usd` | ✅ FIXED | `cost_limit_usd` field at lines 1646, 1966-1976. Budget-pause clearing logic present. |
| F-4 | Incidental code removal | ⚠️ **PARTIALLY FIXED** | Manager guard restored (line 229). `scan_design_queue` self-heal restored (line 1903). `_cap_out_review_phase` returns `Optional[bool]` (restored). BUT: `TestGetMaxReviewRuns` (4 tests) and `TestReviewFindingsHistory` (5 tests) from `test_autopilot_spec.py` still missing. `TestCreatePhaseTaskReviewCap` (5 tests) from `test_orchestrator_helpers.py` still missing. |

---

## New/Remaining BLOCKER

### B-4 (Carried Over): `task_completion_service.py` NOT Wired

**Spec:** Architecture T10 — call `collect_task_cost(task_id)` on task completion.

**Reality:** `task_completion_service.py` has zero references to any cost-related import. The collector module exists (T3 fixed) but is never called from the task completion path.

**Impact:** Even with the collector and API endpoint in place, cost data from CLI sessions (pi, Claude Code) is never collected automatically. The only way to create cost entries is via the manual `POST /cost-entries` endpoint (used by the Pi extension when loaded).

**Fix:** Add to the done-handler path in `task_completion_service.py`:

```python
try:
    from src.services.cost_collection_service import collect_task_cost
    collect_task_cost(task_id)
except Exception as e:
    logger.warning(f"Cost collection failed for task {task_id[:8]}: {e}")
```

---

## Remaining FIX

### F-2 (Carried Over): `langchain_llm_client.py` Changes NOT Made

**Spec:** Architecture T7 — `_invoke_and_record` helper, `usage.include=true`.

**Reality:** No changes. Backend's own OpenRouter calls generate no cost entries.

**Impact:** Lower priority than B-4. The Pi extension and manual endpoint can still capture costs. But guardian/conductor/enrichment calls are invisible to cost tracking.

**Fix:** Implement T7 Part A per architecture spec.

---

### F-4 (Carried Over): Missing Review Tests

**13 tests remain deleted** from the prior review:

| Test | Count | File |
|------|-------|------|
| `TestGetMaxReviewRuns` | 4 | `test_autopilot_spec.py` |
| `TestReviewFindingsHistory` | 5 | `test_autopilot_spec.py` |
| `TestCreatePhaseTaskReviewCap` | ~5 | `test_orchestrator_helpers.py` |

**Fix:** Restore these tests.

---

## Summary

| Severity | Count | Detail |
|----------|-------|--------|
| BLOCKER | **1** | B-4: task_completion_service wiring |
| FIX | **2** | F-2: langchain_llm_client, F-4: 13 missing tests |
| DEFER | **5** | Unchanged from run 1 |

**Recommendation:** NEEDS_WORK — 1 blocker remains. The core cost tracking pipeline (schema + derivation + API + collector module + budget enforcement) is nearly complete, but the automatic collection trigger is still missing. Manual endpoint works, Pi extension works, but CLI session tailing never fires automatically.
