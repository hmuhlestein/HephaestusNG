# Adversarial Review — Cost Derivation Engine (Run 4)

**Reviewer:** Hephaestus Adversarial Agent (Phase 6)  
**Date:** 2025-07-21  
**Scope:** Verification of 2 BLOCKERs + 4 WARNINGs from prior run  
**Verdict:** CONDITIONAL PASS — 1 BLOCKER remains (race condition). 1 WARNING remains (missing 'starting' status).

---

## BLOCKER Verification Summary

### BLOCKER-1: `/autopilot/stop` NOT Refactored — Phase 0 Workflows Survive User Stop — **FIXED ✅**

**File:** `src/mcp/autopilot_api.py:4013`

The `stop_pipeline` function now queries `Workflow.definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS)` where `DESIGN_WORKFLOW_DEFINITION_IDS = ("autopilot", "autopilot-phase0", "feature_architect")`. Phase 0 workflows are now properly included when the user clicks Stop.

**Verification:** `src/core/constants.py:42-43` confirms `DESIGN_WORKFLOW_DEFINITION_IDS` includes `"autopilot-phase0"`.

---

### BLOCKER-2: `_run_one_feature` Budget Guard Uses Separate DB Session — Race Condition — **STILL PRESENT ❌**

**File:** `src/autopilot/orchestrator.py:6436-6439`

```python
# Budget guard: refuse to launch features for over-budget projects
from src.core.cost_derivation import check_budget_before_new_work

if project_id:
    with get_db() as budget_db:  # <-- SEPARATE session!
        if not check_budget_before_new_work(budget_db, project_id):
```

**Failure Sequence:**
1. Thread A enters `_run_one_feature`, opens `get_db()` at line ~6380 for feature lookup
2. Thread B records cost and triggers `_check_budget_enforcement` → pauses workflows
3. Thread A opens SECOND `get_db()` at line 6437 for budget check
4. Thread B's commit may not be visible to Thread A's new session (depending on SQLite WAL timing)
5. Budget guard reads stale `project.cost_total_usd` → returns True
6. New workflow launches despite project being over budget

**Impact:** Under concurrent feature pipelines, a new feature can launch after budget is exceeded, consuming additional tokens.

**Recommended Fix:** Reuse the existing `db` session from the earlier context block, or pass the session as a parameter to `check_budget_before_new_work`.

---

## WARNING Verification

### WARNING-1: `_pause_project_workflows` Missing 'starting' Agent Status — **STILL PRESENT ⚠️**

**File:** `src/core/cost_derivation.py:355`

```python
agents_to_terminate = (
    db.query(Agent)
    .join(Task, Agent.current_task_id == Task.id)
    .filter(
        Task.workflow_id.in_(workflow_ids),
        Agent.status.in_(["working", "idle"]),  # <-- Missing "starting"
    )
    .all()
)
```

**Failure Sequence:**
1. Agent spawned, enters "starting" state
2. Budget exceeded → `_pause_project_workflows` called
3. Filter excludes "starting" agents
4. Agent transitions to "working"
5. Agent continues spending past budget

**Recommended Fix:** Change to `Agent.status.in_(["working", "starting", "idle"])`

---

### WARNING-2 through WARNING-4: **Status Unknown**

The prior run's report files were deleted, so I cannot verify the specific findings for:
- Misleading log messages for generalized paused_by guards
- Stale status_reason on user-paused workflows  
- OpenRouter direct costs bypassing budget enforcement

These may or may not have been addressed. A full re-review would be needed to confirm.

---

## New Issues Found

No new BLOCKERs or WARNINGs were introduced by recent changes (mostly formatting/style changes to orchestrator.py).

---

## Verdict: CONDITIONAL PASS

**1 BLOCKER remains** (race condition in `_run_one_feature` budget guard). This is a low-probability but high-impact issue under concurrent feature pipelines. The fix is straightforward (reuse existing DB session).

**1 WARNING remains** (missing 'starting' status in `_pause_project_workflows`). This is a medium-probability issue that could allow agents to spend past budget.

**Recommendation:** Fix BLOCKER-2 before merge. WARNING-1 can be addressed in a follow-up.
