# Product Validation Report: Budget Enforcement and Pipeline Throttling

**Feature ID:** des-91c8-budget-enforcement  
**Feature Name:** Budget Enforcement and Pipeline Throttling  
**Validation Date:** 2026-07-21  
**Design Document:** `docs/COST_TRACKING_DESIGN.md` (Budget Enforcement section)  
**Requirements Document:** `docs/requirements_analysis.md`  
**QA Report:** `docs/qa_validation/qa_report.md`  
**Verdict:** CONDITIONAL PASS — Backend enforcement complete; 3 frontend gaps require follow-up

---

## 1. Executive Summary

The Budget Enforcement and Pipeline Throttling feature completes the enforcement layer for the Cost Tracking system. The core backend enforcement is **fully implemented and tested** (84/84 tests pass across all cost-related test files). The orchestrator correctly blocks new work for over-budget projects, self-heal guards protect budget-paused workflows from auto-resume, and the `/autopilot/stop` endpoint has been refactored to include Phase 0 workflows.

**Three frontend gaps remain:** (1) `ProjectSettingsModal.tsx` has no `cost_limit_usd` number input for budget configuration, (2) no "$current / $limit" indicator exists on the autopilot design screen, and (3) budget-paused workflows show a generic "Paused" label instead of "Paused: budget limit reached." These are UI-only gaps that don't affect the backend enforcement logic — budget limits can be set via the API, and enforcement works correctly once set.

---

## 2. Design Document Comparison

### 2.1 Core Enforcement Requirements

| Requirement | Design Spec | Implementation | Status |
|-------------|-------------|----------------|--------|
| FR-1: Budget guard in `pick_next_design()` | Skip over-budget projects before querying pending designs | `orchestrator.py:2021-2027`: calls `check_budget_before_new_work()`, returns `None` if over budget, logs with project name and cost details | ✅ MATCH |
| FR-2: Budget guard in `_run_one_feature()` | Block new workflow launches when over budget | `orchestrator.py:7061-7068`: calls `check_budget_before_new_work()`, returns `"budget_blocked"`, updates feature status to "paused" with reason | ✅ MATCH |
| FR-3: Generalize `paused_by` guards | Change `== "user"` to `is not None` in 3 locations; keep `== "user"` in `AutopilotService.start()` | `orchestrator.py:3763` (`_try_auto_resume_paused_workflow`): `is not None` ✓ | ✅ MATCH |
| | | `orchestrator.py:5694` (`_create_corrective_task`): `is not None` ✓ | ✅ MATCH |
| | | `orchestrator.py:5879` (`attempt_recovery` stuck restart): `is not None` ✓ | ✅ MATCH |
| | | `orchestrator.py:398` (`AutopilotService.start()`): keeps `== "user"` ✓ | ✅ MATCH |
| FR-4: Refactor `/autopilot/stop` | Use shared `_pause_project_workflows()` from `cost_derivation.py` | `autopilot_api.py:3686`: imports and calls `_pause_project_workflows(db, pid, paused_by="user")` | ✅ MATCH |
| FR-5: Budget-paused resume via limit increase | Clear `paused_by="budget"` when limit raised/cleared | `autopilot_api.py:1841-1866`: clears budget-paused workflows when `cost_limit_usd` raised or set to None | ✅ MATCH (pre-existing) |

### 2.2 UI Requirements

| Requirement | Design Spec | Implementation | Status |
|-------------|-------------|----------------|--------|
| FR-6: Budget config in ProjectSettingsModal | Number input for `cost_limit_usd`, display `cost_total_usd` | `ProjectSettingsModal.tsx` has no cost/budget fields | ❌ MISSING |
| FR-7: Cost display on design screen | "$current / $limit" indicator with link to settings | `DesignQueuePanel.tsx` and `PipelineStatusCard.tsx` have no cost display | ❌ MISSING |
| FR-8: Budget-paused status label | "Paused: budget limit reached" for budget-paused workflows | No frontend component checks `paused_by === "budget"` | ❌ MISSING |

### 2.3 Design Decision Verification

