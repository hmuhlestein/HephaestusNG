# Adversarial Review — Cost Tracking UI

Scope: the diff introduced by this workflow's development phase, i.e.
`git diff $(git merge-base main HEAD) HEAD` (13 files, 721 insertions /
1701 deletions). This is the "Cost Tracking UI" slice that wires
pre-existing cost-derivation/backend infrastructure (built in an earlier,
separate feature) into the Autopilot dashboard: `DesignQueuePanel.tsx`,
`PipelineStatusCard.tsx`, `Autopilot.tsx`, `Dashboard.tsx`, and a small
addition to `src/mcp/autopilot_api.py`'s `get_project_design_status`.

Findings below also reference pre-existing files (`ProjectCostSummary.tsx`,
`ProjectSettingsModal.tsx`, cost API endpoints) where this phase's new
integration points expose gaps in them — those files weren't touched by
this diff, but the wiring done here is what makes the gap user-reachable.

---

## BLOCKERS

### B1. Dashboard's "Set budget limit" button is a silent no-op

**File:** `frontend/src/pages/Dashboard.tsx:282-288`, `frontend/src/components/cost/ProjectCostSummary.tsx:75-82`

`ProjectCostSummary` renders a "Set budget limit" button (and a gear icon)
that only appear/act `onClick={onConfigureBudget}`. `Dashboard.tsx` renders
the component without ever passing `onConfigureBudget`:

```tsx
<ProjectCostSummary
  projectId={projectCosts.project_id}
  projectName={projectCosts.project_name}
  costTotal={projectCosts.cost_total_usd}
  costLimit={projectCosts.cost_limit_usd}
  isOverBudget={projectCosts.is_over_budget}
/>
```

**Failure sequence:** a project has no `cost_limit_usd` set → Dashboard
shows the cost card with a "Set budget limit" button → user clicks it →
`onClick={undefined}` → nothing happens, no navigation, no modal, no error.
The gear/settings icon (line 40-48 of `ProjectCostSummary.tsx`) doesn't even
render in this state since it's gated on the same missing prop. There is no
way to configure a budget from the Dashboard at all; the user has to already
know to go to Autopilot → the pipeline status bar → the new "Budget" button
added in this phase → `ProjectSettingsModal`.

**Fix:** pass `onConfigureBudget={() => setShowProjectSettings(true)}` from
`Dashboard.tsx` (mirroring what `Autopilot.tsx` now does for
`PipelineStatusCard`), rendering `ProjectSettingsModal` from `Dashboard.tsx`
the same way.

### B2. Budget-triggered pause is indistinguishable from a user pause on the exact screen this phase built

**File:** `src/mcp/autopilot_api.py:3159-3167` (workflow list in
`get_project_design_status`), `frontend/src/components/autopilot/DesignQueuePanel.tsx:413-422` (`StatusBadge`)

`design.md` (lines ~446-453) explicitly requires: *"When a workflow shows
`paused_by == 'budget'` specifically (vs. `'user'`), surface that
distinction in whatever status text/badge already exists for paused
workflows — 'Paused: budget limit reached' reads very differently from a
generic 'Paused.'"*

The `Workflow` model has `paused_by` and `status_reason` columns (set by
`_check_budget_enforcement` / `_pause_project_workflows` in
`cost_derivation.py`), and a *different* page (`WorkflowCard.tsx:28-29`,
not touched by this phase) already reads `paused_by === 'budget'` to render
`"PAUSED: BUDGET LIMIT REACHED"`. But `get_project_design_status` — the
endpoint `DesignQueuePanel.tsx` and `PipelineStatusCard.tsx` poll every 10s
— only emits `{ id, status, created_at, error }` per workflow (lines
3159-3167); `paused_by` and `status_reason` are never included. `StatusBadge`
(line 413) takes a bare `status` string keyed into a static
`STATUS_CONFIG` map, with no way to differentiate why a `"paused"` status
happened.

