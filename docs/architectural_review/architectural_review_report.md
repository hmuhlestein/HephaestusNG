# Architectural Review Report: Budget Enforcement and Pipeline Throttling

**Reviewer:** Architect (Phase 5 re-invocation)  
**Date:** 2026-07-21  
**Architecture Source:** `docs/architecture.md` (Phase 3 output)  
**Requirements Source:** `docs/requirements_analysis.md` (Phase 1 output)  
**Implementation Commit:** `932134a` (Phase 4 development)

---

## Executive Summary

The developer implemented the core backend enforcement logic correctly: budget guards in `pick_next_design()` and `_run_one_feature()`, `paused_by` generalization at all 3 orchestrator locations, and the `AutopilotService.start()` exception is preserved. The 20 tests in `test_budget_enforcement.py` cover the key scenarios.

However, there are **5 FIX findings** (design deviations) and **2 DEFER findings** (out of scope items). The most significant FIX is that the `/autopilot/stop` endpoint was **not** refactored to use the shared `_pause_project_workflows()` function — this was an explicit requirement (FR-4) and a bug fix for the Phase 0 gap. No BLOCKERs (architecture violations) were found.

---

## Findings

### FIX-1: `/autopilot/stop` Endpoint NOT Refactored to Shared Function

**Classification:** FIX (design deviation — architecture explicitly required this)

**Location:** `src/mcp/autopilot_api.py`, lines 3670-3724

**Architecture Requirement (FR-4):**
> Replace inline pause logic in `/autopilot/stop` with call to `_pause_project_workflows(db, project_id, "user")`

**What Was Implemented:**
The endpoint still uses inline logic:
```python
query = db.query(Workflow).filter_by(definition_id="autopilot").filter(Workflow.status.in_(["active", "running"]))
```

**Impact:**
- Phase 0 workflows (`definition_id == "autopilot-phase0"`) are NOT paused when user clicks Stop
- This is the exact bug the architecture identified: "The existing endpoint's `definition_id == "autopilot"` filter misses Phase 0"
- The shared `_pause_project_workflows()` in `cost_derivation.py` already handles both definition IDs

**Recommended Fix:**
Replace the inline pause logic (lines ~3670-3724) with:
```python
from src.core.cost_derivation import _pause_project_workflows
for stopped_project_id in stopped_project_ids:
    _pause_project_workflows(db, stopped_project_id, paused_by="user")
# Keep stale agent cleanup as separate step (not in shared function)
```

---

### FIX-2: `_pause_project_workflows` Missing "starting" Agent Status

**Classification:** FIX (design deviation — inconsistency with existing codebase)

**Location:** `src/core/cost_derivation.py`, line ~340

**Problem:**
The shared `_pause_project_workflows()` filters agents with:
```python
Agent.status.in_(["working", "idle"])
```

But the `/autopilot/stop` inline code (and rest of codebase) uses:
```python
Agent.status.in_(["working", "starting", "idle"])
```

**Impact:**
- Agents in "starting" status are not terminated during budget pause
- These agents may continue launching work after the budget is exceeded

**Recommended Fix:**
Change the filter to include "starting":
```python
Agent.status.in_(["working", "starting", "idle"])
```

---

### FIX-3: Log Messages Still Say "user-paused" for Generalized Guards

**Classification:** FIX (design deviation — misleading log output)

**Locations:**
- `src/autopilot/orchestrator.py`, line 5703: `f"[CORRECTIVE-TASK] Workflow {workflow_id[:8]} is user-paused — "`
- `src/autopilot/orchestrator.py`, line 5884: `f"[RESUME-STUCK] Workflow {workflow_id[:8]} is user-paused — skipping"`

**Problem:**
The guards were generalized from `== "user"` to `is not None` (now also covers "budget", "system", etc.), but the log messages still say "user-paused". This will confuse operators debugging budget enforcement.

**Recommended Fix:**
Change log messages to reflect the generalized guard:
```python
f"[CORRECTIVE-TASK] Workflow {workflow_id[:8]} is deliberately paused (paused_by={wf.paused_by}) — skipping"
f"[RESUME-STUCK] Workflow {workflow_id[:8]} is deliberately paused (paused_by={wf.paused_by}) — skipping"
```

---

### FIX-4: Missing Tests for `_create_corrective_task` and `_resume_stuck_workflow_tasks`

**Classification:** FIX (design deviation — architecture specified tests for all 3 guard locations)

**Location:** `tests/test_budget_enforcement.py`

**Architecture Requirement (T6):**
> Test cases:
> 3. Self-heal guards: budget-paused workflow not auto-resumed by `_try_auto_resume_paused_workflow`, `_create_corrective_task`, or `attempt_recovery`