| Decision | Design Spec | Implementation | Status |
|----------|-------------|----------------|--------|
| D-1: Budget guards no-op when no limit | `check_budget_before_new_work()` returns `True` when `cost_limit_usd is None` | `cost_derivation.py:282-283`: `if project.cost_limit_usd is None: return True` | ✅ MATCH |
| D-2: `paused_by` generalization (except `start()`) | `is not None` everywhere except `AutopilotService.start()` | All 3 guard locations use `is not None`; `start()` keeps `== "user"` | ✅ MATCH |
| D-3: Spend over-limit expected | No exact-cutoff design; enforcement stops next call | Budget check happens after cost derivation rollup; next work blocked | ✅ MATCH |
| D-4: Shared `_pause_project_workflows` | Both `/autopilot/stop` and budget enforcement use same function | Both call `_pause_project_workflows()` from `cost_derivation.py` | ✅ MATCH |

---

## 3. Functional Requirements Verification

### FR-1: Budget Guard in `pick_next_design()` — ✅ PASS

**Evidence:** `src/autopilot/orchestrator.py:2021-2027`
```python
# Budget guard: skip project entirely if over budget
from src.core.cost_derivation import check_budget_before_new_work

if not check_budget_before_new_work(db, project.id):
    logger.info(
        f"pick_next_design: project '{project.name}' ({project.id[:8]}) "
        f"over budget (${project.cost_total_usd:.2f} >= ${project.cost_limit_usd:.2f}) — skipping"
    )
    return None
```

| Acceptance Criterion | Status | Evidence |
|---------------------|--------|----------|
| AC-1.1: Over budget returns None | ✅ | Returns `None` when `check_budget_before_new_work` returns False |
| AC-1.2: No limit proceeds normally | ✅ | `check_budget_before_new_work` returns True when `cost_limit_usd is None` |
| AC-1.3: Under budget proceeds normally | ✅ | `check_budget_before_new_work` returns True when under limit |
| AC-1.4: Log includes project name | ✅ | Log message includes project name, ID, and cost amounts |

---

### FR-2: Budget Guard in `_run_one_feature()` — ✅ PASS

**Evidence:** `src/autopilot/orchestrator.py:7061-7068`
```python
if project_id:
    from src.core.cost_derivation import check_budget_before_new_work
    if not check_budget_before_new_work(db, project_id):
        logger.info(
            f"[BUDGET] Project over budget — blocking new workflow for feature {feature_key}"
        )
        _update_feature_status(
            feature_id, design_entry.db_id, "paused", "Budget limit reached", logger
        )
        return "budget_blocked"
```

| Acceptance Criterion | Status | Evidence |
|---------------------|--------|----------|
| AC-2.1: Over budget blocks workflow launch | ✅ | Returns `"budget_blocked"` without calling `run_single_workflow` |
| AC-2.2: Return value distinguishes budget-blocked | ✅ | Returns `"budget_blocked"` string |
| AC-2.3: Existing running workflows unaffected | ✅ | Guard only checks before *new* launches |

**Note:** The implementation goes beyond the design spec by also updating the feature status to "paused" with reason "Budget limit reached" — this is a positive deviation that provides better visibility.

---

### FR-3: Generalize `paused_by` Guards — ✅ PASS

| Location | Before | After | Status |
|----------|--------|-------|--------|
| `_try_auto_resume_paused_workflow()` (line 3763) | `== "user"` | `is not None` | ✅ |
| `_create_corrective_task()` (line 5694) | `== "user"` | `is not None` | ✅ |
| `attempt_recovery()` stuck restart (line 5879) | `== "user"` | `is not None` | ✅ |
| `AutopilotService.start()` (line 398) | `== "user"` | `== "user"` (KEPT) | ✅ |

| Acceptance Criterion | Status | Evidence |
|---------------------|--------|----------|
| AC-3.1: `_try_auto_resume` skips budget-paused | ✅ | Test `test_try_auto_resume_skips_budget_paused` passes |
| AC-3.2: `_create_corrective_task` skips budget-paused | ✅ | Uses `is not None` check |
| AC-3.3: `attempt_recovery` skips budget-paused | ✅ | Uses `is not None` check |
| AC-3.4: `start()` only resumes user-paused | ✅ | Keeps `== "user"` filter |
| AC-3.5: User-paused still protected | ✅ | Test `test_try_auto_resume_skips_user_paused` passes |

---

### FR-4: Refactor `/autopilot/stop` — ✅ PASS

