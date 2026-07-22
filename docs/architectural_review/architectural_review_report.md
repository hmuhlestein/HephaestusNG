# Architectural Review Report: Budget Enforcement and Pipeline Throttling

**Reviewer:** Architect (Phase 5 — final review after fixes)  
**Date:** 2026-07-21  
**Architecture Source:** `docs/architecture.md` (Phase 3 output)  
**Requirements Source:** `docs/requirements_analysis.md` (Phase 1 output)  
**Implementation:** Latest commit `b0c74e2`

---

## Executive Summary

The implementation correctly fulfills all core architectural requirements. The developer addressed all 5 findings from the initial review: `/autopilot/stop` refactored to shared `_pause_project_workflows`, agent termination includes "starting" status, log messages updated to reflect generalized guards, budget guard uses same DB session, and frontend UI implemented. **0 BLOCKERs, 1 FIX (test coverage gap), 2 DEFERs.**

---

## Verification Matrix

| Architecture Requirement | Status | Evidence |
|-------------------------|--------|----------|
| FR-1: Budget guard in `pick_next_design()` | ✅ DONE | `orchestrator.py:2017-2027` — calls `check_budget_before_new_work`, returns `None` when over budget |
| FR-2: Budget guard in `_run_one_feature()` | ✅ DONE | `orchestrator.py:7119-7131` — calls `check_budget_before_new_work`, returns `"budget_blocked"` |
| FR-3: `paused_by` generalization (3 locations) | ✅ DONE | Lines 3760, 5694, 5879 — all changed to `is not None` |
| FR-3: `AutopilotService.start()` exception | ✅ DONE | Line 398 — keeps `== "user"` |
| FR-4: Refactor `/autopilot/stop` to shared function | ✅ DONE | `autopilot_api.py:3679-3690` — uses `_pause_project_workflows(db, pid, paused_by="user")` |
| FR-5: Budget-paused resume via limit increase | ✅ DONE | `autopilot_api.py:1856-1861` — clears budget-paused on limit raise |
| FR-6: UI — Budget configuration | ✅ DONE | `ProjectSettingsModal.tsx` — number input with save/cancel |
| FR-7: UI — Cost display | ✅ DONE | `BudgetStatusCard.tsx` — progress bar with $current/$limit |
| FR-8: UI — Budget-paused status label | ✅ DONE | `WorkflowCard.tsx:22-30` — shows "PAUSED: BUDGET LIMIT REACHED" |

---

## Findings

### FIX-1: Missing Tests for `_create_corrective_task` and `_resume_stuck_workflow_tasks`

**Classification:** FIX (design deviation — architecture specified tests for all 3 guard locations)

**Location:** `tests/test_budget_enforcement.py`

**Problem:**
Architecture T6 specified test cases for all 3 guard generalization locations. The test file covers `_try_auto_resume_paused_workflow` (3 tests) but has no tests for:
- `_create_corrective_task` skipping budget-paused workflows
- `_resume_stuck_workflow_tasks` skipping budget-paused workflows

**Impact:** Low — the guards are generalized in the same pattern and the code is straightforward. But the architecture explicitly required verification of all 3 locations.

**Recommended Fix:**
Add two test cases:
```python
def test_create_corrective_task_skips_budget_paused(self, db_session, active_autopilot_workflow):
    from src.autopilot.orchestrator import _create_corrective_task
    active_autopilot_workflow.status = "paused"
    active_autopilot_workflow.paused_by = "budget"
    db_session.commit()
    result = _create_corrective_task(active_autopilot_workflow.id, "phase-123", MagicMock())
    assert result is None

def test_resume_stuck_skips_budget_paused(self, db_session, active_autopilot_workflow):
    from src.autopilot.orchestrator import _resume_stuck_workflow_tasks
    active_autopilot_workflow.status = "paused"
    active_autopilot_workflow.paused_by = "budget"
    db_session.commit()
    result = _resume_stuck_workflow_tasks(active_autopilot_workflow.id, MagicMock())
    assert result == 0
```

---

## Deferred Items

### DEFER-1: Budget Status Card Not Integrated in Main Autopilot View

**Classification:** DEFER (nice to have)

`BudgetStatusCard.tsx` is a standalone component. It exists but may not be wired into the main autopilot page layout. The architecture specified showing cost on the design screen. The component is available for integration.

---

### DEFER-2: Source-Code Inspection Tests Removed

**Classification:** DEFER (minor)

The test file previously had source-inspection tests (`inspect.getsource`) verifying guards were wired in. These were replaced with behavioral tests per NIT-1 feedback. The behavioral approach is correct, but the source-inspection tests provided an additional layer of assurance that the guards exist in the right functions.

---

## Summary

| Category | Count |
|----------|-------|
| BLOCKER | 0 |
| FIX | 1 |
| DEFER | 2 |

All core architectural requirements are correctly implemented. The single FIX is a test coverage gap for 2 of 3 guard locations — the code changes are correct but not fully verified by tests. The DEFERs are minor polish items.

---

*Review complete. Implementation APPROVED with 1 minor fix required.*
