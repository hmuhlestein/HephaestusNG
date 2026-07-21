# Adversarial Review Report: Budget Enforcement and Pipeline Throttling

**Reviewer:** Hephaestus Adversarial Review (Phase 6)  
**Date:** 2026-07-21  
**Commit:** `932134a` (Phase 4 development)  
**Architecture Source:** `docs/architecture.md` (Phase 3)  
**Requirements Source:** `docs/requirements_analysis.md` (Phase 1)  
**Prior Review:** `docs/architectural_review/architectural_review_report.md` (Phase 5)

---

## Executive Summary

The implementation has **2 BLOCKERs** that will cause production failures under realistic concurrency, **4 WARNINGs** that cause silent data integrity issues or operational confusion, and **3 NITs**. The most critical finding is that the `/autopilot/stop` endpoint was **not refactored** to use the shared `_pause_project_workflows()` function, leaving Phase 0 workflows running when the user clicks "Stop" — this is the exact bug the requirements identified and explicitly required fixing. The second BLOCKER is a race condition in the `_run_one_feature` budget guard where a separate DB session is used, allowing stale reads under concurrent cost recording.

All 19 tests in `test_budget_enforcement.py` pass, but the test suite has significant behavioral gaps: 2 of 4 `TestPickNextDesignBudgetGuard` tests only assert that source code contains a string, not that the guard actually blocks over-budget projects.

---

## BLOCKER Findings

### BLOCKER-1: `/autopilot/stop` NOT Refactored — Phase 0 Workflows Survive User Stop

**Severity:** BLOCKER  
**Location:** `src/mcp/autopilot_api.py`, `stop_pipeline()`, line ~3670  
**Requirement Violated:** FR-4 (Refactor `/autopilot/stop` to use shared `_pause_project_workflows`)

**Failure Sequence:**
1. User starts a pipeline. Phase 0 (Feature Architect) launches with `definition_id="autopilot-phase0"`.
2. Phase 0 agent begins consuming LLM tokens (typically $0.50-$2.00 per run).
3. User clicks "Stop" in the UI (e.g., project is at budget limit).
4. `stop_pipeline()` queries: `db.query(Workflow).filter_by(definition_id="autopilot")`.
5. Phase 0 workflow is **NOT found** — its `definition_id` is `"autopilot-phase0"`.
6. Phase 0 agent continues running, unchecked.
7. If the project was at its budget limit, Phase 0 continues spending past it.
8. The user believes the pipeline is stopped, but Phase 0 is still burning tokens.

**Current Code:**
```python
query = db.query(Workflow).filter_by(definition_id="autopilot").filter(
    Workflow.status.in_(["active", "running"])
)
```

**Required Fix:**
```python
from src.core.cost_derivation import _pause_project_workflows
for stopped_project_id in stopped_project_ids:
    _pause_project_workflows(db, stopped_project_id, paused_by="user")
```

The shared `_pause_project_workflows` already filters `definition_id.in_(["autopilot", "autopilot-phase0"])`. This fix was explicitly specified in FR-4 and the architecture document.

**Impact:** Real financial loss. Phase 0 typically takes 5-15 minutes and costs $0.50-$2.00. Users who click "Stop" to halt spending will find Phase 0 still running.

---

### BLOCKER-2: `_run_one_feature` Budget Guard Uses Separate DB Session — Race Condition

**Severity:** BLOCKER  
**Location:** `src/autopilot/orchestrator.py`, `_run_one_feature()`, lines ~7119-7131  
**Requirement Violated:** Architecture specified "use existing DB session"

**Failure Sequence:**
1. `_run_one_feature` opens `get_db()` at line ~7020 to read feature record and project (session A).
2. Feature is "active", `workflow_id` is set. Code continues to budget guard.
3. Budget guard opens a **second** `get_db()` context at line ~7119 (session B).
4. **Between** sessions A and B closing and B opening, another thread completes a task.
5. That thread calls `collect_task_cost()` → `record_cost()` → `derive_project_cost()`.
6. `derive_project_cost` updates `project.cost_total_usd` to exceed the limit.
7. `_check_budget_enforcement` calls `_pause_project_workflows`, pausing all active workflows.
8. Session B's budget guard reads the project from its **own** snapshot.
9. Session B sees `cost_total_usd` from **before** the cost recording (stale read).
10. Budget guard returns `True` (under budget) — the new workflow is launched.
11. A new feature workflow starts despite the project being over budget.

**Current Code:**
```python
# Budget guard: block new workflow launches if project is over budget
if project_id:
    from src.core.cost_derivation import check_budget_before_new_work
    with get_db() as budget_db:  # ← SECOND session, can be stale
        if not check_budget_before_new_work(budget_db, project_id):
```

**Required Fix:**
Reuse the earlier `db` session from line ~7020's `with get_db() as db:` block. If the budget guard must be outside that block, capture and reuse the session manager reference rather than opening a new context.