**Evidence:** `src/mcp/autopilot_api.py:3682-3690`
```python
# Uses shared _pause_project_workflows which includes Phase 0 workflows
# (definition_id in ["autopilot", "autopilot-phase0"]).
from src.core.cost_derivation import _pause_project_workflows
with get_db() as db:
    for pid in stopped_project_ids:
        paused = _pause_project_workflows(db, pid, paused_by="user")
        terminated_count += paused
    db.commit()
```

| Acceptance Criterion | Status | Evidence |
|---------------------|--------|----------|
| AC-4.1: Pauses both autopilot and phase0 | ✅ | `_pause_project_workflows` filters `definition_id.in_(["autopilot", "autopilot-phase0"])` |
| AC-4.2: Sets `paused_by="user"` | ✅ | Passes `paused_by="user"` explicitly |
| AC-4.3: Active agents terminated | ✅ | `_pause_project_workflows` terminates agents with status "working", "idle", or "starting" |
| AC-4.4: Endpoint response unchanged | ✅ | Same response format: `{stopped, agents_terminated, state_cleared}` |

---

### FR-5: Budget-Paused Resume via Limit Increase — ✅ PASS (Pre-existing)

**Evidence:** `src/mcp/autopilot_api.py:1841-1866`

| Acceptance Criterion | Status | Evidence |
|---------------------|--------|----------|
| AC-5.1: Raising limit clears budget pause | ✅ | Test `test_raising_limit_clears_budget_pause` passes |
| AC-5.2: Clearing limit clears budget pause | ✅ | Test `test_clearing_limit_clears_budget_pause` passes |
| AC-5.3: Lowering limit doesn't clear pause | ✅ | Condition: `cost_total_usd < cost_limit_usd` must be true |
| AC-5.4: User-paused not affected | ✅ | Filter only targets `paused_by == "budget"` |

---

### FR-6: UI — Budget Configuration — ❌ NOT IMPLEMENTED

**Evidence:** `frontend/src/components/ProjectSettingsModal.tsx` contains no references to `cost_limit_usd`, `cost_total_usd`, or budget-related fields. The modal only handles project creation, deletion, and basic settings.

| Acceptance Criterion | Status |
|---------------------|--------|
| AC-6.1: Number input field visible | ❌ |
| AC-6.2: Setting persists via PUT /projects/{id} | ❌ |
| AC-6.3: Clearing sets to None | ❌ |
| AC-6.4: Current spend displayed | ❌ |
| AC-6.5: Accepts decimal values | ❌ |

---

### FR-7: UI — Cost Display on Design Screen — ❌ NOT IMPLEMENTED

**Evidence:** `frontend/src/components/autopilot/DesignQueuePanel.tsx` and `PipelineStatusCard.tsx` contain no cost or budget references.

**Partial credit:** `FeatureGallery.tsx:184-228` and `FeatureDetailModal.tsx:220` display `cost_total` on feature cards, but this is feature-level cost display, not the project-level "$current / $limit" indicator specified in the design.

| Acceptance Criterion | Status |
|---------------------|--------|
| AC-7.1: Cost indicator visible on design screen | ❌ |
| AC-7.2: Shows "$X.XX / $Y.YY" when limit set | ❌ |
| AC-7.3: Shows "$X.XX spent" when no limit | ❌ |
| AC-7.4: Link opens ProjectSettingsModal | ❌ |
| AC-7.5: Indicator updates on refresh | ❌ |

---

### FR-8: UI — Budget-Paused Status Label — ❌ NOT IMPLEMENTED

**Evidence:** No frontend component checks `paused_by === "budget"` to display a distinct label. The string "Paused: budget limit reached" or "budget" does not appear in any frontend source file.

| Acceptance Criterion | Status |
|---------------------|--------|
| AC-8.1: Budget-paused shows distinct label | ❌ |
| AC-8.2: User-paused shows existing label | ✅ (no regression) |
| AC-8.3: Label is human-readable | ❌ |

---

## 4. Non-Functional Requirements Verification

