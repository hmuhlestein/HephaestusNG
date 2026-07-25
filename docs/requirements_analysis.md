# Product Requirements Analysis: Cost Tracking UI

**Feature ID:** des-91c8-cost-ui
**Feature Name:** Cost Tracking UI
**Status:** Requirements Extracted
**Date:** 2026-07-24
**Design Document:** `.hephaestus/design.md` (UI section, lines 438-457; Implementation Phase 7, lines 706-711)
**Parent Feature:** Budget Enforcement and Pipeline Throttling (DES-91c8) — already merged (`199ff5a`, `b0c74e2`)

---

## 0. Critical Finding: This Scope Appears Already Implemented

The Budget Enforcement feature (already merged to `main`) delivered the full cost data pipeline — `cost_entries` ledger, self-healing `cost_total_usd` rollups on `Task`/`Feature`/`Workflow`/`AutopilotDesign`/`AutopilotProject`, `cost_limit_usd` enforcement, and a family of `GET .../costs` REST endpoints (task/workflow/feature/design/project level). That same merge also landed a set of UI building blocks — `ProjectCostSummary`, `CostDisplay`, `FeatureCostBadge`, `DesignCostRow`, `BudgetPausedLabel` (`frontend/src/components/cost/`) — plus working budget-limit configuration in `ProjectSettingsModal.tsx` and a project-level cost summary already rendered on the Dashboard.

**This feature closes the remaining visibility gap.** Two of the five cost components built in the prior merge — `FeatureCostBadge` and `DesignCostRow` — are exported from `components/cost/index.ts` but are never imported anywhere else in the app: they were built but never wired into the screens the design calls out. Likewise, four of the five cost-fetching API client functions (`getDesignCosts`, `getFeatureCosts`, `getWorkflowCosts`, `getTaskCosts`) exist in `services/api.ts` but have zero callers outside that file. The `paused_by === 'budget'` distinction is separately (and correctly) surfaced in `WorkflowCard.tsx` via inline logic, but `BudgetPausedLabel` — the dedicated component built for that exact purpose — is unused, a duplication worth resolving while touching this code.

**Current State (from Budget Enforcement merge):**
- ✅ `GET /projects/{id}/costs`, `/designs/{id}/costs`, `/features/{id}/costs`, `/workflows/{id}/costs`, `/tasks/{id}/costs` — all five cost-summary endpoints implemented (`src/mcp/autopilot_api.py:2190-2432`)
- ✅ `apiService.getProjectCosts/getDesignCosts/getFeatureCosts/getWorkflowCosts/getTaskCosts` — all five client wrappers exist (`frontend/src/services/api.ts:842-874`)
- ✅ `ProjectSettingsModal.tsx` — budget (`cost_limit_usd`) configuration UI, fully wired to `PUT /projects/{id}`
- ✅ `Dashboard.tsx` — renders `ProjectCostSummary` using `getProjectCosts` data (project-level, already live)
- ✅ `WorkflowCard.tsx` — shows "PAUSED: BUDGET LIMIT REACHED" for `paused_by === 'budget'` (inline logic, not using `BudgetPausedLabel`)
- ✅ `frontend/src/components/cost/{CostDisplay,FeatureCostBadge,DesignCostRow,ProjectCostSummary,BudgetPausedLabel}.tsx` — all five components built and exported
- ⚠️ `FeatureCostBadge` — built, never imported outside `components/cost/`
- ⚠️ `DesignCostRow` — built, never imported outside `components/cost/`
- ⚠️ `BudgetPausedLabel` — built, never imported outside `components/cost/`; `WorkflowCard.tsx` duplicates its logic inline instead
- ❌ `PipelineStatusCard.tsx` — no cost/budget indicator (design calls for a "$current / $limit" metric alongside its existing Agents/Pending/Processed/Succeeded/Failed row)
- ❌ `DesignQueuePanel.tsx` feature rows (~line 700-870) — render `feature.name`, `feature.feature_key`, `FeatureStatusBadge`, task list, but no cost
- ❌ `GET /projects/{project_id}/designs/{filename}/status` (`autopilot_api.py:2760`, feeding `DesignQueuePanel` via `getAutopilotProjectDesignStatus`) — the `features` list it returns (`autopilot_api.py:3056-3070`) does not include `cost_total_usd`, even though `Feature.cost_total_usd` exists on the model; the frontend has no way to render a per-feature cost badge in this panel without either a backend change or N supplemental `getFeatureCosts` calls