**Impact:** Under concurrent feature pipelines (the default with `MAX_PARALLEL_FEATURES=4`), new workflows can be launched after the budget is exceeded. The cost derivation system will catch up eventually (next `record_cost` call triggers enforcement), but there's a window where spending continues unchecked.

---

## WARNING Findings

### WARNING-1: `_pause_project_workflows` Missing "starting" Agent Status

**Severity:** WARNING  
**Location:** `src/core/cost_derivation.py`, `_pause_project_workflows()`, line ~340

**Failure Sequence:**
1. An agent is spawned for a new task. Its status is "starting" (initial state before tmux session is confirmed).
2. The project exceeds its budget while the agent is still starting.
3. `_pause_project_workflows` is called.
4. The agent filter: `Agent.status.in_(["working", "idle"])` — "starting" is excluded.
5. The "starting" agent is NOT terminated.
6. The agent transitions to "working" and begins consuming LLM tokens.
7. The project continues spending despite being over budget.

**Evidence:** The rest of the codebase consistently uses `["working", "starting", "idle"]`:
- `src/autopilot/orchestrator.py:1300`: `("working", "starting", "idle")`
- `src/autopilot/orchestrator.py:1589`: `("working", "starting", "idle")`
- `src/autopilot/orchestrator.py:5478`: `["working", "idle", "starting"]`
- `src/mcp/autopilot_api.py` (stop endpoint): `["working", "starting", "idle"]`

**Fix:**
```python
Agent.status.in_(["working", "starting", "idle"])
```

---

### WARNING-2: Misleading Log Messages for Generalized `paused_by` Guards

**Severity:** WARNING  
**Location:** `src/autopilot/orchestrator.py`, lines 5703 and 5884

**Problem:**
The `paused_by` guards were generalized from `== "user"` to `is not None` (now also covers "budget", "system", etc.), but the log messages still say "user-paused":

- Line 5703: `f"[CORRECTIVE-TASK] Workflow {workflow_id[:8]} is user-paused — skipping corrective task"`
- Line 5884: `f"[RESUME-STUCK] Workflow {workflow_id[:8]} is user-paused — skipping"`

When a workflow is paused by budget enforcement, operators see "user-paused" in the logs. This causes confusion about why the pipeline stalled — operators think they accidentally paused it, when in reality the budget limit was reached.

**Fix:**
```python
f"[CORRECTIVE-TASK] Workflow {workflow_id[:8]} is deliberately paused (paused_by={wf.paused_by}) — skipping"
f"[RESUME-STUCK] Workflow {workflow_id[:8]} is deliberately paused (paused_by={wf.paused_by}) — skipping"
```

---

### WARNING-3: User-Paused Workflows Preserve Stale `status_reason`

**Severity:** WARNING  
**Location:** `src/core/cost_derivation.py`, `_pause_project_workflows()`

**Failure Sequence:**
1. Project goes over budget. `_pause_project_workflows` sets `paused_by="budget"` and `status_reason="Budget limit reached"`.
2. User raises the limit. Workflows resume. The resume code (in `autopilot_api.py`) clears `status_reason`.
3. Project goes over budget again. Workflows paused again with `status_reason="Budget limit reached"`.
4. User pauses the pipeline via `/autopilot/stop`. The stop endpoint sets `paused_by="user"`.
5. `status_reason` remains `"Budget limit reached"` — stale and misleading.
6. The `/autopilot/status` endpoint returns `status_reason="Budget limit reached"` for a user-paused workflow.

**Fix:** When `paused_by="user"`, explicitly clear `status_reason`:
```python
if paused_by == "budget":
    wf.status_reason = "Budget limit reached"
elif paused_by == "user":
    wf.status_reason = None  # Clear any stale reason
```

---

### WARNING-4: OpenRouter Direct Costs Without Entity Links Bypass Budget Enforcement

**Severity:** WARNING  
**Location:** `src/core/cost_derivation.py`, `record_cost()` function; `src/mcp/autopilot_api.py`, `POST /cost-entries`

**Failure Sequence:**
1. An external caller posts to `POST /cost-entries` with `source="openrouter_direct"`, `cost_usd=5.0`, `workflow_id=None`, `task_id=None`.
2. `record_cost` creates the CostEntry row in the ledger.
3. Since both `task_id` and `workflow_id` are `None`, no derivation rollup is triggered:
   ```python
   if task_id:
       derive_task_cost(db, task_id, write_back=True)
   if workflow_id:
       derive_workflow_cost(db, workflow_id, write_back=True)
   # Both skipped — no rollup, no budget check
   ```
4. No entity's `cost_total_usd` is updated.
5. `_check_budget_enforcement` is never called.
6. The project's actual spend is higher than `cost_total_usd` reports.

**Impact:** The cost ledger is accurate (the entry exists), but the derived totals on Task/Feature/Design/Project are understated. Budget enforcement only fires on the next cost entry that DOES have an entity link. If the only cost source is direct API calls without entity links, budget enforcement never fires.

