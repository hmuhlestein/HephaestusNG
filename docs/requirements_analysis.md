# Product Requirements Analysis: Budget Enforcement and Pipeline Throttling

**Feature ID:** des-91c8-budget-enforcement  
**Feature Name:** Budget Enforcement and Pipeline Throttling  
**Status:** Requirements Extracted  
**Date:** 2026-07-21  
**Design Document:** `docs/COST_TRACKING_DESIGN.md` (Budget Enforcement section) + `.hephaestus/design.md`  
**Related Design Docs:** `design_docs/budget_tracking_approval_system.md`, `design_docs/per_task_cost_tracking.md`  
**Parent Feature:** Cost Tracking Database Schema (DES-91c8) — already merged

---

## 1. Executive Summary

The Cost Tracking Database Schema feature (already merged) established the foundational data model: `cost_entries` ledger table, `SessionCostCheckpoint` for session tracking, `cost_total_usd` rollup columns on all entity models, `cost_limit_usd` on `AutopilotProject`, self-healing cost derivation via `cost_derivation.py`, and a Pi JSONL collector wired into task completion.

**This feature completes the enforcement layer.** The cost data infrastructure exists but the pipeline doesn't yet *react* to it. The orchestrator has no budget guards in its design/feature queue loops, the `paused_by` guards only recognize `"user"` (not `"budget"`), and the UI has no cost visibility or budget configuration. Without this feature, a user can set `cost_limit_usd` on a project and cost data will be tracked and rolled up, but the pipeline will happily continue spending indefinitely — the budget limit is stored but not enforced.

**Current State (from Cost Tracking Schema merge):**
- ✅ `CostEntry` table with all columns and indexes
- ✅ `SessionCostCheckpoint` table keyed by `session_id`
- ✅ `cost_total_usd` on `Task`, `Feature`, `Workflow`, `AutopilotDesign`, `AutopilotProject`
- ✅ `cost_limit_usd` on `AutopilotProject` (nullable, None = no limit)
- ✅ `cost_derivation.py` with self-healing rollup chain + `_check_budget_enforcement` + `_pause_project_workflows` (Phase 0 fix: matches both `"autopilot"` and `"autopilot-phase0"`)
- ✅ `check_budget_before_new_work()` guard function (exists but NOT called anywhere in orchestrator)
- ✅ Pi JSONL collector in `cost_collection_service.py`
- ✅ `collect_task_cost()` wired into `task_completion_service.py`
- ✅ `POST /cost-entries` API endpoint with agent authentication
- ✅ `cost_limit_usd` in `ProjectUpdate`/`ProjectItem` API models
- ✅ Budget pause clearing logic in `update_project` API (clears `"budget"`-paused workflows when limit raised/cleared)
- ✅ Tests for cost derivation, budget enforcement, pause idempotency

**Target State (this feature):**
- Orchestrator blocks new work for over-budget projects (budget guards in `pick_next_design` and `_run_one_feature`)
- All self-heal/auto-resume guards recognize `"budget"`-paused workflows (generalize `== "user"` to `is not None`)
- `/autopilot/stop` endpoint refactored to use shared `_pause_project_workflows` (fixing its Phase 0 gap)
- UI shows cost data and budget configuration
- Comprehensive integration tests for the full enforcement lifecycle

---

## 2. Problem Statement

### 2.1 Enforcement Gap

The cost derivation module (`cost_derivation.py`) checks budgets on every `CostEntry` write and calls `_pause_project_workflows` when the limit is exceeded. However, three critical enforcement points are missing:

1. **New work starts despite over-budget status.** `pick_next_design()` in the orchestrator picks the next pending design from the queue without checking whether the project is over budget. If a project has 10 designs and exceeds its budget after design 3, designs 4-10 will still be picked up and processed.

2. **New feature workflows launch despite over-budget status.** `_run_one_feature()` launches new feature workflows via `run_single_workflow` without checking budget. Even if a running workflow was paused for budget, a new feature in the same design could start.