**Target State (this feature):**
- `PipelineStatusCard.tsx` (or the design-screen surface it renders inside `Autopilot.tsx`) shows a "$current / $limit" budget indicator with a link into `ProjectSettingsModal`, matching the design's stated integration point
- `DesignQueuePanel.tsx` feature rows show `FeatureCostBadge` next to each feature's cost, once cost data is available to the row
- `WorkflowCard.tsx` uses `BudgetPausedLabel` instead of its duplicated inline label logic (or `BudgetPausedLabel` is removed if truly redundant — see Open Questions)
- The backend design-status endpoint feeding `DesignQueuePanel` carries per-feature `cost_total_usd` so the frontend doesn't need a per-row network waterfall
- No new backend cost computation, schema, or enforcement logic — this feature is UI wiring plus the one small backend field addition needed to unblock it

---

## 1. Scope Boundary (from task assignment)

### 2.1 Built-but-orphaned components

`FeatureCostBadge.tsx` and `DesignCostRow.tsx` were implemented as part of the Budget Enforcement feature (commit `b0c74e2`, "feat(ui): Add budget configuration and display components (FR-6, FR-7, FR-8)") but never connected to a live data source or rendered anywhere. They pass a basic grep for "does the component exist" but fail any real usage check — `grep -rl FeatureCostBadge frontend/src` and `grep -rl DesignCostRow frontend/src` both return only the component's own file and the barrel `index.ts`. Same for `BudgetPausedLabel.tsx`. This is dead code sitting in the tree, and the design's own UI section (lines 445-452) explicitly calls for exactly this wiring ("surfacing `cost_total_usd` on feature cards / design rows").

### 2.2 Missing project-level indicator on the pipeline status surface

The design (lines 445-452) specifies: *"Autopilot design screen (`DesignQueuePanel.tsx` or `PipelineStatusCard.tsx` — whichever already renders project-level status) : a small '$current / $limit' indicator... with a link that opens `ProjectSettingsModal`."* Neither component currently does this. `PipelineStatusCard.tsx` is the better fit — it already renders a metrics row (Agents/Pending/Processed/Succeeded/Failed, `PipelineStatusCard.tsx:99-104`) and a project name header, matching the "project-level status" description precisely.

### 2.3 Backend field gap for feature-row cost

`GET /projects/{project_id}/designs/{filename}/status` (`autopilot_api.py:2760-3070`) is the endpoint that feeds `DesignQueuePanel`'s expandable feature rows via `apiService.getAutopilotProjectDesignStatus`. Its `features` list construction (`autopilot_api.py:3056-3070`) builds each feature dict from `feat.id`, `feat.name`, `feat.feature_key`, `feat.status`, `feat.scope`, tasks, `feat.depends_on`, timestamps, and `has_report` — but omits `feat.cost_total_usd`, even though that column already exists and is self-healingly maintained on the `Feature` model. Without this field, wiring `FeatureCostBadge` into the row requires either a backend addition (cheap — one more field in an existing dict literal) or N extra `getFeatureCosts(feature.id)` calls per expanded design (a real N+1 the design's "additive to plumbing that already exists, not new plumbing" framing (line 711) argues against).

---

## 2. Functional Requirements

### FR-1: Budget Indicator on Pipeline Status Surface

**Requirement:** `PipelineStatusCard.tsx` displays project cost-so-far, and the project's budget limit if one is set, using data already available via `apiService.getProjectCosts(projectId)`.

**Acceptance Criteria:**
- When no `cost_limit_usd` is set, show `"$current spent"` (matches design wording, line 448)
- When `cost_limit_usd` is set, show `"$current / $limit"`
- Indicator is a clickable link/button that opens `ProjectSettingsModal` scoped to the active project (reuses the existing modal — no new settings surface)
- Indicator sits alongside the existing metrics row (Agents/Pending/Processed/Succeeded/Failed) rather than replacing any of them
- Uses the existing `ProjectCostSummary` or `CostDisplay` component rather than a new one-off rendering, consistent with "Touch only what you must" and the existing component library
- No new data fetch if `Dashboard.tsx`'s existing `getProjectCosts` query can be reasonably reused/shared for the active project; otherwise a scoped `useQuery` calling the existing `apiService.getProjectCosts`

