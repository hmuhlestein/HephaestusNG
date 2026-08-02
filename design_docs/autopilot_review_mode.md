# Autopilot Review Mode

**Status:** Design Proposal  
**Date:** 2026-08-01  
**Scope:** Autopilot pipeline — feature-level pause-for-review with UI toggle, highlighted rows, and review modal

---

## 1. Summary

Today the autopilot pipeline runs continuously: it picks up a design, decomposes it into features, runs the 10-phase workflow for each feature, and advances automatically with no human checkpoint between features.

This design adds **Review Mode** — an opt-in per-project mode where the pipeline automatically pauses after each feature completes and waits for the user to either approve the feature (pipeline advances) or request changes (feature is re-queued with annotated feedback). The user can switch between Full Autopilot and Review Mode at any time with a large slider toggle on the Autopilot page.

---

## 2. User-Facing Behavior

### 2.1 Full Autopilot (default, existing behavior)
- Pipeline runs unattended.
- Features appear in the Design Queue as they complete, showing their status badge (validated / needs_review / failed).
- No change from today.

### 2.2 Review Mode (new)
- After each feature's 10-phase workflow reaches a terminal state (completed, validated, or failed), the orchestrator **automatically pauses the feature's workflow** with `paused_by="review"` and holds the pipeline at that feature.
- In the Design Queue panel, the paused-for-review feature row is **highlighted** (amber-gold background, left border accent, subtle pulse animation).
- An inline **"Review"** button appears on that row.
- Pressing "Review" opens the **Feature Review Modal** — a wider version of the existing FeatureDetailModal with:
  - The HTML design report rendered in an iframe (the same `feature_report.html` already generated).
  - A feedback text area for the user to write change requests.
  - Two action buttons: **"Request Changes"** and **"Approve & Continue"**.
- **Approve & Continue**: clears the review pause, marks the feature reviewed/approved, and the pipeline advances to the next feature.
- **Request Changes**: saves the feedback text into the feature row, marks the feature `needs_review`, and re-queues the feature's workflow for another iteration. Feedback is injected as an `AgentMessage` of type `review_feedback` so it enters the agent's full context window on the next cycle. The pipeline advances to the next feature immediately — re-iteration runs in the background.

### 2.3 Mode Toggle
- A large slider toggle (not a tiny checkbox) sits prominently in the header area of the Autopilot page, just below the PipelineStatusCard.
- Left position: **Full Autopilot** (dark background, rocket icon). Right position: **Review Mode** (amber/orange background, eye icon).
- The toggle writes `review_mode` to the `AutopilotProject` row via a PATCH endpoint. Switching modes takes effect for the next feature to complete — it does not interrupt features already in flight. Disabling Review Mode while features are waiting for review does **not** auto-approve them; those features stay paused until explicitly acted on.
- The toggle is disabled (greyed) if no project is selected.

---

## 3. Data Model Changes

### 3.1 `AutopilotProject` — new column

```python
review_mode = Column(Boolean, default=False, nullable=False)
```

This is a per-project preference. When `True`, every feature that reaches a terminal phase automatically enters `paused_by="review"` state.

### 3.2 `Feature` — new columns

```python
review_status = Column(
    String,
    CheckConstraint("review_status IN ('pending', 'approved', 'changes_requested')"),
    nullable=True,
    default=None,
)
review_feedback = Column(Text, nullable=True)   # user's change-request text
reviewed_at = Column(DateTime, nullable=True)
reviewed_by = Column(String(100), nullable=True, default="ui-user")
```

`review_status=None` means no review has occurred (or review mode was off). `approved` means the user signed off. `changes_requested` means the pipeline should inject feedback into the next iteration.

### 3.3 `Workflow` — existing `paused_by` column (no change to schema)

The existing `paused_by` column (VARCHAR, values: `"user"`, `"budget"`) gets a new allowed value: `"review"`. The self-heal sweep already skips workflows with `paused_by` set, so this prevents auto-resume without any schema change.

---

## 4. Backend Changes

### 4.1 Orchestrator hook — `src/autopilot/orchestrator.py`

After the per-feature workflow reaches a terminal phase, add:

```python
def _should_pause_for_review(project_id: str) -> bool:
    with get_db() as db:
        proj = db.query(AutopilotProject).filter_by(id=project_id).first()
        return bool(proj and proj.review_mode)

def _pause_feature_for_review(feature_id: str, workflow_id: str) -> None:
    """Set paused_by='review' on the workflow. The self-heal sweep won't touch it."""
    with get_db() as db:
        wf = db.query(Workflow).filter_by(id=workflow_id).first()
        if wf and wf.status not in ("paused", "cancelled", "failed"):
            wf.status = "paused"
            wf.paused_by = "review"
        feature = db.query(Feature).filter_by(id=feature_id).first()
        if feature:
            feature.status = "paused"
        db.commit()
```

Call site: the same location where `_update_design_status` is called after a feature's final phase. Only fires when `_should_pause_for_review` returns `True`.

The pipeline's main loop **blocks** on this feature (does not advance to the next feature) until either an approve or request-changes call clears the `paused_by="review"` flag.

**Why block vs. continue?** Blocking is intentional — in Review Mode, the user wants a sequential checkpoint per feature. If they wanted parallelism they would use Full Autopilot. The pipeline loop already has a configurable sleep-and-retry pattern; blocking on a `review`-paused feature fits that loop naturally (it will re-check once per sweep tick, ~30s, same as any other blocked state).

### 4.2 New API endpoints — `src/mcp/autopilot_api.py`

#### `PATCH /autopilot/projects/{project_id}/review-mode`

```
Request body: { "review_mode": bool }
Response:     { "review_mode": bool }
```

Updates `AutopilotProject.review_mode`. No other side effects — changes take effect for the next feature to complete.

#### `POST /autopilot/features/{feature_id}/review`

```
Request body:
  {
    "action": "approve" | "request_changes",
    "feedback": "string (required when action='request_changes')"
  }

Response:
  { "success": true, "next_feature_id": "feat-..." | null }
```

**Approve path:**
1. Set `feature.review_status = "approved"`, `feature.reviewed_at = datetime.utcnow()`.
2. Set `wf.status = "active"`, `wf.paused_by = None`. (Pipeline loop will now advance.)
3. If the feature was `validated` or `completed`, leave it as-is. If it was `failed`, leave it failed — approval means "I've seen it, move on", not "mark it as passed".

**Request-changes path:**
1. Set `feature.review_status = "changes_requested"`, `feature.review_feedback = feedback`, `feature.reviewed_at = datetime.utcnow()`.
2. Create an `AgentMessage` record with `type="review_feedback"` containing the feedback text. This is the established channel for mid-workflow human input and reaches the agent's full context window on the next iteration.
3. Call the existing `resume_feature` logic (reset workflow to `active`, restart failed/blocked tasks with pending status).
4. The pipeline advances past this feature immediately; re-iteration runs in the background.

### 4.3 `PipelineStatus` response — add field

```python
features_awaiting_review: int = 0   # count of features with paused_by="review"
```

Populated by the existing status endpoint. Displayed as a badge on the Design Queue tab and in PipelineStatusCard.

---

## 5. Frontend Changes

### 5.1 Review Mode Toggle — `Autopilot.tsx` + `PipelineStatusCard.tsx`

Place the toggle in a new `ReviewModeToggle` component rendered in `Autopilot.tsx` immediately below `PipelineStatusCard`. 

```
┌───────────────────────────────────────────────────────────────────┐
│  Pipeline Status Card (existing)                                   │
└───────────────────────────────────────────────────────────────────┘
┌───────────────────────────────────────────────────────────────────┐
│  ○─────────────────────── ●  Review Mode  👁                      │
│    Full Autopilot                                                   │
│  Changes take effect after the current feature completes           │
└───────────────────────────────────────────────────────────────────┘
```

**Toggle anatomy:**
- Width: ~280px. Height: ~48px. Rounded pill shape.
- Left half label: "Full Autopilot" with `Rocket` icon, slate background when selected.
- Right half label: "Review Mode" with `Eye` icon, amber-600 background when selected.
- Transition: 300ms ease spring slide with thumb that travels the full width.
- On click: fires `PATCH /autopilot/projects/{projectId}/review-mode`.
- Optimistic update with rollback on error (same pattern as `togglePipeline`).
- Disabled + 50% opacity when `projectId` is null.

**State source:** `status.review_mode` from `getAutopilotStatus` (add field to `PipelineStatus` response) **or** a separate `useQuery` against the project endpoint. Using the existing status poll (3s interval) is preferred to avoid a second polling loop.