3. **Self-heal guards silently resume budget-paused work.** Three places in the orchestrator check `wf.paused_by == "user"` to avoid auto-resuming deliberately paused workflows: `_try_auto_resume_paused_workflow` (line 3749), `_create_corrective_task` (line 5680), and stuck-workflow restart in `attempt_recovery` (line 5864). A `"budget"`-paused workflow passes right through these guards as if it were normal, causing the self-heal system to reactivate work that was deliberately paused for budget reasons.

### 2.2 Phase 0 Gap in `/autopilot/stop`

The `/autopilot/stop` endpoint filters `Workflow.definition_id == "autopilot"` but Phase 0 (Feature Architect) launches under `definition_id == "autopilot-phase0"`. A stop/budget-pause that only matches `"autopilot"` leaves Phase 0 running. The `_pause_project_workflows` function in `cost_derivation.py` already fixes this (matches both IDs), but the `/autopilot/stop` endpoint still uses inline logic with the old filter.

### 2.3 UI Visibility Gap

No frontend component displays cost data, budget limits, or budget-paused status. Users have no visibility into project spend or pipeline pause reasons.

---

## 3. Functional Requirements

### FR-1: Budget Guard in `pick_next_design()`

**Requirement:** Before picking a pending design from the queue, check whether the project is over budget. If over budget, skip the project and log a message.

**Location:** `src/autopilot/orchestrator.py`, function `pick_next_design()` (~line 1975)

**Implementation:**
```python
# After resolving project, before querying for pending designs:
from src.core.cost_derivation import check_budget_before_new_work
if not check_budget_before_new_work(db, project.id):
    logger.info(f"pick_next_design: project '{project.name}' over budget — skipping")
    return None
```

**Behavior:**
- Returns `None` (no design picked) when project is over budget
- Logs an info-level message so operators can see why the queue stalled
- Does NOT modify any state (no side effects)
- The existing `check_budget_before_new_work()` function handles the `cost_limit_usd is None` (no limit) and `cost_total_usd < cost_limit_usd` (under budget) cases, returning `True` to proceed

**Acceptance Criteria:**
- AC-1.1: When `cost_total_usd >= cost_limit_usd`, `pick_next_design` returns `None`
- AC-1.2: When `cost_limit_usd is None`, `pick_next_design` proceeds normally
- AC-1.3: When under budget, `pick_next_design` proceeds normally
- AC-1.4: Log message includes project name and budget status

---

### FR-2: Budget Guard in `_run_one_feature()`

**Requirement:** Before launching a new feature workflow via `run_single_workflow`, check whether the project is over budget. If over budget, return early without launching.

**Location:** `src/autopilot/orchestrator.py`, function `_run_one_feature()` (~line 2673 area)

**Implementation:**
```python
# Before calling run_single_workflow for a new feature:
from src.core.cost_derivation import check_budget_before_new_work
if not check_budget_before_new_work(db, project_id):
    logger.info(f"_run_one_feature: project over budget — blocking new workflow for feature {feature_id[:8]}")
    return "budget_blocked"
```

**Behavior:**
- Returns a distinct status (`"budget_blocked"`) so callers can distinguish from other early returns
- Does NOT launch the workflow or create any agents
- Logs feature ID for traceability

**Acceptance Criteria:**
- AC-2.1: When over budget, no workflow is launched for the feature
- AC-2.2: Return value distinguishes budget-blocked from other outcomes
- AC-2.3: Existing feature (already running workflow) is unaffected — this guard only blocks *new* launches

---

### FR-3: Generalize `paused_by` Guards to `is not None`

**Requirement:** Change all self-heal/auto-resume guards from `wf.paused_by == "user"` to `wf.paused_by is not None` so that `"budget"`-paused workflows are also protected from auto-resume.

**Affected locations in `src/autopilot/orchestrator.py`:**

