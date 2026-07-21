# Feature: Cost Tracking UI

## Overview
Add cost visibility and budget configuration to the frontend. In `ProjectSettingsModal.tsx`, add a `cost_limit_usd` number input (optional, blank = no limit) wired to the existing `PUT /projects/{project_id}` mutation. In the autopilot design screen (via `DesignQueuePanel.tsx` and/or `PipelineStatusCard.tsx`), add a cost-so-far indicator showing current spend with a link that opens ProjectSettingsModal for the active project. Surface `paused_by == 'budget'` as a distinct badge/message ('Paused: budget limit reached') in whatever status display already exists for paused workflows. Also display `cost_total_usd` on feature cards, design rows, and project-level summary in the autopilot dashboard — the field already flows through `autopilot_api.py`'s existing report shape, so this is additive plumbing to existing UI components that already read `cost_total` (confirmed in `FeatureGallery.tsx` and `FeatureDetailModal.tsx`).

## Files Owned
- `frontend/src/components/ProjectSettingsModal.tsx`
- `frontend/src/components/autopilot/DesignQueuePanel.tsx`
- `frontend/src/components/autopilot/PipelineStatusCard.tsx`
- `frontend/src/components/autopilot/FeatureGallery.tsx`
- `frontend/src/components/autopilot/FeatureDetailModal.tsx`
- `frontend/src/pages/Autopilot.tsx`
- `frontend/src/types/index.ts`

## Dependencies
- `cost-schema` — frontend types need `cost_total_usd`, `cost_limit_usd` fields
- `budget-enforcement` — UI surfaces `paused_by == 'budget'` distinction from backend

## Implementation Notes

### ProjectSettingsModal.tsx — Budget Limit Input
- Add a number input field for `cost_limit_usd` (optional, cleared/blank = no limit)
- Wire to the existing `PUT /projects/{project_id}` mutation that `apiService` already handles for `name`/`base_dir`/`is_default`
- Backend `ProjectUpdate` schema needs `cost_limit_usd` added (this is a backend change in `src/mcp/autopilot_api.py` or wherever the PUT endpoint is defined — included in budget-enforcement feature)
- Show current `cost_total_usd` alongside the limit input for context

### DesignQueuePanel.tsx / PipelineStatusCard.tsx — Spend Indicator
- Add a compact cost widget: `$current / $limit` (or just `$current spent` when no limit)
- When the pipeline is paused with `paused_by == "budget"`, show a distinct badge: "Paused: budget limit reached" (not just "Paused")
- Include a link/button that opens ProjectSettingsModal scoped to the active project, so a user who sees auto-pause has a clear path to raise the limit
- The `$current` value comes from `AutopilotProject.cost_total_usd` which is already part of the API response shapes

### FeatureGallery.tsx — Already Partially Done!
- Already reads `feature.cost_total` and displays it as `${feature.cost_total.toFixed(2)}` on feature cards (lines 184-187, 227-228)
- Verify this works correctly with the new cost collection pipeline producing real (non-zero) data
- May need minor polish: handle null/undefined gracefully, show "—" instead of "$0.00" when no cost data exists

### FeatureDetailModal.tsx — Already Done!
- Already reads `detail.cost_total` and displays it (line 220): `{ label: 'Cost', value: detail.cost_total > 0 ? ... : 'N/A' }`
- No changes needed unless the display format needs adjustment

### Autopilot.tsx / Project-level summary
- Add a project-level cost summary to the autopilot dashboard
- Show cumulative project spend, possibly with a breakdown by source (pi vs openrouter vs claude_code)
- This is a new section in the existing page layout

### Frontend type updates
- `frontend/src/types/index.ts` (or wherever autopilot types are defined) needs `cost_total_usd` and `cost_limit_usd` fields on the project type, and `cost_total_usd` on feature/design types
- Check that the backend API response shapes already include these fields (they should after the schema and API changes from cost-schema and budget-enforcement features)

### Backward compatibility
- Cost fields default to 0/null in the DB, so existing data renders safely
- UI components should handle 0.0 and null gracefully (show "N/A" or "—" not "$NaN")

## Acceptance Criteria
- [ ] ProjectSettingsModal has a numeric input for cost_limit_usd that submits via PUT /projects/{project_id}
- [ ] Autopilot design screen shows a cost indicator (current spend / limit) on the project
- [ ] `paused_by == 'budget'` workflows show "Paused: budget limit reached" badge distinct from user-paused
- [ ] Clicking "manage budget" link opens ProjectSettingsModal for the active project
- [ ] Feature gallery cards display cost correctly (already partially done; verify it works with real data)
- [ ] Project-level cost summary exists on the Autopilot page
- [ ] All cost values handle null/0.0/undefined gracefully with no NaN or broken display