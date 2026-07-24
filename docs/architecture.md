# Architecture: Cost Tracking UI

**Feature ID:** des-91c8-cost-ui
**Status:** Architecture Complete
**Date:** 2026-07-24
**Requirements:** `docs/requirements_analysis.md` (5 FRs, PASSED scope review — `docs/scope_review/scope_review_result.json`)

---

## 1. Summary

This feature wires four already-built cost components into three screens and adds one additive
backend field. No new components, endpoints, schema, or cost computation. All work is either a
prop addition to an existing component, a new (small) local usage of an existing component, or a
one-line dict-literal addition in an existing endpoint handler.

Five tasks below, one per FR, in dependency order (backend field before the frontend rows that
consume it).

---

## 2. System Architecture

No new services, processes, or data stores. This is UI wiring against the existing Budget
Enforcement data pipeline (merged, untouched):

```
AutopilotProject.cost_total_usd / cost_limit_usd  (existing column, self-healing rollup)
        │
        └─ GET /projects/{id}/costs  (existing endpoint, unmodified)
                 │
                 ├─► apiService.getProjectCosts  (existing, unmodified)
                 │        │
                 │        ├─► Dashboard.tsx  (LIVE, unmodified)
                 │        └─► Autopilot.tsx  (NEW query, same queryFn/pattern as Dashboard.tsx)
                 │                 └─► PipelineStatusCard.tsx  (NEW props: costTotal, costLimit, onBudgetClick)
                 │                          └─► CostDisplay  (existing component, reused as-is)
                 │
Feature.cost_total_usd  (existing column, self-healing rollup)
        │
        └─ GET /projects/{id}/designs/{filename}/status  (existing endpoint — MODIFIED, additive field only)
                 │  autopilot_api.py: get_project_design_status
                 │  features[].cost_total_usd            ◄── NEW FIELD (FR-4)
                 │  cost_total_usd (design-level sum)     ◄── NEW FIELD (derived, FR-3)
                 │
                 └─► DesignQueuePanel.tsx
                          ├─► FeatureRow (expanded)   ──► FeatureCostBadge  (existing component, NEW wiring, FR-2)
                          └─► SortableDesignItem (collapsed header) ──► CostDisplay  (existing component, NEW wiring, FR-3)

Workflow.paused_by == "budget"  (existing, unmodified)
        └─► WorkflowCard.tsx  (existing inline statusColors/statusLabels system — KEPT; BudgetPausedLabel REMOVED, FR-5)
```

---

## 3. Component Interfaces

### 3.1 `PipelineStatusCard.tsx` — new props (FR-1)

```ts
interface PipelineStatusCardProps {
  // ...existing props unchanged...
  costTotal?: number;
  costLimit?: number | null;
  onBudgetClick?: () => void;
}
```

Rendering: inside the existing "Right: Metrics" flex group (`PipelineStatusCard.tsx:97-133`), add
one more item using `CostDisplay` (`frontend/src/components/cost/CostDisplay.tsx`, unmodified),
wrapped in a `<button onClick={onBudgetClick}>` matching the existing per-metric button style
(`PipelineStatusCard.tsx:106-121`) so it visually matches Agents/Pending/Processed/etc. rather than
introducing a new visual language. `CostDisplay` already renders `"$current"` alone when
`costLimit` is `undefined`/`null` and `"$current / $limit"` when set — satisfies FR-1's two display
states with zero new formatting logic. Card renders nothing extra when `costTotal` is `undefined`
(project not yet loaded) — omit the button, don't render a `$0.00` placeholder.

### 3.2 `Autopilot.tsx` — new query + modal instance (FR-1)

```ts
const { data: projectCosts } = useQuery({
  queryKey: ['project-costs', projectId],
  queryFn: () => apiService.getProjectCosts(projectId!),
  refetchInterval: 30000,
  enabled: !!projectId,
});
const [showProjectSettings, setShowProjectSettings] = useState(false);
```