**What Was Implemented:**
Tests only cover `_try_auto_resume_paused_workflow`. No tests for:
- `_create_corrective_task` skipping budget-paused workflows
- `_resume_stuck_workflow_tasks` (attempt_recovery path) skipping budget-paused workflows

**Recommended Fix:**
Add two more test cases to `TestPausedByGeneralization`:
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

### FIX-5: Budget Guard in `_run_one_feature` Uses Separate DB Session

**Classification:** FIX (design deviation — potential stale read)

**Location:** `src/autopilot/orchestrator.py`, lines 7119-7131

**Problem:**
The implementation opens a new `get_db()` context for the budget check:
```python
with get_db() as budget_db:
    if not check_budget_before_new_work(budget_db, project_id):
```

The architecture specified checking within the existing DB session. The function already has a `db` session from earlier context. Using a separate session could read stale data if another thread updated `cost_total_usd` between the two sessions.

**Impact:**
- Low — SQLite WAL mode handles concurrent reads, and the check is conservative (blocks if over budget)
- But architecturally inconsistent with the "same session" pattern used elsewhere

**Recommended Fix:**
Use the existing `db` session from the earlier context block, or restructure to avoid the extra session.

---

## Deferred Items

### DEFER-1: Frontend UI Not Implemented

**Classification:** DEFER (out of scope for this PR — backend enforcement is complete)

**What's Missing:**
- `cost_limit_usd` number input in `ProjectSettingsModal.tsx`
- Cost display on `DesignQueuePanel.tsx` or `PipelineStatusCard.tsx`
- "Paused: budget limit reached" label for budget-paused workflows

**Rationale:**
The requirements analysis listed these as "❌ NOT IMPLEMENTED" and they were part of the target state. However, the architecture document's section 3 (File Change Map) explicitly marks T8 (UI) as "❌ DEFERRED". The backend enforcement layer is complete and functional without the UI — users can set `cost_limit_usd` via the API directly.

**Note:** The frontend already has cost display in `FeatureDetailModal.tsx` and `FeatureGallery.tsx` from the parent cost tracking schema feature. The budget-specific UI (limit input, pause label) is the only missing piece.

---

### DEFER-2: Budget Guard Doesn't Use Inline Import Pattern Consistently

**Classification:** DEFER (minor code style)

**Location:** `src/autopilot/orchestrator.py`, lines 7119-7121

The budget guard in `_run_one_feature` does:
```python
from src.core.cost_derivation import check_budget_before_new_work
with get_db() as budget_db:
```

While the guard in `pick_next_design` does:
```python
from src.core.cost_derivation import check_budget_before_new_work
if not check_budget_before_new_work(db, project.id):
```

The first uses an inline import (consistent with the file's pattern), the second also does inline import. Both are fine, but the `_run_one_feature` version opens a new session while `pick_next_design` reuses the existing one. Not a bug, just inconsistent.

---

## Verification Matrix

| Architecture Requirement | Status | Notes |
|-------------------------|--------|-------|
| FR-1: Budget guard in `pick_next_design()` | ✅ DONE | Correctly implemented with logging |
| FR-2: Budget guard in `_run_one_feature()` | ✅ DONE | Returns "budget_blocked", updates feature status |
| FR-3: `paused_by` generalization (3 locations) | ✅ DONE | All 3 guards changed to `is not None` |
| FR-3: `AutopilotService.start()` exception | ✅ DONE | Keeps `== "user"` — preserved correctly |
| FR-4: Refactor `/autopilot/stop` to shared function | ❌ NOT DONE | Still uses inline logic, Phase 0 gap persists |
| FR-5: Budget-paused resume via limit increase | ✅ ALREADY DONE | Was implemented in parent feature |
| FR-6: UI — Budget configuration | ❌ DEFERRED | Not in scope per architecture §3 |
| FR-7: UI — Cost display | ❌ DEFERRED | Not in scope per architecture §3 |
| FR-8: UI — Budget-paused status label | ❌ DEFERRED | Not in scope per architecture §3 |
| Test coverage for all guard locations | ⚠️ PARTIAL | Only `_try_auto_resume` tested, missing `_create_corrective_task` and `_resume_stuck` |
| Agent termination includes "starting" status | ❌ MISSING | `_pause_project_workflows` omits "starting" |

---

## Summary

| Category | Count |
|----------|-------|
| BLOCKER | 0 |
| FIX | 5 |
| DEFER | 2 |

The implementation is solid for the core enforcement path. The most important FIX is FR-4 (refactor `/autopilot/stop`) which was an explicit requirement and fixes a real bug (Phase 0 gap). FIX-2 (missing "starting" status) is a functional gap. FIX-3 and FIX-4 are correctness/quality issues. FIX-5 is minor.

The DEFER items are explicitly out of scope per the architecture document's file change map.

---

*Review complete. Implementation approved with 5 fixes required before merge.*
