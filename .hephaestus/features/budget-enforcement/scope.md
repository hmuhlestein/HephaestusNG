# Feature: Budget Enforcement and Pipeline Throttling

## Overview
Implement spend-limit enforcement using `AutopilotProject.cost_limit_usd` and `cost_total_usd` (from cost-schema). Extract `_pause_project_workflows(project_id, paused_by)` from the `/autopilot/stop` route handler in `src/autopilot/orchestrator.py` into a reusable function that correctly filters `definition_id.in_(["autopilot", "autopilot-phase0"])` (fixing the existing bug where Phase 0 workflows are missed). Hook this into `cost_derivation.py`'s recompute path: after updating `cost_total_usd`, check the limit and call `_trigger_budget_pause` with `paused_by='budget'`. Add `cost_total_usd >= cost_limit_usd` guards at the top of `pick_next_design` and in `_run_one_feature` before launching new work. Generalize every `paused_by == 'user'` self-heal/auto-resume guard to `paused_by is not None` across orchestrator.py, autopilot_api.py, and server.py — but deliberately leave `AutopilotService.start()`'s resume-on-play filter as `paused_by == 'user'` so the play button cannot backdoor-clear a budget-pause.

## Files Owned
- `src/autopilot/orchestrator.py`
- `src/mcp/autopilot_api.py`

## Dependencies
- `cost-schema` — reads `cost_limit_usd` and `cost_total_usd` on AutopilotProject
- `cost-derivation` — triggers enforcement check after cost totals are updated

## Implementation Notes

### Extract `_pause_project_workflows(project_id, paused_by)`
Currently the `/autopilot/stop` endpoint in `src/mcp/autopilot_api.py` contains the logic to pause workflows and terminate active agents. This must be extracted into a standalone function that:
1. Queries `Workflow` where `project_id` matches AND `definition_id.in_(["autopilot", "autopilot-phase0"])` AND `status.in_(["active", "running"])`
2. Sets `status = "paused"`, `paused_by = paused_by`
3. Terminates active agents (with `terminated_at` set per existing invariant)
4. Returns the count of workflows paused
5. Is naturally idempotent (second call finds nothing to pause → returns 0)

**Critical bug fix**: The existing `/autopilot/stop` endpoint filters `definition_id == "autopilot"` only, missing `"autopilot-phase0"` (the Feature Architect workflow). Both must be included.

### Integration point in `cost_derivation.py`
After `derive_cost_totals` writes the updated `project.cost_total_usd`:
```python
if project.cost_limit_usd is not None and project.cost_total_usd >= project.cost_limit_usd:
    _pause_project_workflows(project.id, paused_by="budget")
```
This runs synchronously as part of the derive flow, so enforcement is immediate on the CostEntry that crosses the threshold.

### Guards for new work
In `src/autopilot/orchestrator.py`:
1. **`pick_next_design`** (~line 1936): Before selecting a design for a project, check `project.cost_total_usd >= project.cost_limit_usd` (when limit is set). Skip the project entirely if over budget.
2. **`_run_one_feature`** (~line 2634): Before calling `run_single_workflow` for a new feature, check the same condition on the feature's parent project.

### Generalize `paused_by` guards
Every self-heal/auto-resume guard currently checks `wf.paused_by == "user"`. Change each to `wf.paused_by is not None` (any deliberate pause reason prevents auto-resume):

Locations to update:
1. `src/autopilot/orchestrator.py` — `_try_auto_resume_paused_workflow` area (~line 3710)
2. `src/autopilot/orchestrator.py` — `attempt_recovery` stuck-workflow restart (~line 2812)
3. `src/mcp/autopilot_api.py` — resume-on-play auto-resume logic
4. `src/mcp/server.py` — workflow resume guard

**EXCEPTION**: `AutopilotService.start()`'s resume-on-play logic (~line 390 in orchestrator.py) KEEPS `paused_by == "user"` — the play button can clear user-pauses but NOT budget-pauses. Clearing a budget-pause requires raising the limit.

### Clearing a budget pause
In the `PUT /projects/{project_id}` endpoint (where the user updates project settings including the new `cost_limit_usd`):
- If the new limit is `None` or `new_limit > project.cost_total_usd`: clear `paused_by` on that project's `"budget"`-paused workflows and set their status back to `"active"`, so the next sweep or a "play" click can resume them.

### Spend always lands slightly over
Cost is only knowable after the LLM call completes. The CostEntry that crosses the limit represents work that already happened — enforcement can only prevent the *next* call. This is inherent, not a bug.

## Acceptance Criteria
- [ ] `_pause_project_workflows(project_id, paused_by)` extracted as reusable function in orchestrator.py
- [ ] Function correctly filters `definition_id.in_(["autopilot", "autopilot-phase0"])` (previously only checked "autopilot")
- [ ] `pick_next_design` skips over-budget projects
- [ ] `_run_one_feature` guards against launching new features for over-budget projects
- [ ] All `paused_by == "user"` guards generalized to `paused_by is not None` (except `start()`'s play-button resume)
- [ ] Raising/ clearing the cost limit auto-clears `paused_by="budget"` on paused workflows
- [ ] Enforcement is natural-idempotent (concurrent CostEntry writes don't cause cascading pauses)