This duplicates Dashboard.tsx's existing `project-costs` query verbatim (same queryKey, same
queryFn, same interval) rather than lifting shared state into a context. React Query dedupes by
queryKey across mounted components, so when Dashboard and Autopilot are both mounted (they aren't —
they're separate routes) or on remount, this is a cache hit, not a duplicate network call in
practice — and even in the worst case it's one extra lightweight `/costs` GET on route entry, not
a waterfall. Introducing a shared cost context/provider to avoid this would be a bigger structural
change than a UI-wiring feature justifies (see requirements doc §11 Non-Goals).

`ProjectSettingsModal` (`frontend/src/components/ProjectSettingsModal.tsx`) takes only
`{ isOpen, onClose }` — it is not project-scoped by prop (it lists/manages all projects
internally). `Layout.tsx` already owns one instance with its own local `showProjectSettings` state
for the sidebar settings icon; that state is private to `Layout` and not exposed via context, so
`Autopilot.tsx` gets its **own independent instance** of the same component with its own local
`showProjectSettings` state, wired to `PipelineStatusCard`'s `onBudgetClick`. Two independent modal
instances (Layout's, Autopilot's) is fine — only one renders at a time since each is gated by its
own boolean, and `ProjectSettingsModal` has no shared mutable state beyond React Query's cache
(which is safely shared/deduped already).

### 3.3 `DesignQueuePanel.tsx` — `FeatureRow` (FR-2)

Feature row right-side badge cluster (`DesignQueuePanel.tsx:850-858`, immediately before
`FeatureStatusBadge`):

```tsx
<FeatureCostBadge cost={feature.cost_total_usd ?? 0} />
<FeatureStatusBadge status={feature.status} />
```

`FeatureCostBadge` already no-ops on `cost <= 0` internally (`FeatureCostBadge.tsx:17`) — no extra
guard needed at the call site. Phase-0 pseudo-feature and placeholder entries get
`cost_total_usd: 0.0` from the backend (§4), so `?? 0` is a defensive fallback only, not the
primary mechanism.

### 3.4 `DesignQueuePanel.tsx` — `SortableDesignItem` collapsed header (FR-3)

**Decision: wire a cost total into the collapsed per-design header using `CostDisplay`, not
`DesignCostRow`.**

`DesignCostRow`'s shape (`designId`, `designName`, `costTotal` — a standalone name+cost list row,
`DesignCostRow.tsx:16-34`) is built for a *list of designs*, not for embedding inside a header that
already renders the design name prominently in an `<h4>` (`DesignQueuePanel.tsx:631-636`) alongside
a drag handle, filename, size, timestamp, status badge, and action icons. Forcing `DesignCostRow` in
there would duplicate the design name and doesn't fit the existing dense single-row flex layout.
`CostDisplay` (the primitive `DesignCostRow` itself wraps internally) is the right piece to reuse
directly: small, inline, no name duplication.

Placement: in the collapsed header's action cluster (`DesignQueuePanel.tsx:654-657`, next to
`StatusBadge`):

```tsx
{costTotal > 0 && <CostDisplay currentCost={costTotal} showProgress={false} className="text-xs" />}
{status && status !== 'pending' && <StatusBadge status={status} />}
```

Data source: the top-level `designStatuses` React Query (`DesignQueuePanel.tsx:60-85`) already
calls `getAutopilotProjectDesignStatus` for **every** design (not just expanded ones) every 10s to
populate status badges — it already fetches the response but discards everything except
status/workflowId/error. Extend the returned map to also carry the new design-level sum:

```ts
statuses[d.filename] = {
  status: status.status || 'pending',
  workflowId: status.workflows?.[0]?.id,
  error: status.error || null,
  costTotal: status.cost_total_usd ?? 0,  // NEW — from the response's design-level sum, §4
};
```

Then thread `designStatuses[item.filename]?.costTotal` down as a new `costTotal` prop on
`SortableDesignItem`. **Zero new network calls** — this reuses data the component already fetches
for every design, satisfying NFR-1 the same way FR-2/FR-4 do.

`DesignCostRow` remains **unwired** after this feature. It is not deleted (requirements doc §6.2
lists it as "no changes needed", and speculative deletion of a working, tested component beyond
what FR-5 explicitly calls out for `BudgetPausedLabel` is scope creep). Flag it for a follow-up
cleanup pass if a genuine "list of designs with cost" surface (e.g., a cross-project cost view) is
ever built — no such surface exists today and inventing one is out of scope (requirements doc §11
Non-Goals).

### 3.5 `WorkflowCard.tsx` — remove `BudgetPausedLabel` duplication (FR-5)

**Decision: keep the existing inline `getStatusLabel`/`statusColors` system, delete
`BudgetPausedLabel.tsx`.**

`WorkflowCard.tsx:11-32` already has a unified badge system (`statusColors`/`statusLabels` driving
one consistently-styled pill, `WorkflowCard.tsx:135-137`) that correctly special-cases the
budget-paused label (`getStatusLabel`, lines 27-31). Swapping in `BudgetPausedLabel` — a
differently-styled standalone badge (red bg + `AlertCircle` icon vs. the plain colored pill used
for every other status) — for only the budget-paused case would make that one status visually
inconsistent with `active`/`completed`/`failed`/etc., which all share one pill style. That's a
regression in visual consistency (NFR-3) to eliminate an unused component, not an improvement.

