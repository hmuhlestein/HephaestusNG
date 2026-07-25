# Architectural Review: Cost Tracking UI

**Feature ID:** des-91c8-cost-ui
**Date:** 2026-07-24
**Reviewer:** Architect (same session that authored `docs/architecture.md`)
**Commit reviewed:** `2f8fcb3` (development phase, diff against `f209b42`)

---

## Summary

Implementation matches `docs/architecture.md` exactly across all five tasks (T1–T5). No
architecture violations, no scope creep, no unauthorized abstractions. All decisions
requiring judgment calls (FR-3's `DesignCostRow` placement, FR-5's `BudgetPausedLabel`
resolution) were implemented exactly as the architecture doc specified, including the
reasoning captured there. `npm run type-check` (`npx tsc --noEmit`) reports 6 pre-existing
errors, none introduced or touched by this diff (verified against `HEAD~1`). Backend tests
pass, including the new `test_design_status_includes_cost_total` regression test.

**0 BLOCKER, 0 FIX, 1 DEFER.**

---

## Task-by-task verification

### T1 — Backend: `cost_total_usd` in design-status response (FR-4) — compliant

`src/mcp/autopilot_api.py`:
- Real-feature dict (line 3069): `"cost_total_usd": feat.cost_total_usd or 0.0` — matches
  architecture §4.1 exactly.
- Phase-0 pseudo-feature dict (line 3110) and placeholder dict (line 3128): both add
  `"cost_total_usd": 0.0` — matches.
- Top-level response (line 3173): `"cost_total_usd": sum(f["cost_total_usd"] for f in features)`
  — matches architecture §4.2 exactly, no new query, computed from the already-built `features`
  list.
- No changes to `cost_derivation.py`, `orchestrator.py`, or the `/costs` endpoints — NFR-2
  respected.
- New test `test_design_status_includes_cost_total` (tests/test_autopilot_api.py:1513-1566)
  asserts both `features[0]["cost_total_usd"]` and the top-level sum. Passes.

### T2 — Frontend: budget indicator on `PipelineStatusCard` (FR-1) — compliant

`PipelineStatusCard.tsx`: new props `costTotal?`, `costLimit?`, `onBudgetClick?` match the
architecture's interface exactly (§3.1). The budget metric is rendered as a button inside the
existing metrics row, styled with the same `hover:bg-white/15` classes as the other per-metric
buttons, gated on `costTotal !== undefined` (no `$0.00` flash before data loads) — matches
architecture's stated rendering rule precisely. Uses `CostDisplay` unmodified.

`Autopilot.tsx`: adds a `project-costs` query with identical shape (queryKey, queryFn,
`refetchInterval: 30000`) to Dashboard.tsx's existing query, plus its own local
`showProjectSettings` state and `<ProjectSettingsModal>` instance — matches architecture §3.2's
explicit call not to lift shared state into a context, and its rationale (React Query
deduping by queryKey makes the "duplicate query" concern a non-issue in practice).

### T3 — Frontend: `FeatureCostBadge` in `DesignQueuePanel` feature rows (FR-2) — compliant

`FeatureRow` (`DesignQueuePanel.tsx:866`) renders `<FeatureCostBadge cost={feature.cost_total_usd ?? 0} />`
immediately before `FeatureStatusBadge`, exactly the call site and prop expression the
architecture specified (§3.3) — no redundant guard added at the call site, relying on the
component's own `cost <= 0` no-op as intended. No new network call — reads from data already
fetched by `fetchFeatures()`.

### T4 — Frontend: design-level cost in collapsed header (FR-3) — compliant, judgment call implemented as specified

This was the one FR requiring an architectural judgment call (`DesignCostRow` vs. a smaller
primitive), and the implementation matches it precisely:
- `designStatuses` query's per-design map gains `costTotal: status.cost_total_usd ?? 0`
  (`DesignQueuePanel.tsx:74`), including the catch-branch fallback (`{ status: 'pending', costTotal: 0 }`,
  line 78) — architecture didn't explicitly spec the catch branch but this is the correct
  defensive completion of the type, not a deviation.