**Failure sequence:** project goes over budget → `_pause_project_workflows`
sets `status="paused"`, `paused_by="budget"`, `status_reason="Budget limit
reached"` on the active workflow → user watching the Autopilot design queue
(the screen this phase's cost UI work targeted) sees a generic "Paused"
badge — identical to what they'd see if they'd clicked pause themselves —
with the new Budget button showing the number but nothing tying the pause
event to it. This is the core "obvious path to notice you're over budget"
requirement from the design doc, and it's the one requirement in the UI
section not implemented in this phase's code.

**Fix:** add `paused_by` and `status_reason` to each workflow dict at
`autopilot_api.py:3159-3167`, thread them through
`designStatuses[...]` in `DesignQueuePanel.tsx`, and have `StatusBadge`
(or a wrapper) render "Paused: budget limit reached" when
`paused_by === 'budget'`, same text `WorkflowCard.tsx` already uses.

---

## WARNINGS

### W1. Cost-fetch failures are silently reported as "$0 spent"

**File:** `frontend/src/components/autopilot/DesignQueuePanel.tsx:66-79`

```tsx
try {
  const status = await apiService.getAutopilotProjectDesignStatus(projectId, d.filename);
  statuses[d.filename] = { ..., costTotal: status.cost_total_usd ?? 0 };
} catch {
  statuses[d.filename] = { status: 'pending', costTotal: 0 };
}
```

The bare `catch {}` predates this phase (existing "M-5 fix" pattern for
`status`), but this phase piggybacks `costTotal` onto it without
distinguishing "design genuinely costs $0" from "the status call 401'd /
timed out / 500'd." Any transient failure (auth hiccup, backend restart,
network blip) makes every design in the queue display `$0.00` for that
poll cycle, which is exactly the kind of number a user checks before
deciding whether to let the pipeline keep running. Nothing is logged
(no `console.error`), so there's no way to notice why the number is wrong.

**Fix:** at minimum `console.error` the caught error, and consider
propagating a per-design `costUnavailable` flag so `CostDisplay` can show
"—" instead of "$0.00" on fetch failure.

### W2. FeatureCostBadge's "expensive" threshold is a hardcoded constant, not tied to the project's budget

**File:** `frontend/src/components/cost/FeatureCostBadge.tsx:28`

```tsx
cost >= 5 ? 'bg-red-100 text-red-800' : 'bg-blue-100 text-blue-800'
```

A $6 feature on a project with a $1000 limit renders red ("expensive"); a
$4 feature on a project with a $5 limit (about to blow the whole budget)
renders blue ("normal"). The badge's only signal is an absolute dollar
figure disconnected from the actual budget context that this phase's own
`CostDisplay`/`ProjectCostSummary` components use (`costLimit`-relative
percentage). The low-level presentational component is making a judgment
call ("is this cost concerning?") that only the caller — which knows the
project's budget — can actually answer correctly: a composition smell as
well as a correctness one.

**Fix:** either drop the color-coding (just show the dollar figure) or pass
`costLimit`/percent-of-budget into the badge so the threshold is relative,
not an arbitrary absolute constant.

---

## NITS

### N1. `DesignCostRow` is dead code

**File:** `frontend/src/components/cost/DesignCostRow.tsx`, exported at `frontend/src/components/cost/index.ts:3`

Implemented and exported, but grep shows no import of it anywhere in
`frontend/src/` outside its own definition. Either wire it in (design.md
doesn't call for a per-design cost breakdown row explicitly, so it may be
intentionally unused scaffolding) or delete it.

### N2. `CostDisplay` divides by a possibly-zero `costLimit`

**File:** `frontend/src/components/cost/CostDisplay.tsx:23`

```tsx
const progressPercent = costLimit != null ? Math.min((currentCost / costLimit) * 100, 100) : null;
```

`ProjectSettingsModal.tsx` allows saving `cost_limit_usd = 0` (`min="0"`,
only rejects `< 0`). With `costLimit = 0`, `currentCost / 0` is `Infinity`
(or `NaN` if `currentCost` is also `0`), producing an invalid `width:
NaN%`/`Infinity%` inline style. Harmless visually (browsers ignore invalid
CSS values), but it's a real code path producing `NaN`, and a `$0` budget
is a legitimate way for someone to try to hard-pause a project immediately.
