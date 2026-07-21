# Adversarial Review Report: Budget Enforcement and Pipeline Throttling (Run 2)

**Reviewer:** Hephaestus Adversarial Review (Phase 6, re-verification)  
**Date:** 2026-07-21  
**Commit Under Review:** `bbe52e7` (fix commit addressing prior run's findings)  
**Prior Review Commit:** `92b90af` (2 BLOCKERs, 4 WARNINGs, 3 NITs found)

---

## Executive Summary

The fix commit `bbe52e7` **correctly resolves both BLOCKERs and 4 of 6 non-blocking findings** from the prior adversarial review. All 78 tests pass. No new BLOCKERs or WARNINGs were introduced by the fixes.

Two lower-severity findings from the prior run remain unfixed:
- **WARNING-4** (OpenRouter direct costs without entity links bypass budget enforcement)
- **NIT-2** (`_extract_session_id` fragile tmux name parsing)

Neither is a merge blocker — WARNING-4 is a design limitation with low practical risk (the Pi extension always provides entity links), and NIT-2 is a code quality issue that doesn't affect correctness when the tmux naming convention is followed.

---

## Prior Findings Verification

### BLOCKER-1: `/autopilot/stop` Phase 0 Gap — ✅ FIXED

**Prior Issue:** The `/autopilot/stop` endpoint used inline `filter_by(definition_id="autopilot")`, missing Phase 0 workflows (`definition_id="autopilot-phase0"`). Phase 0 agents continued running and spending tokens after user clicked "Stop".

**Fix Applied:** Replaced inline pause logic with shared `_pause_project_workflows(db, pid, paused_by="user")` in `src/mcp/autopilot_api.py` line 3671-3675.

**Verification:**
```python
# src/mcp/autopilot_api.py, line 3671-3675
from src.core.cost_derivation import _pause_project_workflows
with get_db() as db:
    for pid in stopped_project_ids:
        paused = _pause_project_workflows(db, pid, paused_by="user")
        terminated_count += paused
    db.commit()
```

The shared function filters `definition_id.in_(["autopilot", "autopilot-phase0"])`, correctly including Phase 0 workflows.

---

### BLOCKER-2: `_run_one_feature` Budget Guard Used Separate DB Session — ✅ FIXED

**Prior Issue:** The budget guard in `_run_one_feature` opened a second `get_db()` context (`budget_db`), allowing stale reads when another thread recorded costs between the two sessions.

**Fix Applied:** Budget guard moved inside the existing `with get_db() as db:` block at line 7007, reusing the same session.

**Verification:**
```python
# src/autopilot/orchestrator.py, line 7007-7070
with get_db() as db:
    feat_record = db.query(Feature).filter_by(...)
    # ... resume support ...
    
    # Budget guard: same DB session, no stale reads
    if project_id:
        from src.core.cost_derivation import check_budget_before_new_work
        if not check_budget_before_new_work(db, project_id):  # ← uses same `db`
            ...
            return "budget_blocked"
```

The guard now reads from the same session that already has the feature record loaded, eliminating the race window.

---

### WARNING-1: Missing "starting" Agent Status — ✅ FIXED

**Prior Issue:** `_pause_project_workflows` filtered `Agent.status.in_(["working", "idle"])`, excluding agents in "starting" state. These agents could transition to "working" and continue spending past the budget.

**Fix Applied:** Changed filter to `Agent.status.in_(["working", "starting", "idle"])` in `src/core/cost_derivation.py` line 343. Also added `STARTING = "starting"` to `AgentStatus` class and its CheckConstraint in `src/core/database.py`.

**Test Added:** `test_terminates_starting_agents` — creates a "starting" agent, pauses project, verifies agent is terminated.

---

### WARNING-2: Misleading Log Messages — ✅ FIXED

**Prior Issue:** Log messages at lines 5703 and 5884 said "user-paused" for all non-null `paused_by` values, including "budget".

**Fix Applied:** Both messages now include the actual `paused_by` value:
- Line 5703: `f"[CORRECTIVE-TASK] Workflow {workflow_id[:8]} is deliberately paused (paused_by={wf.paused_by}) — skipping corrective task"`
- Line 5884: `f"[RESUME-STUCK] Workflow {workflow_id[:8]} is deliberately paused (paused_by={wf.paused_by}) — skipping"`

---

### WARNING-3: Stale `status_reason` on User Pause — ✅ FIXED

**Prior Issue:** When `_pause_project_workflows` paused for "user", it didn't clear `status_reason`, leaving stale "Budget limit reached" text.

**Fix Applied:** Added `elif paused_by == "user": wf.status_reason = None` in `src/core/cost_derivation.py` line 329-330.

**Test Added:** `test_user_pause_clears_stale_budget_reason` — sets stale reason, pauses by user, verifies reason is cleared.

---

### WARNING-4: Unlinked Costs Bypass Budget Enforcement — ❌ STILL OPEN

**Issue:** `record_cost()` in `src/core/cost_derivation.py` accepts `workflow_id=None` and `task_id=None`. When both are None, no derivation rollup occurs and no budget enforcement fires. The `POST /cost-entries` API endpoint allows this.

**Impact:** Low in practice — the Pi extension always provides `task_id` and `agent_id`. But a direct API caller could submit costs without entity links, and those costs would be recorded in the ledger but invisible to budget enforcement.

**Current Code (unchanged):**
```python
# src/core/cost_derivation.py, record_cost()
if task_id:
    derive_task_cost(db, task_id, write_back=True)
if workflow_id:
    derive_workflow_cost(db, workflow_id, write_back=True)
# Both skipped when None → no budget check
```

**Recommended Fix:** Either:
1. Require at least one of `task_id`/`workflow_id` in the API validator, or
2. When both are None, still derive project cost from `agent_id` (look up agent → task → workflow), or
3. Maintain a separate "unlinked costs" counter on the project

**Not a merge blocker** — the practical risk is limited to direct API callers, not the normal pipeline flow.

---

### NIT-1: Source Inspection Tests — ✅ FIXED

**Prior Issue:** Two tests used `inspect.getsource()` to check for string presence instead of testing actual behavior.

**Fix Applied:** Replaced with behavioral tests:
- `test_check_budget_allows_no_limit_set` — verifies `True` when no limit set
- `test_budget_guard_blocks_at_exact_limit` — verifies `False` when cost equals limit

---

### NIT-2: Fragile `_extract_session_id` — ❌ STILL OPEN

**Issue:** `_extract_session_id` in `src/services/cost_collection_service.py` parses tmux session names by splitting on hyphens: `return "-".join(parts[1:])`. If the naming convention changes, extraction fails silently and cost collection is skipped.

**Impact:** Silent cost data loss for agents whose tmux names don't match the expected format. Low risk as long as the naming convention in `src/autopilot/phases.py` is stable.

**Recommended Fix:** Store `session_id` explicitly in the Agent model or a metadata column.

---

## New Findings (Run 2)

No new BLOCKERs, WARNINGs, or NITs were identified. The fix commit was clean and did not introduce regressions.

---

## Test Results

| Test File | Tests | Status |
|-----------|-------|--------|
| `test_budget_enforcement.py` | 21 | ✅ All pass |
| `test_cost_tracking.py` | 31 | ✅ All pass |
| `test_cost_collection_service.py` | 26 | ✅ All pass |
| **Total** | **78** | **✅ All pass** |

---

## Summary

| Prior Finding | Status | Notes |
|--------------|--------|-------|
| BLOCKER-1: Phase 0 gap in stop endpoint | ✅ FIXED | Uses shared `_pause_project_workflows` |
| BLOCKER-2: Stale DB session in budget guard | ✅ FIXED | Reuses existing session |
| WARNING-1: Missing "starting" agent status | ✅ FIXED | Added to filter + AgentStatus constants |
| WARNING-2: Misleading log messages | ✅ FIXED | Shows actual `paused_by` value |
| WARNING-3: Stale status_reason | ✅ FIXED | Cleared on user pause |
| WARNING-4: Unlinked costs bypass enforcement | ❌ OPEN | Low practical risk |
| NIT-1: Source inspection tests | ✅ FIXED | Replaced with behavioral tests |
| NIT-2: Fragile session ID parsing | ❌ OPEN | Low practical risk |

**Verdict:** Implementation approved. Both BLOCKERs resolved. The 2 remaining findings (WARNING-4, NIT-2) are low-risk design limitations, not merge blockers.

---

*Re-verification complete. 0 new BLOCKERs. Prior BLOCKERs: 2/2 fixed.*
