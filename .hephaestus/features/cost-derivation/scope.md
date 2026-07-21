# Feature: Cost Derivation Engine

## Overview
Build `src/core/cost_derivation.py`, mirroring the existing `src/core/status_derivation.py` pattern. This module provides `derive_cost_totals(db, task_id)` which reads SUM(cost_entries.cost_usd) grouped by task_id and writes it to Task.cost_total_usd, then rolls up through Feature → AutopilotDesign → AutopilotProject using the existing entity hierarchy relationships. Called on every new CostEntry write so the denormalized totals are always current without a separate polling loop. **This module is pure derivation only** — it computes and writes totals, then returns the new value. It performs no side effects (no workflow pausing, no enforcement checks). The budget enforcement hook (checking cost_limit_usd and calling `_pause_project_workflows` when the limit is crossed) is the responsibility of the `budget-enforcement` feature's caller, not this module.

## Files Owned
- `src/core/cost_derivation.py`

## Dependencies
- `cost-schema` — requires `cost_entries` table and `cost_total_usd` columns to exist

## Implementation Notes

### Core function: `derive_cost_totals(db, task_id)`
1. `SUM(cost_entries.cost_usd)` WHERE task_id matches → write to `Task.cost_total_usd`
2. Derive parent Feature's total: `SUM` of all task cost_total_usd WHERE `Task.workflow_id` maps to the feature's workflow → write to `Feature.cost_total_usd`
3. Roll up to AutopilotDesign: `SUM` of feature totals WHERE `Feature.design_id == design.id` → write to `AutopilotDesign.cost_total_usd`
4. Roll up to AutopilotProject: `SUM` of design totals WHERE `AutopilotDesign.project_id == project.id` → write to `AutopilotProject.cost_total_usd`
5. **Return** the new `project.cost_total_usd` (or 0.0 if no project found), so callers can inspect it for enforcement logic.

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
The caller (`cost_collection_service.py` at task completion, and `langchain_llm_client.py` for direct OpenRouter calls) passes `write_back=True` to update the denormalized columns and receives the computed value back.

### No budget enforcement here
This module does NOT import or call `_pause_project_workflows`. It does NOT check `cost_limit_usd`. Those concerns belong to the `budget-enforcement` feature and its callers. `derive_cost_totals` is a pure data transformer: read cost_entries, compute totals, write them back, return the value.

## Acceptance Criteria
- [ ] `src/core/cost_derivation.py` exists and follows `status_derivation.py` patterns
- [ ] `derive_cost_totals(db, task_id)` correctly aggregates from `cost_entries` and writes to Task/Feature/AutopilotDesign/AutopilotProject
- [ ] NULL task_id overhead costs are included in Feature and project-level totals
- [ ] Returns the computed cost_total_usd value (not just side-effect writes)
- [ ] Handles empty `cost_entries` gracefully (returns 0.0, no errors)
- [ ] Module has NO imports from `src/autopilot/orchestrator.py` and NO budget enforcement logic