### FR-2: Per-Feature Cost Badge in Design Queue Panel

**Requirement:** Each feature row in `DesignQueuePanel.tsx` (the block rendering `feature.name`/`feature.feature_key`/`FeatureStatusBadge`, `autopilot_api.py`-fed via `getAutopilotProjectDesignStatus`) shows a `FeatureCostBadge` next to the feature's status badge.

**Acceptance Criteria:**
- `FeatureCostBadge` renders using `feature.cost_total_usd` (new field, see FR-4) sourced from the design-status response — no per-row supplemental fetch
- Badge is hidden when cost is `0` or absent (matches `FeatureCostBadge`'s existing `if (cost <= 0) return null` behavior — no change needed there)
- Phase-0 pseudo-feature and placeholder rows (`id` starting with `phase0-`/`placeholder-`) either omit the badge or pass `cost_total_usd: 0` (they have no `Feature` DB row to source cost from)
- Badge placement doesn't break existing row layout/wrapping at typical viewport widths

### FR-3: Design-Level Cost Row (Deferred Judgment)

**Requirement:** Evaluate whether `DesignCostRow` has a real integration point in this feature's scope, or whether design-level cost is already adequately covered by the project-level indicator (FR-1) plus per-feature badges (FR-2) summing to the same information.

**Acceptance Criteria:**
- If `DesignQueuePanel.tsx` has a per-design summary line (collapsed/header state of each design entry, separate from its expanded feature rows) that plausibly matches `DesignCostRow`'s shape (design name + cost), wire it there
- If no such surface exists and inventing one isn't clearly asked for by the design doc, leave `DesignCostRow` unwired and flag this explicitly rather than building a new UI surface to justify using an existing component — matches "No abstractions/features beyond what was asked"
- This is a judgment call for architecture_design to resolve with a concrete look at `DesignQueuePanel`'s collapsed-row markup, not something to force in requirements

### FR-4: Feature Cost in Design-Status API Response

**Requirement:** `GET /projects/{project_id}/designs/{filename}/status` includes `cost_total_usd` on each feature dict it returns.

**Acceptance Criteria:**
- `autopilot_api.py:3056-3070` feature dict literal gains `"cost_total_usd": feat.cost_total_usd or 0.0`
- Phase-0 pseudo-feature dict (`autopilot_api.py:~3100-3109`) and the placeholder dict (`~3116-3127`) either include `"cost_total_usd": 0.0` for type consistency or the frontend treats the field as optional — pick one and apply consistently
- No change to response caching behavior, no new query — `feat` is already loaded in this loop, this is a zero-cost field addition
- Existing consumers of this endpoint (if any beyond `DesignQueuePanel`) are unaffected by the additive field

### FR-5: Resolve `BudgetPausedLabel` Duplication

**Requirement:** `WorkflowCard.tsx`'s inline `getStatusLabel` budget-paused text (`WorkflowCard.tsx:26-30`, `"PAUSED: BUDGET LIMIT REACHED"`) and the standalone `BudgetPausedLabel` component (badge-styled, `"Paused: budget limit reached"`) currently do the same job in two different ways in two different places, with only one actually wired up.

**Acceptance Criteria:**
- Either: replace `WorkflowCard.tsx`'s inline label rendering with `<BudgetPausedLabel />` where it currently renders the plain status text, or
- Determine `BudgetPausedLabel` is redundant with `WorkflowCard`'s existing status-badge system (it already has `statusColors`/`statusLabels` dictionaries driving a colored dot + text) and remove the unused component instead
- Do not ship both a used inline implementation and an unused duplicate component — pick one, this is exactly the kind of orphan cleanup CLAUDE.md calls for ("Do clean up orphans created by your own changes")
- This is the smallest-scoped item in this feature; if architecture_design judges it out of scope for a "UI" feature strictly about cost *display*, it may be deferred, but it should be an explicit decision, not silence

---

## 4. Non-Functional Requirements

### NFR-1: No New Network Waterfalls

Per the design's own framing (line 711: "this is additive to plumbing that already exists, not new plumbing"), per-feature cost must not require one HTTP request per visible feature row. FR-4 (embedding cost in the existing design-status response) exists specifically to satisfy this — a `getFeatureCosts(feature.id)` call per row in `DesignQueuePanel`'s `.map()` would be an N+1 anti-pattern the design implicitly rules out.

### NFR-2: No Behavior Change to Enforcement

This feature must not touch `cost_derivation.py`, budget-guard logic in `orchestrator.py`, or the `paused_by` semantics established by the merged Budget Enforcement feature. Those are done and tested; this feature is display-only plus the one additive API field.

### NFR-3: Visual Consistency

New/wired-up cost UI must match the existing Tailwind styling conventions already established by `CostDisplay`/`FeatureCostBadge`/`ProjectCostSummary` (color-coded by magnitude, `DollarSign` icon from `lucide-react`) rather than introducing a new visual language for cost display.

### NFR-4: Backward Compatibility

Adding `cost_total_usd` to the design-status feature dict is purely additive — no existing field is renamed or removed, so no frontend consumer of that endpoint (beyond `DesignQueuePanel`) can break from this change.

---

## 5. Technology Constraints

- **Backend**: Python 3.12, FastAPI, SQLAlchemy — matches existing stack, no new dependencies. `Feature.cost_total_usd` column already exists (`src/core/database.py:1107` per Budget Enforcement merge).
- **Frontend**: React 18, TypeScript, Tailwind CSS, `@tanstack/react-query` for data fetching, `lucide-react` for icons — all components to be wired already follow these conventions.
- No new frontend libraries needed; all required components (`FeatureCostBadge`, `DesignCostRow`, `BudgetPausedLabel`, `CostDisplay`, `ProjectCostSummary`) and API client functions already exist.

---

## 6. Open Issue: Missing scope.md

### 6.1 Files Likely to Change

- `frontend/src/components/autopilot/PipelineStatusCard.tsx` — add budget indicator (FR-1)
- `frontend/src/components/autopilot/DesignQueuePanel.tsx` — wire `FeatureCostBadge` into feature rows (FR-2), possibly `DesignCostRow` into a design-summary row (FR-3, pending architecture judgment)
- `frontend/src/components/workflow/WorkflowCard.tsx` — resolve `BudgetPausedLabel` duplication (FR-5)
- `src/mcp/autopilot_api.py` (`get_project_design_status`, ~line 3056-3127) — add `cost_total_usd` to feature dicts (FR-4)
- `frontend/src/pages/Autopilot.tsx` — only if `PipelineStatusCard` needs a new prop (e.g. `costLimit`/`costTotal`) threaded down from a query already live at the page level, rather than duplicating the query inside the card

### 6.2 Files Already Complete (No Changes Needed)

- `frontend/src/components/cost/CostDisplay.tsx`
- `frontend/src/components/cost/FeatureCostBadge.tsx`
- `frontend/src/components/cost/DesignCostRow.tsx`
- `frontend/src/components/cost/ProjectCostSummary.tsx`
- `frontend/src/components/cost/BudgetPausedLabel.tsx` (used or removed per FR-5, not modified)
- `frontend/src/components/ProjectSettingsModal.tsx`
- `frontend/src/services/api.ts` (`getProjectCosts`/`getDesignCosts`/`getFeatureCosts`/`getWorkflowCosts`/`getTaskCosts` all already implemented)
- `frontend/src/pages/Dashboard.tsx` (project-level summary already live)
- `src/core/cost_derivation.py`, `src/core/database.py` (schema/rollup — untouched by this feature)
- `src/mcp/autopilot_api.py` cost-summary endpoints (`/costs` routes, lines 2190-2432 — untouched; only the unrelated design-status endpoint at line 2760 gets the additive field)

### 6.3 Key Architectural Relationships

```
AutopilotProject.cost_total_usd / cost_limit_usd
        │
        ├─ GET /projects/{id}/costs ──► apiService.getProjectCosts ──► Dashboard.tsx (LIVE)
        │                                                          └─► PipelineStatusCard.tsx (FR-1, NEW)
        │
Feature.cost_total_usd
        │
        ├─ GET /features/{id}/costs ──► apiService.getFeatureCosts (UNUSED — not needed if FR-4 lands)
        │
        └─ GET /projects/{id}/designs/{filename}/status
                  └─ features[].cost_total_usd (FR-4, NEW FIELD)
                          └─► DesignQueuePanel.tsx feature rows ──► FeatureCostBadge (FR-2, NEW WIRING)

Workflow.paused_by == "budget"
        └─► WorkflowCard.tsx (inline label, LIVE) ──vs──► BudgetPausedLabel (FR-5, orphaned)
```

---

## 7. Acceptance Criteria Summary

- [ ] Pipeline status surface shows current spend, and limit when set, with a working link to `ProjectSettingsModal`
- [ ] `DesignQueuePanel` feature rows show cost via `FeatureCostBadge` for features with nonzero cost
- [ ] Design-status backend endpoint includes `cost_total_usd` per feature, no N+1 calls introduced
- [ ] `BudgetPausedLabel` duplication with `WorkflowCard`'s inline label resolved one way or the other, explicitly
- [ ] No changes to budget enforcement logic, schema, or `paused_by` semantics
- [ ] `DesignCostRow` usage decided explicitly (wired or deliberately deferred) rather than left silently orphaned
- [ ] `npm run type-check` passes; no new backend endpoints needed, existing `/costs` endpoints untouched

---

## 8. Critical Design Decisions

### D-1: Fix the Backend Field Gap Rather Than Add Per-Row Fetches

`DesignQueuePanel` needs feature-level cost, and the cleanest source is the endpoint it already calls (`getAutopilotProjectDesignStatus`), not the separate `getFeatureCosts` endpoint. Adding one field to an existing dict is cheaper and avoids N+1 — directly following the design doc's own "additive to plumbing that already exists" framing.

### D-2: `PipelineStatusCard` Over `DesignQueuePanel` for the Project-Level Indicator

The design doc offers either component as the target for the "$current/$limit" indicator. `PipelineStatusCard` already renders project-level aggregate metrics (Agents/Pending/Processed/etc.) with no per-design granularity, making it the closer semantic match for a single project-wide budget number. `DesignQueuePanel` is oriented around individual designs/features, which is where the per-feature badge (FR-2) belongs instead.

### D-3: This Is a UI-Wiring Feature, Not New Cost Infrastructure

Every backend piece needed for this feature already exists except one additive field. Scope creep risk is architecture inventing new cost endpoints, new rollup logic, or new schema — none of that is needed or asked for.

---

## 9. Risk Assessment

- **Low risk overall** — this is UI wiring against a stable, already-tested backend. The only backend change (FR-4) is a one-line additive field in an existing response.
- **Main risk**: scope drift into "improving" the cost components' styling/behavior beyond wiring them up, or inventing new UI surfaces (e.g., a dedicated cost dashboard page) not asked for by the design doc's UI section.
- **Secondary risk**: FR-5 (`BudgetPausedLabel` cleanup) is adjacent-but-not-strictly-cost-display; architecture_design should make an explicit in/out-of-scope call rather than let it default either way.

---

## 10. Open Questions

1. Does `DesignQueuePanel` have a collapsed/header row per design (not just per feature) where `DesignCostRow` would plausibly fit? Needs a direct look at the component during architecture_design — not resolved here to avoid inventing a UI surface speculatively.
2. Is `BudgetPausedLabel` in scope for this feature, or should it be flagged as pre-existing dead code for a separate cleanup pass? Leaning toward in-scope since it's a one-line swap in a file already visited for FR-2's neighboring badge work, but flagging for scope_review.
3. Should `PipelineStatusCard`'s budget indicator fetch its own `getProjectCosts` data, or should `Autopilot.tsx` (the page-level parent) fetch once and pass down as props, avoiding a duplicate query alongside whatever `Dashboard.tsx` already does? Architecture-level call, not a requirements-level one.

---

## 11. Non-Goals (Explicitly Deferred)

- Any new cost computation, schema, or enforcement logic (fully owned by the merged Budget Enforcement feature)
- Task-level or workflow-level cost UI (the `getTaskCosts`/`getWorkflowCosts` endpoints exist but nothing in the design doc's UI section calls for surfacing them in a screen — out of scope unless scope_review says otherwise)
- A dedicated standalone cost/spend analytics page or dashboard beyond the existing `Dashboard.tsx` summary and the two new integration points (FR-1, FR-2)
- Real-time/streaming cost updates mid-task (explicitly deferred in the design doc itself, line 650-651, as a pi-extension-only side effect, unrelated to this feature)