### 5.2 Feature Row Highlight — `DesignQueuePanel.tsx`

In `SortableDesignItem`'s expanded feature list, when a feature's status is `"paused"` **and** the feature has `review_pending: true` (a new field returned by the design-status endpoint):

- Row background: `bg-amber-50 border-l-4 border-amber-400` (replaces the default `bg-white` hover).
- Left accent bar pulses gently: `animate-pulse` on the border, duration 2s.
- The existing `RowActionIcons` strip gets a new **"Review"** button (BookOpen icon, amber-600 color) that appears only on review-pending rows.

The `getAutopilotProjectDesignStatus` response already returns a `features` array. Add `review_pending: bool` to each feature entry (true when `wf.paused_by == "review"`).

### 5.3 Feature Review Modal — new component `FeatureReviewModal.tsx`

A dedicated modal component (not the existing `FeatureDetailModal`, but sharing its layout skeleton). Props:

```typescript
interface FeatureReviewModalProps {
  featureId: string | null;
  onClose: () => void;
  onDecision: (action: 'approve' | 'request_changes', feedback?: string) => void;
}
```

**Layout (full-width modal, max-w-6xl, 90vh):**

```
┌──────────────────────────────────────────────────────────────────────┐
│  Header: Feature name + status badge + "Awaiting Your Review" label  │
│          [close ×]                                                    │
├────────────────────────────────────┬─────────────────────────────────┤
│                                    │  REVIEW PANEL                   │
│   HTML REPORT (iframe)             │  ─────────────────────          │
│                                    │  Feature name                   │
│   Full feature_report.html         │  Status + cost + time metrics   │
│   rendered at 100% width of        │  ─────────────────────          │
│   this pane.                       │  [Feedback textarea]            │
│                                    │  Placeholder: "Describe what    │
│   Left pane: ~65% width            │  needs to change, or leave      │
│                                    │  blank to approve..."           │
│                                    │  ─────────────────────          │
│                                    │  [Approve & Continue ✓]         │
│                                    │  [Request Changes →]            │
└────────────────────────────────────┴─────────────────────────────────┘
```

- Left pane: `<iframe src="/api/autopilot/feature-records/{featureId}/report" />` — the existing report endpoint, unchanged.
- Right pane: metadata summary (same fields as FeatureCard), then a `<textarea>` for feedback (max 2000 chars, `resize-y`).
- **"Approve & Continue"**: green button, disabled while mutation is in-flight. Calls `POST /autopilot/features/{featureId}/review` with `action="approve"`. On success: close modal, invalidate design-status queries, show toast "Feature approved — pipeline advancing".
- **"Request Changes"**: amber button, disabled if feedback textarea is empty. Calls `POST /autopilot/features/{featureId}/review` with `action="request_changes"` and feedback text. On success: close modal, invalidate queries, show toast "Changes requested — feature queued for revision".
- Both buttons show a spinner while the mutation is pending.

**Opening the modal:** the "Review" button in the feature row calls `setReviewFeatureId(featureId)` in `DesignQueuePanel` (same pattern as `setSelectedFeature`). The modal is rendered at the bottom of `DesignQueuePanel` or lifted to `Autopilot.tsx`.

### 5.4 Badge on Design Queue tab

`PipelineStatus.features_awaiting_review > 0` causes a new amber pulsing dot to appear next to the "Design Queue" tab label, distinct from the existing blue queue-depth badge. This draws attention when the user is on another tab and a review is waiting.

---

## 6. Orchestrator Loop Change (detail)

Each design runs its own pipeline loop independently. The per-design loop already iterates over features; the change adds a review gate after each feature's **deploy phase** (the final phase — after the report is generated and the feature is fully archived).

**Before (simplified):**
```python
async def run_design(design, project_id, stop_event):
    for feature in design.features:
        await run_feature_workflow(feature)   # all phases including deploy
    mark_design_complete(design)
```

**After:**
```python
async def run_design(design, project_id, stop_event):
    for feature in design.features:
        await run_feature_workflow(feature)   # all phases including deploy
        if _should_pause_for_review(project_id):
            _pause_feature_for_review(feature.id, feature.workflow_id)
            await _wait_for_review_clearance(feature.id, stop_event)
    mark_design_complete(design)
```