Changes:
- Delete `frontend/src/components/cost/BudgetPausedLabel.tsx`
- Remove its export from `frontend/src/components/cost/index.ts`
- Remove the dead `BudgetPausedLabel` import from `Dashboard.tsx:14` (confirmed by scope review:
  imported, never rendered)

No change to `WorkflowCard.tsx` itself — its existing logic already does the right thing.

---

## 4. Backend Change: `autopilot_api.py::get_project_design_status`

**File:** `src/mcp/autopilot_api.py`, function starting line 2761.

### 4.1 Per-feature field (FR-4)

Real-feature dict (line 3056-3070):
```python
features.append(
    {
        "id": feat.id,
        "name": feat.name,
        "feature_key": feat.feature_key,
        "workflow_id": feat.workflow_id,
        "status": feat_status,
        "scope": feat.scope or "",
        "tasks": feat_tasks,
        "depends_on": feat.depends_on or [],
        "created_at": feat.created_at.isoformat() if feat.created_at else None,
        "completed_at": feat.completed_at.isoformat() if feat.completed_at else None,
        "has_report": has_report,
        "cost_total_usd": feat.cost_total_usd or 0.0,   # NEW
    }
)
```

Phase-0 pseudo-feature dict (line 3097-3110) and placeholder dict (line 3116-3127): both add
`"cost_total_usd": 0.0` for type consistency across all three dict shapes returned in `features`
(neither has a backing `Feature` row to source a real cost from).

### 4.2 Design-level total (FR-3 data source)

At the return statement (line 3149-3170), add one derived field — no new query, `features` is
already fully built in memory at this point:

```python
return {
    "filename": filename,
    "name": design_name,
    ...
    "cost_total_usd": sum(f["cost_total_usd"] for f in features),   # NEW
    "features": features,
}
```

This is what §3.4 reads via `status.cost_total_usd` for the collapsed-header total.

### 4.3 No other backend changes

`cost_derivation.py`, `orchestrator.py` budget-guard logic, and the `/costs` endpoints
(lines 2190-2432) are untouched, per NFR-2.

---

## 5. Data Flow

1. **Project-level (FR-1):** `Autopilot.tsx` mounts → fires `getProjectCosts(projectId)` (30s
   poll, same pattern as `Dashboard.tsx`) → passes `costTotal`/`costLimit` to `PipelineStatusCard`
   → user clicks the new budget metric button → `onBudgetClick` opens Autopilot's local
   `ProjectSettingsModal` instance.
2. **Feature-level (FR-2, FR-4):** `DesignQueuePanel`'s per-design `fetchFeatures()` (fires on
   expand + 10s poll while expanded, `DesignQueuePanel.tsx:533-551`) now receives
   `features[].cost_total_usd` from the modified endpoint → `FeatureRow` renders
   `FeatureCostBadge` directly from that field, no extra fetch.
3. **Design-level (FR-3):** `DesignQueuePanel`'s existing all-designs `designStatuses` query
   (`DesignQueuePanel.tsx:60-85`, already calls the same endpoint per design every 10s regardless
   of expand state) now also captures the response's new `cost_total_usd` sum →
   `SortableDesignItem` renders it via `CostDisplay` in the collapsed header, no extra fetch.
4. **Workflow-level (FR-5):** No data flow change — `WorkflowCard` already reads
   `execution.paused_by` and `execution.status` from its existing props.

---

## 6. Infrastructure Requirements

None. No new env vars, no new dependencies, no migration (the `cost_total_usd` columns this
feature reads already exist and are already maintained by the merged Budget Enforcement feature).

---

## 7. Task Breakdown

Five tasks, matching the five FRs. Frontend tasks that consume the new backend field (T3, T4)
block on the backend task (T1) landing first; the rest are independent of each other.

### T1 — Backend: add `cost_total_usd` to design-status response (FR-4, §4)
**Blocks:** T3, T4
**Files:** `src/mcp/autopilot_api.py`
**Acceptance criteria:**
- Real-feature dict includes `"cost_total_usd": feat.cost_total_usd or 0.0`
- Phase-0 pseudo-feature dict and placeholder dict both include `"cost_total_usd": 0.0`
- Top-level response includes `"cost_total_usd": sum(f["cost_total_usd"] for f in features)`
- No new DB query added — `feat` and `features` are already loaded/built at these points
- Existing endpoint tests (if any cover this function) still pass; response is purely additive,
  no field renamed or removed

