# Feature: Budget Enforcement and Pipeline Throttling

## Overview
Implement spend-limit enforcement using `AutopilotProject.cost_limit_usd` and `cost_total_usd` (from cost-schema). Extract `_pause_project_workflows(project_id, paused_by)` from the `/autopilot/stop` route handler in `src/mcp/autopilot_api.py` into a reusable function in `src/autopilot/orchestrator.py` that correctly filters `definition_id.in_(["autopilot", "autopilot-phase0"])` (fixing the existing bug where Phase 0 workflows are missed). Build a `_enforce_budget_limit(project_id)` wrapper that calls `derive_cost_totals()` (from cost-derivation) to get the updated total, then checks `cost_limit_usd` and calls `_pause_project_workflows` when the limit is crossed. Wire this wrapper as the enforcement hook after every CostEntry write. Add `cost_total_usd >= cost_limit_usd` guards at the top of `pick_next_design` and in `_run_one_feature` before launching new work. Generalize every `paused_by == "user"` self-heal/auto-resume guard to `paused_by is not None` — except `start()`'s resume-on-play filter which stays `paused_by == "user"` so the play button cannot backdoor-clear a budget-pause.

## Files Owned
- `src/autopilot/orchestrator.py`
- `src/mcp/autopilot_api.py`

## Dependencies
- `cost-schema` — reads `cost_limit_usd` and `cost_total_usd` on AutopilotProject
- `cost-derivation` — calls `derive_cost_totals()` from this module

## Implementation Notes

### Extract `_pause_project_workflows(project_id, paused_by)` in `src/autopilot/orchestrator.py`
Currently the `/autopilot/stop` endpoint in `src/mcp/autopilot_api.py` contains the logic to pause workflows and terminate active agents. The primary canonical stop logic is around line 3841–3913 (the main autopilot stop handling block). However, `definition_id == "autopilot"` filter logic appears at multiple locations in the file (lines 713, 1078, 1329, 1361, 3849, 4065), so the extracted function should be used at all call sites where workflow pausing/filtering occurs — not just the canonical stop endpoint. This extraction must:
1. Queries `Workflow` where `project_id` matches AND `definition_id.in_(["autopilot", "autopilot-phase0"])` AND `status.in_(["active", "running"])`
2. Sets `status = "paused"`, `paused_by = paused_by`
3. Terminates active agents (with `terminated_at` set per existing invariant)
4. Returns the count of workflows paused
5. Is naturally idempotent (second call finds nothing to pause → returns 0)

**Critical bug fix**: The existing `/autopilot/stop` endpoint filters `definition_id == "autopilot"` only, missing `"autopilot-phase0"` (the Feature Architect workflow). Both must be included.

### Build `_enforce_budget_limit(project_id)` in `src/autopilot/orchestrator.py`
This is the single integration point between cost derivation and budget enforcement:
1. Lookup the project, get `cost_limit_usd` and `cost_total_usd`
2. If `cost_limit_usd is not None and cost_total_usd >= cost_limit_usd`:
   - Call `_pause_project_workflows(project_id, paused_by="budget")`
3. Return whether enforcement was triggered (for logging/debugging)

**Callers of `_enforce_budget_limit`:**
1. `cost_collection_service.py` — after calling `derive_cost_totals(db, task_id)` at task completion, call `_enforce_budget_limit(project_id)` with the returned total
2. `langchain_llm_client.py` — after writing a direct OpenRouter CostEntry and calling `derive_cost_totals`
3. `src/mcp/server.py` — after the POST /api/cost endpoint writes a CostEntry (pi extension submissions)

This wrapper keeps the coupling one-directional: `orchestrator.py` depends on `cost_derivation.py`, not the reverse.

### Guards for new work
In `src/autopilot/orchestrator.py`:
1. **`pick_next_design`** (~line 1936): Before selecting a design for a project, check `project.cost_total_usd >= project.cost_limit_usd` (when limit is set). Skip the project entirely if over budget.
2. **`_run_one_feature`** (~line 2634): Before calling `run_single_workflow` for a new feature, check the same condition on the feature's parent project.

### Generalize `paused_by` guards
Every self-heal/auto-resume guard currently checks `wf.paused_by == "user"`. Change each to `wf.paused_by is not None` (any deliberate pause reason prevents auto-resume):

Locations to update:
1. `src/autopilot/orchestrator.py` — `_try_auto_resume_paused_workflow` area (~line 3710)
2. `src/autopilot/orchestrator.py` — `_create_corrective_task` (~line 5408) — **currently checks `paused_by == "user"` and must be generalized to `is not None` to prevent silent override of budget-paused workflows**
3. `src/autopilot/orchestrator.py` — `attempt_recovery` stuck-workflow restart (~line 2812)
4. `src/mcp/autopilot_api.py` — resume-on-play auto-resume logic
5. `src/mcp/server.py` — workflow resume guard

**EXCEPTION**: `AutopilotService.start()`'s resume-on-play logic (~line 390 in orchestrator.py) KEEPS `paused_by == "user"` — the play button can clear user-pauses but NOT budget-pauses. Clearing a budget-pause requires raising the limit.

### Clearing a budget pause
In the `PUT /projects/{project_id}` endpoint (where the user updates project settings including the new `cost_limit_usd`):
- If the new limit is `None` or `new_limit > project.cost_total_usd`: clear `paused_by` on that project's `"budget"`-paused workflows and set their status back to `"active"`, so the next sweep or a "play" click can resume them.

### Spend always lands slightly over
Cost is only knowable after the LLM call completes. The CostEntry that crosses the limit represents work that already happened — enforcement can only prevent the *next* call. This is inherent, not a bug.

## Acceptance Criteria
- [ ] `_pause_project_workflows(project_id, paused_by)` extracted as reusable function in orchestrator.py
- [ ] Function correctly filters `definition_id.in_(["autopilot", "autopilot-phase0"])` (previously only checked "autopilot")
- [ ] `_enforce_budget_limit(project_id)` exists as the single hook calling `derive_cost_totals` and triggering `_pause_project_workflows` when limit exceeded
- [ ] All CostEntry write paths (task completion, direct OpenRouter, pi extension API) call `_enforce_budget_limit` after derivation
- [ ] `pick_next_design` skips over-budget projects
- [ ] `_run_one_feature` guards against launching new features for over-budget projects
- [ ] All `paused_by == "user"` guards generalized to `paused_by is not None` — including `_create_corrective_task` — (except `start()`'s play-button resume)
- [ ] Raising/ clearing the cost limit auto-clears `paused_by="budget"` on paused workflows
- [ ] Enforcement is naturally-idempotent (concurrent CostEntry writes don't cause cascading pauses)