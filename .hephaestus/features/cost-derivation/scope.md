# Feature: Cost Derivation Engine

## Overview
Build `src/core/cost_derivation.py`, mirroring the existing `src/core/status_derivation.py` pattern. This module provides `derive_cost_totals(db, task_id)` which reads SUM(cost_entries.cost_usd) grouped by task_id and writes it to Task.cost_total_usd, then rolls up through Feature → AutopilotDesign → AutopilotProject using the existing entity hierarchy relationships. Called on every new CostEntry write so the denormalized totals are always current without a separate polling loop. After writing project.cost_total_usd, if the project has a cost_limit_usd set and the total crosses it, trigger budget enforcement by calling `_pause_project_workflows(project.id, paused_by='budget')` from `src/autopilot/orchestrator.py` — this is the single integration point for budget enforcement, keeping the check colocated with the write that crosses the threshold.

## Files Owned
- `src/core/cost_derivation.py`

## Dependencies
- `cost-schema` — requires `cost_entries` table and `cost_total_usd` columns to exist
- `budget-enforcement` — imports `_pause_project_workflows` from `src/autopilot/orchestrator.py` for budget enforcement check

## Implementation Notes

### Core function: `derive_cost_totals(db, task_id)`
1. `SUM(cost_entries.cost_usd)` WHERE task_id matches → write to `Task.cost_total_usd`
2. Derive parent Feature's total: `SUM` of all task cost_total_usd WHERE `Task.workflow_id` maps to the feature's workflow → write to `Feature.cost_total_usd`
3. Roll up to AutopilotDesign: `SUM` of feature totals WHERE `Feature.design_id == design.id` → write to `AutopilotDesign.cost_total_usd`
4. Roll up to AutopilotProject: `SUM` of design totals WHERE `AutopilotDesign.project_id == project.id` → write to `AutopilotProject.cost_total_usd`

### Rollup with NULL task_id (overhead costs)
Some `CostEntry` rows have `task_id=NULL` (guardian/conductor housekeeping calls). These should be included at the workflow and project level. After the task-scoped rollup, compute a second aggregate: `SUM(cost_entries.cost_usd) WHERE task_id IS NULL AND workflow_id = X` and add that to the Feature.total for features whose workflow_id matches.

### Module structure
Follow `status_derivation.py`'s pattern exactly:
- `derive_cost_totals(db: Session, task_id: str, write_back: bool = True) -> float` — primary entry, returns the new total
- `derive_feature_cost_total(db: Session, feature_id: str) -> float`
- `derive_design_cost_total(db: Session, design_id: str) -> float`
- `derive_project_cost_total(db: Session, project_id: str) -> float`

Each function does `SELECT SUM` from the child table, then `UPDATE SET cost_total_usd` if `write_back=True`.

### When to call
The caller (`cost_collection_service.py` at task completion, and `langchain_llm_client.py` for direct OpenRouter calls) passes `write_back=True` to update the denormalized columns.

### Budget enforcement integration
After writing `project.cost_total_usd` (step 4 above), if the project has a `cost_limit_usd` set and `cost_total_usd >= cost_limit_usd`, call `_pause_project_workflows(project.id, paused_by='budget')`. This function is provided by the `budget-enforcement` feature in `src/autopilot/orchestrator.py`. The check runs synchronously as part of the derive flow, so enforcement is immediate on the CostEntry that crosses the threshold.

## Acceptance Criteria
- [ ] `src/core/cost_derivation.py` exists and follows `status_derivation.py` patterns
- [ ] `derive_cost_totals(db, task_id)` correctly aggregates from `cost_entries` and writes to Task/Feature/AutopilotDesign/AutopilotProject
- [ ] NULL task_id overhead costs are included in Feature and project-level totals
- [ ] Returns the computed cost_total_usd value (not just side-effect writes)
- [ ] Handles empty `cost_entries` gracefully (returns 0.0, no errors)
- [ ] After writing project cost_total_usd, checks cost_limit_usd and calls `_pause_project_workflows` if limit exceeded