### T2 — Frontend: budget indicator on `PipelineStatusCard` (FR-1, §3.1–3.2)
**Blocks:** none
**Files:** `frontend/src/components/autopilot/PipelineStatusCard.tsx`, `frontend/src/pages/Autopilot.tsx`
**Acceptance criteria:**
- `PipelineStatusCard` accepts `costTotal?`, `costLimit?`, `onBudgetClick?` props
- When `costTotal` is defined, renders a `CostDisplay`-based button in the existing metrics row
  showing `"$current"` (no limit set) or `"$current / $limit"` (limit set), styled consistent with
  the other metric buttons (`hover:bg-white/15` etc.)
- When `costTotal` is `undefined`, the budget metric is omitted entirely (no `$0.00` flash)
- Clicking the budget metric calls `onBudgetClick`
- `Autopilot.tsx` adds a `project-costs` query (same shape as `Dashboard.tsx`'s), a local
  `showProjectSettings` boolean, and its own `<ProjectSettingsModal>` instance wired to
  `onBudgetClick`/`onClose`
- No changes to `Dashboard.tsx`'s existing cost query or `ProjectCostSummary` usage

### T3 — Frontend: `FeatureCostBadge` in `DesignQueuePanel` feature rows (FR-2, §3.3)
**Blocks on:** T1
**Files:** `frontend/src/components/autopilot/DesignQueuePanel.tsx`
**Acceptance criteria:**
- `FeatureRow` renders `<FeatureCostBadge cost={feature.cost_total_usd ?? 0} />` immediately before
  `FeatureStatusBadge` in the row's right-side badge cluster
- Badge is invisible (renders nothing) for zero/absent cost, per `FeatureCostBadge`'s own existing
  `cost <= 0` guard — no new guard logic added at the call site
- Row layout/wrapping is unaffected at typical viewport widths (visual check, not a new test)
- No new network call introduced — reads `feature.cost_total_usd` from data already fetched by
  `fetchFeatures()`

### T4 — Frontend: design-level cost in collapsed header (FR-3, §3.4)
**Blocks on:** T1
**Files:** `frontend/src/components/autopilot/DesignQueuePanel.tsx`
**Acceptance criteria:**
- `designStatuses` query's per-design result map gains a `costTotal` field sourced from the
  response's new top-level `cost_total_usd`
- `SortableDesignItem` receives `costTotal` as a prop and renders it via
  `<CostDisplay currentCost={costTotal} showProgress={false} className="text-xs" />` next to
  `StatusBadge` in the collapsed header, only when `costTotal > 0`
- No new network call — reuses the existing per-design status fetch that already runs for every
  design (expanded or not)
- `DesignCostRow` is explicitly left unwired (not deleted, not force-fit) — this is a deliberate
  decision, not silence, matching requirements FR-3's acceptance criteria

### T5 — Frontend: resolve `BudgetPausedLabel` duplication (FR-5, §3.5)
**Blocks:** none
**Files:** `frontend/src/components/cost/BudgetPausedLabel.tsx` (deleted),
`frontend/src/components/cost/index.ts`, `frontend/src/pages/Dashboard.tsx`
**Acceptance criteria:**
- `BudgetPausedLabel.tsx` deleted
- Its export removed from `components/cost/index.ts`
- Its dead import removed from `Dashboard.tsx`
- `WorkflowCard.tsx` unchanged — its existing `getStatusLabel`/`statusColors` system already
  correctly renders the budget-paused case and is kept as the single source of truth for workflow
  status styling
- `npm run type-check` passes (no dangling references to the deleted component/export)

---

## 8. Cross-Cutting Acceptance Criteria

- `npm run type-check` passes after all five tasks land
- No changes to `cost_derivation.py`, `orchestrator.py` budget-guard logic, or `/costs` endpoints
  (NFR-2)
- No per-row or per-design supplemental network calls introduced anywhere (NFR-1) — verified by
  T1 being the sole backend change and T3/T4 both reading fields from data already fetched
- All new/wired cost UI uses `CostDisplay`/`FeatureCostBadge` as-is, no new visual styling (NFR-3)
- `cost_total_usd` additions to the design-status response are purely additive; no existing field
  renamed/removed (NFR-4)

---

## 9. Explicit Non-Goals (carried from requirements doc §11)

- No new cost computation, schema, or enforcement logic
- No task-level or workflow-level cost UI
- No standalone cost/spend analytics page
- No real-time/streaming cost updates
- No deletion of `DesignCostRow` (unlike `BudgetPausedLabel`, it isn't asked to be removed by any
  FR — it's simply left unwired, an explicit and documented decision per §3.4)
