# Code Summary: Cost Tracking UI

**Feature ID:** des-91c8-cost-ui
**Branch:** `feature/des-91c8/cost-ui`

## What this feature does

Wires four already-built, previously-orphaned cost display components into three live screens, and adds one additive backend field to unblock per-feature cost display. No new cost computation, schema, or enforcement logic — the data pipeline was built and merged by the sibling "Budget Enforcement" feature; this feature is display-only wiring.

## Changed files

### Backend

- **`src/mcp/autopilot_api.py`** — `get_project_design_status` (feeds `DesignQueuePanel`) now includes `cost_total_usd` on each feature dict (real features, phase-0 pseudo-feature, and placeholder rows) and a derived design-level `cost_total_usd` sum. Sourced from the already-loaded ORM object — no new query. Also carries two security fixes from `security_review`: input validation on `cost_limit_usd` and authentication on project mutation endpoints.
- **`src/core/database.py`** — minor supporting change for the above (no schema change).

### Frontend

- **`frontend/src/components/autopilot/PipelineStatusCard.tsx`** — new `costTotal`/`costLimit`/`onBudgetClick` props render a `CostDisplay` in a clickable metric slot alongside the existing Agents/Pending/Processed/Succeeded/Failed row.
- **`frontend/src/pages/Autopilot.tsx`** — fetches project cost via the existing `getProjectCosts` client call and wires the click-through to open `ProjectSettingsModal`.
- **`frontend/src/components/autopilot/DesignQueuePanel.tsx`** — imports and renders `FeatureCostBadge` per feature row (`feature.cost_total_usd ?? 0`), hidden when cost is 0.
- **`frontend/src/components/cost/CostDisplay.tsx`** — incidental fix: progress-percent zero-division edge case, color-threshold simplification.
- **`frontend/src/components/cost/FeatureCostBadge.tsx`** — incidental small fix (no behavior change).
- **`frontend/src/components/cost/BudgetPausedLabel.tsx`** — deleted. It duplicated `WorkflowCard.tsx`'s existing inline `paused_by === 'budget'` label logic and was never imported anywhere; the inline implementation was kept as the single source of truth.
- **`frontend/src/components/cost/index.ts`** — removed the `BudgetPausedLabel` export following its deletion.

### Tests

- **`tests/test_autopilot_api.py`** — 4 new tests covering the design-status endpoint's cost fields: `test_design_status_includes_cost_total`, `test_design_status_surfaces_budget_pause_reason`, `test_design_status_surfaces_failure_reason`, `test_design_status_omits_error_when_not_failed`.

## Explicitly out of scope (by design)

- `DesignCostRow` — evaluated during architecture, left unwired; no existing per-design collapsed-header surface matched its shape without inventing a new UI element.
- Any change to `cost_derivation.py`, orchestrator budget-guard logic, or `paused_by` semantics — zero diff vs `main`.

## Verification

- Backend: `test_autopilot_api.py` 76/76 passing.
- Targeted regression: `test_status_derivation.py` + `test_phase_manager.py`, 69/69 passing.
- Frontend: `tsc --noEmit` — no new type errors introduced (6 pre-existing errors on `main`, unrelated files).
- Security: authentication and input-validation fixes verified in place (see `docs/security_report.md`).