`_wait_for_review_clearance` polls `wf.paused_by` every 30s using the loop's existing `asyncio.sleep(30)` heartbeat. It returns as soon as `paused_by` is no longer `"review"` (i.e., the user pressed Approve or Request Changes). It also returns immediately if `stop_event` fires, so Stop/restart work cleanly without leaving orphaned review-waits.

**Concurrency across designs:** when `max_concurrent_projects > 1` or multiple designs run in parallel, each design's `run_design` coroutine waits on its own features independently. Design A pausing on feature 2 does not block Design B's feature 3 — they are separate coroutines with separate `_wait_for_review_clearance` calls. The per-design review queue accumulates independently; the user can approve features from different designs in any order.

---

## 7. Self-Heal Sweep Safety

`orchestrator.py`'s `_try_auto_resume_paused_workflow` already skips any workflow where `paused_by IS NOT NULL`. Since we set `paused_by = "review"`, review-paused workflows are immune to the self-heal sweep with no additional code — the same invariant that protects user-paused workflows protects review-paused ones.

---

## 8. Scope and Non-Goals

**In scope:**
- Per-project review_mode toggle (DB + API + UI)
- Post-feature pause in orchestrator
- Highlighted feature row in Design Queue
- Feature Review Modal with iframe report + feedback + approve/request-changes
- Feedback injection into next iteration via TaskPromptOverride / AgentMessage
- `features_awaiting_review` counter in PipelineStatus

**Out of scope (future):**
- Per-design or per-phase review checkpoints (this design is feature-granularity only)
- Email/webhook notification when a review is waiting
- Review history / audit log (only the last review decision is stored per feature)
- Multi-reviewer or team review workflows
- Diff view comparing feature iterations (show what changed between the original and the revised run)

---

## 9. File Inventory

| File | Change |
|---|---|
| `src/core/database.py` | Add `AutopilotProject.review_mode`, `Feature.review_status`, `Feature.review_feedback`, `Feature.reviewed_at`, `Feature.reviewed_by` |
| `src/autopilot/orchestrator.py` | Add `_should_pause_for_review`, `_pause_feature_for_review`, `_wait_for_review_clearance`; call after per-feature workflow completes |
| `src/mcp/autopilot_api.py` | Add `PATCH /projects/{id}/review-mode`, `POST /features/{id}/review`; add `review_pending` to design-status feature entries; add `features_awaiting_review` to `PipelineStatus` |
| `frontend/src/components/autopilot/ReviewModeToggle.tsx` | New component — large slider pill toggle |
| `frontend/src/components/autopilot/FeatureReviewModal.tsx` | New component — split-pane report iframe + feedback + action buttons |
| `frontend/src/components/autopilot/DesignQueuePanel.tsx` | Add review-pending row highlight + "Review" button; open FeatureReviewModal |
| `frontend/src/pages/Autopilot.tsx` | Render ReviewModeToggle below PipelineStatusCard; thread review modal state |
| `frontend/src/services/api.ts` | Add `patchProjectReviewMode`, `postFeatureReview` methods |

---

## 10. Decisions

1. **Feedback injection channel** — **AgentMessage**. Feedback is stored as an `AgentMessage` record of type `review_feedback` so it reaches the agent's full context window on the next iteration. `TaskPromptOverride` was considered but only patches a single task's prompt; AgentMessage is the established channel for mid-workflow human input and is already plumbed end-to-end.

2. **What if the user never reviews?** — The pipeline **waits indefinitely**. There is no timeout and no auto-approve. The feature row stays highlighted, the pipeline stays paused on that feature, and the user must take an explicit action (Approve or Request Changes) before work continues. This is intentional: Review Mode is an explicit commitment to human sign-off. If the user wants unattended operation they should switch back to Full Autopilot.

3. **Concurrent designs and review mode** — Each design's features are independent. When multiple designs are active concurrently, each design's pipeline pauses after **its own** features complete — it does not wait for other designs. Design A pausing on feature 2 does not block Design B from finishing feature 3. The per-design review queue accumulates independently; the user can approve them in any order.

4. **Where the pipeline pauses** — After the **deploy phase** (the final phase). The full report is already generated and available in the iframe. The feature is in its terminal state before the pause fires; approval simply unblocks the queue and advances to the next feature. No re-execution happens on approve — only a request-changes decision triggers a new iteration.