| ID | Requirement | Status | Evidence |
|----|-------------|--------|----------|
| NFR-1 | Backward Compatibility | ✅ PASS | Budget enforcement is opt-in (`cost_limit_usd` defaults to `None`). `check_budget_before_new_work()` returns `True` when no limit set. All existing behavior preserved. |
| NFR-2 | Performance | ✅ PASS | Budget guards add one lightweight DB column comparison per design/feature pick. No aggregation queries. No additional I/O. |
| NFR-3 | Reliability | ✅ PASS | Budget pause is idempotent (test_idempotent passes). Spend lands at-or-slightly-over limit (cost knowable after the fact). Self-healing derivation ensures consistency. |
| NFR-4 | Observability | ✅ PASS | All budget decisions logged at INFO level with project name, ID, and cost amounts. Budget-paused workflows have `status_reason = "Budget limit reached"`. |

---

## 5. Integration Point Verification

| Component | Design Requirement | Implementation | Status |
|-----------|-------------------|----------------|--------|
| `src/autopilot/orchestrator.py` | Budget guards in `pick_next_design` and `_run_one_feature` | Lines 2021-2027 and 7061-7068 | ✅ |
| `src/autopilot/orchestrator.py` | Generalize `paused_by` guards (3 locations) | Lines 3763, 5694, 5879 | ✅ |
| `src/autopilot/orchestrator.py` | Keep `== "user"` in `AutopilotService.start()` | Line 398 | ✅ |
| `src/mcp/autopilot_api.py` | Refactor `/autopilot/stop` to use shared function | Line 3686 | ✅ |
| `src/mcp/autopilot_api.py` | Budget pause clearing on limit increase | Lines 1841-1866 | ✅ |
| `src/core/cost_derivation.py` | `check_budget_before_new_work()` | Line 270 | ✅ |
| `src/core/cost_derivation.py` | `_pause_project_workflows()` with Phase 0 fix | Line 294 | ✅ |
| `frontend/.../ProjectSettingsModal.tsx` | Budget config input | NOT PRESENT | ❌ |
| `frontend/.../DesignQueuePanel.tsx` | Cost display indicator | NOT PRESENT | ❌ |
| `frontend/.../PipelineStatusCard.tsx` | Budget-pause label | NOT PRESENT | ❌ |

---

## 6. Test Results Summary

| Test File | Tests | Passed | Failed | Pass Rate |
|-----------|-------|--------|--------|-----------|
| `test_budget_enforcement.py` | 21 | 21 | 0 | 100% |
| `test_cost_tracking.py` | 43 | 43 | 0 | 100% |
| `test_cost_collection_service.py` | 20 | 20 | 0 | 100% |
| **Total** | **84** | **84** | **0** | **100%** |

### Budget Enforcement Test Breakdown

| Test Class | Tests | Description |
|------------|-------|-------------|
| `TestPauseProjectWorkflows` | 8 | Phase 0 inclusion, agent termination, idempotency, user pause, starting agents |
| `TestCheckBudget` | 4 | Under/over budget, no limit, nonexistent project |
| `TestPausedByGeneralization` | 3 | Budget-paused skipped, user-paused skipped, None-paused resumes |
| `TestPickNextDesignBudgetGuard` | 4 | Over/under budget, no limit, exact limit |
| `TestUpdateProjectClearsBudgetPause` | 2 | Raising limit, clearing limit |

---

## 7. Positive Deviations from Design

| Deviation | Benefit |
|-----------|---------|
| `_run_one_feature()` updates feature status to "paused" with reason | Better visibility — feature cards show why work stopped |
| `_pause_project_workflows()` terminates "starting" agents | Design only mentioned "active" agents; "starting" agents also terminated |
| User pause clears stale budget `status_reason` | Prevents confusing "Budget limit reached" message after user takes over |
| Budget guard includes logging with exact cost amounts | Easier debugging than design's simpler log message |

---

## 8. Gap Analysis

### Critical Gaps (Block production use)

**None.** The backend enforcement is complete and functional. Budget limits can be set via the API (`PUT /projects/{id}` with `cost_limit_usd`), and enforcement works correctly once set.

### Important Gaps (Should be addressed before production)

| ID | Gap | Impact | Recommended Fix |
|----|-----|--------|-----------------|
| G-1 | `ProjectSettingsModal.tsx` missing `cost_limit_usd` input | Users cannot configure budget limits via UI — must use API directly | Add number input to ProjectSettingsModal, wire to `PUT /projects/{id}` |
| G-2 | No "$current / $limit" indicator on design screen | Users must check API/settings to see budget status | Add cost indicator to `DesignQueuePanel.tsx` or `PipelineStatusCard.tsx` |
| G-3 | No budget-pause status label | Users can't distinguish budget-paused from user-paused workflows | Check `paused_by === "budget"` in status display components |