- `SortableDesignItem` receives `costTotal` and renders
  `<CostDisplay currentCost={costTotal} showProgress={false} className="text-xs" />` next to
  `StatusBadge` (line 659-661), gated on `costTotal > 0` — matches §3.4 exactly.
- `DesignCostRow` is untouched, unwired, not deleted — matches the architecture's explicit
  decision to leave it for a future cleanup pass rather than force-fit it or delete it
  speculatively.
- Zero new network calls — confirmed the `designStatuses` query already fetched
  `getAutopilotProjectDesignStatus` per design before this feature; only the discarded fields
  changed.

### T5 — Frontend: resolve `BudgetPausedLabel` duplication (FR-5) — compliant, judgment call implemented as specified

The architecture's decision was to delete `BudgetPausedLabel` (not wire it into `WorkflowCard`)
because `WorkflowCard`'s existing unified `statusColors`/`statusLabels` pill system already
renders the budget-paused case correctly, and swapping in a differently-styled standalone badge
for just that one status would fragment the badge system's visual consistency. Implementation:
- `BudgetPausedLabel.tsx` deleted.
- Export removed from `components/cost/index.ts`.
- Dead import removed from `Dashboard.tsx` (was imported, never rendered).
- `WorkflowCard.tsx` untouched, as specified — its `getStatusLabel`/`statusColors` logic already
  did the right thing and needed no change.

---

## Cross-cutting checks

- **NFR-1 (no new network waterfalls):** verified. T3 and T4 both read fields from responses
  their host queries already fetched before this feature; no per-row or per-design supplemental
  fetch was added anywhere.
- **NFR-2 (no enforcement logic changes):** verified. `cost_derivation.py` and
  `orchestrator.py`'s budget-guard logic are absent from the diff entirely.
- **NFR-3 (visual consistency):** verified. All new/wired UI reuses `CostDisplay` /
  `FeatureCostBadge` unmodified; `PipelineStatusCard`'s new budget button matches the existing
  per-metric button styling; `WorkflowCard`'s badge system was left as the single source of
  truth rather than fragmented.
- **NFR-4 (backward compatibility):** verified. All backend changes are additive dict-literal
  fields; no field renamed or removed from `get_project_design_status`'s response.
- **Component boundaries:** no component's public interface was changed beyond additive optional
  props (`PipelineStatusCard`, `SortableDesignItem`'s internal `costTotal` prop). No component
  was given new responsibilities beyond rendering already-computed cost data.
- **Type-check:** `npx tsc --noEmit` reports 6 errors, all pre-existing on `HEAD~1` (confirmed by
  diffing against the pre-development commit) — none in files or lines this feature's diff
  touches except `Dashboard.tsx`'s unused `DollarSign` import, which was already unused before
  this feature removed `BudgetPausedLabel` from the same import line; the feature did not
  introduce this warning.
- **Backend tests:** `pytest tests/test_autopilot_api.py -k "cost_total or design_status"` — 3
  passed, including the new coverage for FR-4.

---

## Findings

### DEFER

**D-1: `BudgetStatusCard.tsx` is a second, unrelated piece of dead cost-UI code, pre-dating this
feature, not addressed by it.**
`frontend/src/components/BudgetStatusCard.tsx` (introduced in the original Budget Enforcement
merge, commit `b0c74e2`) has zero references anywhere in `frontend/src` — it's exactly the kind
of orphan `FeatureCostBadge`/`DesignCostRow`/`BudgetPausedLabel` were before this feature, except
it wasn't named in `docs/requirements_analysis.md` or `docs/architecture.md`, so wiring or
removing it was correctly out of scope for this feature (scope review only traced FR-1 through
FR-5, none of which mention this component). Flagging for a future cleanup pass, not a defect in
this implementation.

No BLOCKER or FIX findings — the implementation is a faithful, minimal execution of the
architecture with no deviations requiring rework.