**Mitigation:** Low risk in practice — the Pi extension always provides `task_id` and `agent_id`. But the API allows calls without them.

**Fix:** Either require at least one of `task_id`/`workflow_id`, or route unlinked costs through a fallback derivation path that updates the project total directly.

---

## NIT Findings

### NIT-1: Tests Use Source Inspection Instead of Behavioral Assertions

**Severity:** NIT  
**Location:** `tests/test_budget_enforcement.py`, `TestPickNextDesignBudgetGuard`

**Problem:**
Two tests only check that the function source code contains a string:
```python
def test_budget_guard_is_wired_into_pick_next_design(self):
    import inspect
    from src.autopilot.orchestrator import pick_next_design
    src = inspect.getsource(pick_next_design)
    assert "check_budget_before_new_work" in src

def test_budget_guard_is_wired_into_run_one_feature(self):
    import inspect
    from src.autopilot.orchestrator import _run_one_feature
    src = inspect.getsource(_run_one_feature)
    assert "check_budget_before_new_work" in src
```

These tests pass even if:
- The guard is commented out
- The guard is unreachable (inside an `if False:` block)
- The guard's result is ignored (e.g., `check_budget_before_new_work(...); return True`)

**Fix:** Replace with behavioral tests that create a real in-memory DB, set `cost_total_usd > cost_limit_usd`, and verify:
- `pick_next_design` returns `None` when project is over budget
- `_run_one_feature` returns `"budget_blocked"` when project is over budget
- Both proceed normally when under budget

---

### NIT-2: `_extract_session_id` Fragile Tmux Name Parsing

**Severity:** NIT  
**Location:** `src/services/cost_collection_service.py`, `_extract_session_id()`

**Problem:**
The function extracts session IDs by splitting the tmux session name on hyphens:
```python
parts = agent.tmux_session_name.split("-")
if len(parts) >= 2:
    return "-".join(parts[1:])  # Skip "hephaestus" prefix
```

This is fragile: if the naming format changes (e.g., project name contains hyphens, or the prefix changes), extraction fails silently. When it fails, `session_id` is `None`, and cost collection is entirely skipped for that agent's task — costs are silently lost.

**Impact:** Silent cost data loss for agents whose tmux names don't match the expected format.

**Fix:** Store the session_id explicitly in the Agent model or a metadata column, rather than deriving it from the tmux session name.

---

### NIT-3: `AgentStatus` Constants Missing "starting"

**Severity:** NIT  
**Location:** `src/core/database.py`, `AgentStatus` class (line 39)

**Problem:**
`AgentStatus` defines: `IDLE`, `WORKING`, `STUCK`, `TERMINATED`. But the codebase uses `"starting"` in 4+ locations:
- `stop_pipeline()` in autopilot_api.py
- `is_design_fully_complete()` in orchestrator.py
- `attempt_recovery()` in orchestrator.py
- `peek_agent_output()` in orchestrator.py

This creates a discrepancy between the canonical status constants and actual usage.

**Fix:** Add `STARTING = "starting"` to `AgentStatus.ALL`.

---

## Summary

| Category | Count | Details |
|----------|-------|---------|
| BLOCKER | 2 | Phase 0 gap in stop endpoint; stale DB session in budget guard |
| WARNING | 4 | Missing agent status; misleading logs; stale status_reason; unlinked costs |
| NIT | 3 | Source inspection tests; fragile session ID parsing; missing status constant |

### Risk Assessment

The two BLOCKERs represent real production risks:

1. **BLOCKER-1** (Phase 0 gap) is the highest priority. Every user who clicks "Stop" expecting Phase 0 to halt will continue paying for LLM tokens. This was an explicit requirement (FR-4) and a known bug that the architecture specifically called out.

2. **BLOCKER-2** (stale DB session) is a race condition that manifests under concurrent feature pipelines. With `MAX_PARALLEL_FEATURES=4`, multiple features run simultaneously, and cost recording from one feature's task completion can race with another feature's budget guard check. The window is small but real, and the consequence is launching a new workflow after the budget is exceeded.

### Recommended Fix Priority

1. **BLOCKER-1:** Replace `/autopilot/stop` inline pause logic with `_pause_project_workflows(db, project_id, "user")`. 5-line change.
2. **BLOCKER-2:** Reuse the existing `db` session in `_run_one_feature`'s budget guard. 3-line change.
3. **WARNING-1:** Add "starting" to agent status filter. 1-line change.
4. **WARNING-2:** Fix log messages. 2-line change.
5. **WARNING-3:** Clear `status_reason` on user pause. 2-line change.
6. **WARNING-4:** Add validation requiring entity links on cost entries. 5-line change.
7. **NITs:** Address in follow-up.

---

*Review complete. 2 BLOCKERs, 4 WARNINGs, 3 NITs identified.*