### Minor Gaps (Can be deferred)

| ID | Gap | Impact | Recommended Fix |
|----|-----|--------|-----------------|
| G-4 | No link from design screen to ProjectSettingsModal | Users must navigate to settings separately | Add settings link near cost indicator |

---

## 9. Edge Cases Verified

| Edge Case | Design Spec | Implementation | Status |
|-----------|-------------|----------------|--------|
| Concurrent cost entries cause double-pause | `_pause_project_workflows` is idempotent | Only matches `status.in_(["active", "running"])` — second call finds nothing | ✅ |
| Spend lands at-or-slightly-over limit | Enforcement stops next call, not the one that crossed | Budget check happens in cost derivation rollup, after CostEntry written | ✅ |
| Limit raised while budget-paused | Clear `paused_by="budget"`, resume workflows | `autopilot_api.py:1851-1866` clears budget-paused when limit raised | ✅ |
| Phase 0 workflow running when budget exceeded | Must be paused (not just "autopilot" workflows) | `_pause_project_workflows` filters `definition_id.in_(["autopilot", "autopilot-phase0"])` | ✅ |
| Agent retry gets same session_id | Checkpoint keyed by session_id, not Agent.id | `SessionCostCheckpoint` primary key is `session_id` | ✅ |
| Play button on budget-paused project | Must NOT resume (limit still exceeded) | `AutopilotService.start()` keeps `== "user"` filter | ✅ |
| No limit set (`cost_limit_usd = None`) | Budget guard is no-op | `check_budget_before_new_work` returns `True` when no limit | ✅ |
| Cost equals exact limit | Should block (>= comparison) | `cost_total_usd >= cost_limit_usd` in `check_budget_before_new_work` | ✅ |

---

## 10. Security Verification

| Check | Status | Evidence |
|-------|--------|----------|
| Cost entry endpoint requires authentication | ✅ | `X-Agent-ID` header with `verify_agent_authentication()` |
| Negative cost values rejected | ✅ | Pydantic validator in `CostEntryCreate` |
| Excessive cost values rejected (>$1000) | ✅ | Pydantic validator in `CostEntryCreate` |
| Invalid source values rejected | ✅ | Pydantic validator limits to known sources |
| Path traversal in session file discovery | ✅ | `..` and `~` rejected in `_discover_session_file()` |

---

## 11. Recommendations for Human Review

1. **Frontend gaps (G-1, G-2, G-3):** These are the only gaps preventing full production readiness. The backend enforcement is complete. Recommend scheduling a frontend-only task to add:
   - `cost_limit_usd` number input to `ProjectSettingsModal.tsx`
   - "$current / $limit" cost indicator to `DesignQueuePanel.tsx` or `PipelineStatusCard.tsx`
   - "Paused: budget limit reached" badge when `workflow.paused_by === "budget"`

2. **Workaround for missing UI:** Budget limits can be set via the API directly: `PUT /api/autopilot/projects/{project_id}` with `{"cost_limit_usd": 100.00}`. The enforcement logic works correctly once set.

3. **Future enhancements:**
   - Per-design or per-phase budget limits (currently per-project only)
   - Cost alerting/notifications before limit reached
   - Budget approval workflow for overruns (see `design_docs/budget_tracking_approval_system.md`)

---

## 12. Verdict

**CONDITIONAL PASS**

The Budget Enforcement and Pipeline Throttling implementation meets all backend functional requirements (FR-1 through FR-5) and all non-functional requirements (NFR-1 through NFR-4). All 84 tests pass. The enforcement logic correctly blocks new work for over-budget projects, protects budget-paused workflows from auto-resume, and includes Phase 0 workflows in budget pauses.

**Conditions for full PASS:**
1. Add `cost_limit_usd` input to `ProjectSettingsModal.tsx` (G-1)
2. Add "$current / $limit" cost indicator to design screen (G-2)
3. Add "Paused: budget limit reached" label for budget-paused workflows (G-3)

**No blockers from:** backend enforcement, data layer, API layer, test coverage, or security.

---

## 13. Deliverables

- `docs/product_validation/product_validation.md` — This report
- `docs/product_validation/product_validation.json` — Structured verdict for pipeline gate

---

*Report generated: 2026-07-21*