1. **`_try_auto_resume_paused_workflow()` (~line 3749):**
   ```python
   # BEFORE: if wf.paused_by == "user":
   # AFTER:  if wf.paused_by is not None:
   ```

2. **`_create_corrective_task()` (~line 5680):**
   ```python
   # BEFORE: if wf.paused_by == "user":
   # AFTER:  if wf.paused_by is not None:
   ```

3. **Stuck-workflow restart in `attempt_recovery()` (~line 5864):**
   ```python
   # BEFORE: if wf.status == "paused" and wf.paused_by == "user":
   # AFTER:  if wf.status == "paused" and wf.paused_by is not None:
   ```

**EXCEPTION — `AutopilotService.start()` (~line 391-399):**
This location MUST keep `== "user"`. Clicking "play" in the UI should resume user-paused projects but should NOT clear budget-paused projects (the limit is still exceeded; clicking play shouldn't bypass the cap). The existing code at line 398 (`Workflow.paused_by == "user"`) remains unchanged.

**Rationale:** This is a strict generalization — every workflow previously protected by `== "user"` is still protected, and `"budget"`-paused workflows are now also protected. No behavior change for the existing `"user"` case.

**Acceptance Criteria:**
- AC-3.1: `_try_auto_resume_paused_workflow` skips `"budget"`-paused workflows
- AC-3.2: `_create_corrective_task` skips `"budget"`-paused workflows
- AC-3.3: `attempt_recovery`'s stuck-workflow restart skips `"budget"`-paused workflows
- AC-3.4: `AutopilotService.start()` still only resumes `"user"`-paused workflows (NOT `"budget"`)
- AC-3.5: `"user"`-paused workflows continue to be protected (no regression)

---

### FR-4: Refactor `/autopilot/stop` to Use Shared `_pause_project_workflows`

**Requirement:** Extract the inline pause logic from the `/autopilot/stop` route handler into a call to the shared `_pause_project_workflows()` function from `cost_derivation.py`, which already filters `definition_id.in_(["autopilot", "autopilot-phase0"])`.

**Location:** `src/mcp/autopilot_api.py`, the `/autopilot/stop` endpoint

**Implementation:**
```python
# Replace inline pause logic with:
from src.core.cost_derivation import _pause_project_workflows
paused_count = _pause_project_workflows(db, project_id, paused_by="user")
db.commit()
```

**Side effect fix:** The existing endpoint's `definition_id == "autopilot"` filter misses Phase 0. After refactoring, Phase 0 workflows are also paused when the user clicks stop — this is the correct behavior.

**Acceptance Criteria:**
- AC-4.1: `/autopilot/stop` pauses both `"autopilot"` and `"autopilot-phase0"` workflows
- AC-4.2: `/autopilot/stop` sets `paused_by="user"` (not `"budget"`)
- AC-4.3: Active agents on paused workflows are terminated
- AC-4.4: Endpoint behavior is unchanged from user perspective (same HTTP response)

---

### FR-5: Budget-Paused Workflow Resume via Limit Increase

**Requirement:** When a user raises or clears `cost_limit_usd` via `PUT /projects/{id}`, automatically clear `paused_by` on that project's `"budget"`-paused workflows so the next pipeline sweep can resume them.

**Status:** ✅ ALREADY IMPLEMENTED in `autopilot_api.py` (lines 1841-1866). The logic:
```python
if proj.cost_limit_usd is None or proj.cost_total_usd < proj.cost_limit_usd:
    budget_paused = db.query(Workflow).filter(
        Workflow.project_id == project_id,
        Workflow.paused_by == "budget",
    ).all()
    for wf in budget_paused:
        wf.paused_by = None
        wf.status = "active"
```

**No implementation work needed.** Verified existing behavior.

**Acceptance Criteria (verification only):**
- AC-5.1: Raising `cost_limit_usd` above `cost_total_usd` clears `"budget"` pause
- AC-5.2: Setting `cost_limit_usd` to `None` (clearing limit) clears `"budget"` pause
- AC-5.3: Lowering `cost_limit_usd` (still over budget) does NOT clear pause
- AC-5.4: `"user"`-paused workflows are NOT affected by limit changes

---

### FR-6: UI — Budget Configuration in ProjectSettingsModal

**Requirement:** Add a `cost_limit_usd` number input to `ProjectSettingsModal.tsx` so users can set/clear the per-project budget limit.

**Location:** `frontend/src/components/ProjectSettingsModal.tsx`

**Implementation:**
- Add optional number input field labeled "Budget Limit (USD)"
- Placeholder: "No limit" when empty
- Wire to existing `PUT /projects/{id}` mutation (already supports `cost_limit_usd`)
- Display current `cost_total_usd` alongside the input as read-only context
- Validation: must be a positive number if provided, or empty/null for no limit

**Acceptance Criteria:**
- AC-6.1: Number input field visible in ProjectSettingsModal
- AC-6.2: Setting a value persists to backend via `PUT /projects/{id}`
- AC-6.3: Clearing the value sets `cost_limit_usd` to `None` (no limit)
- AC-6.4: Current spend (`cost_total_usd`) displayed near the input
- AC-6.5: Input accepts decimal values (e.g., `10.50`)

---

### FR-7: UI — Cost Display on Autopilot Design Screen

**Requirement:** Show current project spend on the autopilot design screen, with a link to open ProjectSettingsModal for budget configuration.

**Location:** `frontend/src/components/autopilot/DesignQueuePanel.tsx` or `PipelineStatusCard.tsx`

**Implementation:**
- Display "$current / $limit" when limit is set, or "$current spent" when no limit
- Small indicator, not dominant in the UI
- Link/button that opens `ProjectSettingsModal` scoped to the active project
- Data source: project object already includes `cost_total_usd` and `cost_limit_usd` from API

**Acceptance Criteria:**
- AC-7.1: Cost indicator visible on design screen
- AC-7.2: Shows "$X.XX / $Y.YY" format when limit is set
- AC-7.3: Shows "$X.XX spent" when no limit is set
- AC-7.4: Link opens ProjectSettingsModal
- AC-7.5: Indicator updates when project data refreshes

---

### FR-8: UI — Budget-Paused Status Label

**Requirement:** When a workflow is paused with `paused_by == "budget"`, display "Paused: budget limit reached" instead of a generic "Paused" label.

**Location:** Various frontend components that display workflow/feature status

**Implementation:**
- Check `paused_by` field in workflow status display components
- When `paused_by === "budget"`, show "Paused: budget limit reached"
- When `paused_by === "user"`, show "Paused" (existing behavior)
- When `paused_by === "system"` or `"system-exhausted"`, show existing labels

**Acceptance Criteria:**
- AC-8.1: Budget-paused workflows show distinct label
- AC-8.2: User-paused workflows show existing label (no regression)
- AC-8.3: Label is human-readable and explains the pause reason

---

## 4. Non-Functional Requirements

### NFR-1: Backward Compatibility
- All changes are additive; no existing behavior is broken
- Budget enforcement is opt-in (`cost_limit_usd` defaults to `None`)
- The `paused_by` generalization is a strict superset of existing behavior

### NFR-2: Performance
- Budget guards add one lightweight DB query per design/feature pick (already in the same DB session)
- `check_budget_before_new_work()` is a simple column comparison, no aggregation
- No additional I/O or network calls introduced

### NFR-3: Reliability
- Budget enforcement is idempotent (concurrent calls find nothing to pause after the first)
- Spend always lands at-or-slightly-over limit (cost only knowable after the fact — enforcement stops the *next* call)
- Self-healing cost derivation ensures consistency even if budget check misses a beat

### NFR-4: Observability
- All budget decisions logged at INFO level or higher
- Log messages include project ID/name, cost totals, and limit values
- Budget-paused workflows have `status_reason = "Budget limit reached"` in DB

---

## 5. Technology Constraints

| Constraint | Detail |
|-----------|--------|
| Language | Python 3.12 (backend), TypeScript/React 18 (frontend) |
| ORM | SQLAlchemy with StaticPool, expire_on_commit=False |
| Database | SQLite with WAL mode |
| Frontend | React 18, TypeScript, Tailwind CSS |
| No new dependencies | Pure extensions of existing patterns |
| API | FastAPI with existing endpoint patterns |

---

## 6. Component Dependencies and Integration Points

### 6.1 Files to Modify

| File | Change | Status |
|------|--------|--------|
| `src/autopilot/orchestrator.py` | Add budget guard in `pick_next_design()`. Add budget guard in `_run_one_feature()`. Generalize `paused_by` guards from `== "user"` to `is not None` (3 locations). | ❌ NOT IMPLEMENTED |
| `src/mcp/autopilot_api.py` | Refactor `/autopilot/stop` to use shared `_pause_project_workflows()`. | ❌ NOT IMPLEMENTED |
| `frontend/src/components/ProjectSettingsModal.tsx` | Add `cost_limit_usd` number input. | ❌ NOT IMPLEMENTED |
| `frontend/src/components/autopilot/DesignQueuePanel.tsx` or `PipelineStatusCard.tsx` | Add cost display indicator. | ❌ NOT IMPLEMENTED |
| Various frontend status components | Show "Paused: budget limit reached" for budget-paused workflows. | ❌ NOT IMPLEMENTED |

### 6.2 Files Already Complete (No Changes Needed)

| File | What's Done |
|------|-------------|
| `src/core/database.py` | CostEntry, SessionCostCheckpoint tables. cost_total_usd on all models. cost_limit_usd on AutopilotProject. Migration functions. |
| `src/core/cost_derivation.py` | Full rollup chain. `_check_budget_enforcement()`. `_pause_project_workflows()` (Phase 0 fix). `check_budget_before_new_work()`. |
| `src/services/cost_collection_service.py` | Pi JSONL collector. `collect_task_cost()` entry point. |
| `src/services/task_completion_service.py` | `collect_task_cost()` wired into task completion handler. |
| `src/mcp/autopilot_api.py` | `POST /cost-entries` endpoint. `cost_limit_usd` in ProjectUpdate/ProjectItem. Budget pause clearing logic in `update_project`. |

### 6.3 Key Architectural Relationships

```
                    ┌─────────────────────────────┐
                    │   AutopilotProject           │
                    │   .cost_total_usd (derived)  │
                    │   .cost_limit_usd (config)   │
                    └──────────┬──────────────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
              ▼                ▼                ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
    │ cost_         │  │ pick_next_   │  │ _run_one_feature │
    │ derivation.py │  │ design()     │  │ ()               │
    │               │  │              │  │                  │
    │ on CostEntry  │  │ BUDGET       │  │ BUDGET           │
    │ write:        │  │ GUARD (NEW)  │  │ GUARD (NEW)      │
    │ _check_budget │  │              │  │                  │
    │ _enforcement()│  │ check_budget │  │ check_budget     │
    │               │  │ _before_new  │  │ _before_new_work │
    │ → _pause_     │  │ _work()      │  │ ()               │
    │  project_     │  └──────────────┘  └──────────────────┘
    │  workflows()  │
    └───────────────┘
              │
              ▼
    ┌─────────────────────────────────────────────┐
    │  Orchestrator Self-Heal Guards (GENERALIZE)  │
    │                                              │
    │  _try_auto_resume_paused_workflow():         │
    │    paused_by == "user" → is not None (NEW)   │
    │                                              │
    │  _create_corrective_task():                  │
    │    paused_by == "user" → is not None (NEW)   │
    │                                              │
    │  attempt_recovery() stuck restart:           │
    │    paused_by == "user" → is not None (NEW)   │
    │                                              │
    │  AutopilotService.start() play button:       │
    │    KEEP == "user" (EXCEPTION)                │
    └─────────────────────────────────────────────┘
```

---

## 7. Implementation Tasks

### T1: Orchestrator Budget Guards

**Files:** `src/autopilot/orchestrator.py`  
**Effort:** Small  
**Dependencies:** None (uses existing `check_budget_before_new_work()`)

**Changes:**
1. Add import of `check_budget_before_new_work` from `cost_derivation`
2. In `pick_next_design()`, after project resolution, before querying pending designs:
   ```python
   from src.core.cost_derivation import check_budget_before_new_work
   if not check_budget_before_new_work(db, project.id):
       logger.info(f"pick_next_design: project '{project.name}' over budget — skipping")
       return None
   ```
3. In `_run_one_feature()`, before calling `run_single_workflow`:
   ```python
   if not check_budget_before_new_work(db, project_id):
       logger.info(f"_run_one_feature: project over budget — blocking feature {feature_id[:8]}")
       return "budget_blocked"
   ```

**Acceptance:** Over-budget project's queue stalls. No new features launch. Log messages appear.

---

### T2: `paused_by` Generalization

**Files:** `src/autopilot/orchestrator.py`  
**Effort:** Small  
**Dependencies:** None

**Changes:**
1. Line ~3749 (`_try_auto_resume_paused_workflow`): `== "user"` → `is not None`
2. Line ~5680 (`_create_corrective_task`): `== "user"` → `is not None`
3. Line ~5864 (`attempt_recovery` stuck restart): `== "user"` → `is not None`
4. Line ~391-399 (`AutopilotService.start()`): **KEEP** `== "user"` — no change

**Acceptance:** Budget-paused workflows not auto-resumed by self-heal. User-paused workflows still protected. Play button still resumes user-paused only.

---

### T3: Refactor `/autopilot/stop` Endpoint

**Files:** `src/mcp/autopilot_api.py`  
**Effort:** Small  
**Dependencies:** None (shared function already exists in `cost_derivation.py`)

**Changes:**
1. Replace inline pause logic in `/autopilot/stop` with call to `_pause_project_workflows(db, project_id, "user")`
2. Keep existing response format

**Acceptance:** Phase 0 workflows now paused when user clicks stop. Endpoint response unchanged.

---

### T4: UI — Budget Configuration

**Files:** `frontend/src/components/ProjectSettingsModal.tsx`  
**Effort:** Small  
**Dependencies:** None (API already supports `cost_limit_usd`)

**Changes:**
1. Add `cost_limit_usd` number input field
2. Wire to existing `PUT /projects/{id}` mutation
3. Display `cost_total_usd` as read-only context

**Acceptance:** User can set/clear budget limit via settings modal.

---

### T5: UI — Cost Display

**Files:** `frontend/src/components/autopilot/DesignQueuePanel.tsx` or `PipelineStatusCard.tsx`  
**Effort:** Small  
**Dependencies:** T4 (settings modal must exist for link target)

**Changes:**
1. Add cost indicator showing "$current / $limit" or "$current spent"
2. Link to ProjectSettingsModal
3. Add "Paused: budget limit reached" label for budget-paused workflows

**Acceptance:** Cost visible on design screen. Budget-paused workflows clearly labeled.

---

### T6: Integration Tests

**Files:** `tests/test_budget_enforcement.py` (new)  
**Effort:** Medium  
**Dependencies:** T1, T2, T3

**Test cases:**
1. End-to-end: set limit → insert cost exceeding limit → verify workflows paused → verify pick_next_design returns None → verify _run_one_feature blocked
2. Phase 0 inclusion: budget pause includes `autopilot-phase0` workflows
3. Self-heal guards: budget-paused workflow not auto-resumed by `_try_auto_resume_paused_workflow`, `_create_corrective_task`, or `attempt_recovery`
4. Play button: budget-paused workflow NOT resumed by `start()`
5. Limit increase: raising limit clears budget pause, pipeline resumes
6. Idempotency: concurrent cost entries don't cause double-pause
7. `/autopilot/stop` refactoring: Phase 0 workflows paused by user stop

---

## 8. Critical Design Decisions

### D-1: Budget Guards as No-Op When No Limit Set
**Decision:** `check_budget_before_new_work()` returns `True` when `cost_limit_usd is None`.  
**Rationale:** Budget enforcement is opt-in. Projects without a limit should never be blocked.

### D-2: `paused_by` Generalization (Except `start()`)
**Decision:** Change `== "user"` to `is not None` everywhere except `AutopilotService.start()`.  
**Rationale:** Any non-null `paused_by` means deliberate pause. `start()` keeps `== "user"` because play button should resume user-paused but not budget-paused (limit still exceeded).

### D-3: Spend Over-Limit Is Expected
**Decision:** Don't design for exact-cutoff behavior.  
**Rationale:** Cost is only knowable after the LLM call completes. Enforcement stops the *next* call, not the one that crossed the limit.

### D-4: Shared `_pause_project_workflows` for All Pause Paths
**Decision:** Both `/autopilot/stop` (user-initiated) and budget enforcement use the same function.  
**Rationale:** Prevents the two code paths from drifting apart. Fixes the Phase 0 gap in the endpoint for free.

---

## 9. Acceptance Criteria Summary

| ID | Criterion | Verification |
|----|-----------|--------------|
| AC-1 | pick_next_design blocks over-budget projects | Unit test + integration test |
| AC-2 | _run_one_feature blocks over-budget launches | Unit test + integration test |
| AC-3 | Self-heal guards protect budget-paused workflows | Unit test for each guard location |
| AC-4 | Play button does NOT clear budget pause | Integration test |
| AC-5 | /autopilot/stop pauses Phase 0 | Integration test |
| AC-6 | Budget config UI works | Manual/frontend test |
| AC-7 | Cost display visible on design screen | Manual/frontend test |
| AC-8 | Budget-paused label distinct | Manual/frontend test |
| AC-9 | Existing tests still pass | CI |
| AC-10 | No new dependencies | Dependency audit |

---

## 10. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Budget guard blocks legitimate resume after limit raised | Low | Medium | Limit raise clears `paused_by="budget"` (already implemented) |
| Generalizing `paused_by` breaks existing user-pause behavior | Low | High | Strict generalization — existing `"user"` case unchanged; comprehensive tests |
| `/autopilot/stop` refactor changes endpoint behavior | Low | Medium | Same HTTP response format; Phase 0 inclusion is a bug fix, not a behavior change |
| UI shows stale cost data | Medium | Low | Cost data refreshes on project data poll; not real-time critical |

---

## 11. Open Questions

| # | Question | Status | Recommendation |
|---|----------|--------|----------------|
| Q1 | Should `pick_next_design` log at WARNING level when blocking for budget? | Open | INFO level — budget block is expected behavior, not an anomaly |
| Q2 | Should the UI show per-phase cost breakdown? | Deferred | Out of scope for this feature; design screen shows project-level total only |
| Q3 | Should there be a "force resume" button for budget-paused workflows? | Deferred | Raising the limit via settings is the intended "force resume" path |
| Q4 | Should `status_reason` be surfaced in the API response for paused workflows? | Open | Yes — helps frontend show "Paused: budget limit reached" without guessing from `paused_by` |

---

## 12. Non-Goals (Explicitly Deferred)

- **Per-design or per-phase budget limits.** Per-project only for now (matches design doc scope).
- **Cost alerting/notifications before limit reached.** Only enforcement at limit, not warnings.
- **Historical cost backfill.** Rollups start from deploy time.
- **Claude Code / OpenCode / Codex collectors.** Separate features; Pi collector + OpenRouter direct are in scope for the parent cost tracking feature.
- **Real-time streaming cost display.** Pi extension provides this for pi sessions; other CLIs collect at task completion.
- **Approval workflow for budget overruns.** The `budget_tracking_approval_system.md` design doc covers this as a separate feature.

---

**Requirements extracted. Ready for Scope Review and Architecture Design.**
