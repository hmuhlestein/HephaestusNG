# Phase 2, §4.8 — pause-state primitive findings

Implemented directly (not via the usual prompt-doc handoff to a separate
agent) at the user's explicit request to "implement 4.8 yourself carefully."

## Freshness check result

All four historical bug commits named in the plan (`9aa2a19`, `ce0c4a7`,
`bacaf6b`, `22178b1`) and the related auto-resume fix (`a333616`) are
**already fixed and intact** in the current code, at their original call
sites (now relocated post-decomposition: `src/autopilot/orchestrator.py` →
`src/autopilot/orchestrator/{engine_client,phase_transitions,__init__}.py`).
Verified by reading each site directly and confirming the fix's diff is
still present, not by trusting the plan's stale line numbers.

This meant the item's value wasn't "find and fix the four named bugs" (they
were already fixed) — it was building the shared primitive the plan
describes and auditing every other pause-write site for the *same bug
class*, since none of the four historical fixes built a shared primitive;
each patched exactly one call site. That audit found eleven more live
instances of the same two bug classes, detailed below.

**Note on process:** the first audit pass (before this doc's initial
version) found six of these. Asked to re-read this item's own prompt doc
and check for gaps, a second pass against the same `grep`/`git show`
methodology the prompt doc specified found five more the first pass
missed — `_pause_for_manual_handoff` and `_maybe_retry_failed_tasks`'s
exhausted-retry pause (both in `phase_transitions.py`, both already
correct, just never migrated to the primitive), plus three more resume-side
stale-`paused_at` sites (`state.py`'s project-reactivation resume,
`feature_routes.py`'s two review-approve handlers, and `server.py`'s
`restart_task_endpoint`, the last a genuinely new bug — see below). The
first pass's own freshness-check grep had actually surfaced all of these in
its raw output; they just weren't individually visited and cross-checked
against what got migrated. Listed together below rather than as a
separate section, since they're the same two bug classes as the original
six.

## What was built

`pause_workflow(workflow_id, *, reason, cascade_to_feature=True,
status_reason=None, session=None)` and `resume_workflow(workflow_id, *,
force=False, cascade_to_feature=True, session=None)` in
`src/autopilot/orchestrator/engine_client.py`, sibling to `terminate_agent`
(§4.2's equivalent primitive) and inserted in the same file. `pause_workflow`
always sets `status`/`paused_by`/`paused_at` together and, when
`cascade_to_feature` (default True), sets `status="paused"` on every linked
`Feature`. `resume_workflow` clears all four fields (`status`, `paused_by`,
`paused_at`, `status_reason`) together and narrows on `paused_by` per
`a333616`: `None`/`"system"` resume without `force`; `"user"`, `"budget"`,
`"review"`, `"system-exhausted"` require `force=True`. `pause_workflow_direct`
is now a thin wrapper: `pause_workflow(workflow_id, reason="user")`.

## New bugs found (beyond the four named commits) — same bug class, different sites

The audit covered every `paused_by`/`paused_at`/`status = "paused"` write
site in `src/` (17 sites across 9 files). Six had gaps the four historical
fixes never touched:

1. **`src/mcp/server.py`'s `stop_workflow`** (the actual Pause-button
   backend handler) — set `status`/`paused_by="user"` but never
   `paused_at`, and never cascaded to `Feature.status`. This is the live
   Pause button; the most consequential of the six.
2. **`src/mcp/autopilot/feature_routes.py`'s `pause_feature`** — same
   missing-`paused_at` gap (Feature.status was already handled correctly
   here).
3. **`_pause_feature_for_review`** (`src/autopilot/orchestrator/__init__.py`)
   — missing `paused_at`, despite `ce0c4a7`'s fix (the approved-feature
   skip check) being intact.
4. **`_pause_phase0_for_review`** (same file) — same missing-`paused_at` gap.
5. **`src/mcp/autopilot/queue_routes.py`'s requeue path** (`/queue/requeue`)
   — set `status="paused"` with **no `paused_by` at all**. This is worse
   than the missing-`paused_at` gap: `_try_auto_resume_paused_workflow`'s
   guard treats "no `paused_by`" the same as a `"system"` pause (both
   eligible for auto-resume), so a requeue-pause could be silently
   reverted within one sweep tick (~20s) the moment a done task appears in
   the workflow's in-progress phase — no test previously existed for this
   endpoint at all.
6. **`queue_routes.py`'s rerun path** (`/queue/rerun`) — same
   missing-`paused_by` gap, but lower practical risk: the paused rows are
   deleted moments later in the same request's "clean slate" step, well
   before a sweep tick could fire.

All six are now migrated to `pause_workflow`, closing the gap by
construction. Sites 3 and 4 use `cascade_to_feature=False` since they
already own their own feature-status write (3) or have no Feature row to
touch (4, per its own docstring).

Two more pause sites, found on re-audit, were already fully correct (all
four fields set) but had simply never been migrated to the primitive —
consolidated for consistency, not because they were buggy:

7. **`_pause_for_manual_handoff`** (phase_transitions.py) — the
   review-mode manual-git-handoff pause. Already set all four fields.
8. **`_maybe_retry_failed_tasks`'s exhausted-retry-cap pause**
   (phase_transitions.py) — `paused_by="system"` when a phase's failed
   tasks are all past the retry cap. Already set all four fields. This is
   the literal site `a333616`'s commit message calls "the exhausted-retry-
   cap give-up" — worth locating even though it needed no fix.

## Resume-side asymmetry also found and fixed

Three resume sites cleared `status`/`paused_by`/`status_reason` but left
`paused_at` stale — directly contradicting the column's own docstring
("Cleared whenever the workflow leaves 'paused', by any path"):
`_try_auto_resume_paused_workflow` (phase_transitions.py), `resume_feature`
(feature_routes.py), and the `/api/workflow-executions/{id}/resume`
endpoint (server.py). All three now route through `resume_workflow`, which
clears all four fields together. `resume_feature`'s pre-existing "failed"
workflow branch (not a pause state) was left as a direct write — routing a
non-paused status through a pause-focused primitive isn't a fit.

Four more resume-side gaps found on re-audit, same stale-`paused_at` bug
class:

9. **`_get_or_create_project_id`** (`src/autopilot/orchestrator/state.py`)
   — resumed a project's user-paused workflows via a bulk
   `Workflow.query(...).update({...})` that set `status`/`paused_by` but
   never `paused_at`, and never touched `Feature.status` at all (so a
   feature paused by `stop_workflow`'s new cascade, above, would stay
   stuck showing "Paused" even after its project reactivated and its
   workflow resumed). Rewritten as a per-row loop calling `resume_workflow`
   (force=True, default cascade) — the only real behavior change in this
   item beyond field-consistency, and a necessary one given the new
   pause-side cascade this item introduces at `stop_workflow`.
10. **`_review_phase0_decomposition`'s approve branch** (feature_routes.py)
    — same stale-`paused_at` gap on Phase 0 review approval.
11. **`review_feature`'s approve branch** (feature_routes.py) — same gap
    on a real feature's review approval. Kept its own explicit
    `feature.status = "active"` write (`cascade_to_feature=False`).
12. **`restart_task_endpoint`** (`src/mcp/server.py`) — a genuinely new
    bug, not just a stale-`paused_at` gap: `if wf.status != "active":
    wf.status = "active"` fired unconditionally, including when
    `wf.status == "paused"`. A task can be `"blocked"` — exactly what
    `pause_feature` sets on a paused workflow's in-flight tasks — and
    `restart_task_endpoint` accepts `"blocked"` as a restartable status
    with no pause-awareness guard anywhere above it. Restarting a single
    blocked task inside a paused feature would flip the *workflow's*
    `status` to `"active"` while leaving `paused_by`/`paused_at` still
    set — an inconsistent status="active"-but-flagged-paused row, worse
    than the other sites' mere staleness. Fixed by branching: `status ==
    "paused"` routes through `resume_workflow(force=True)`; anything else
    (e.g. reopening a `"completed"` workflow, the site's original intent)
    keeps the direct write.

## Sites deliberately left unmigrated

- **`_retry_exhausted_paused_workflows`** (phase_transitions.py) — its
  `"system"`→`"active"` and `"system-exhausted"`→`"active"` recovery
  branches also write `paused_retry_count` and reset task retry state as
  part of the same operation; tightly coupled, bespoke business logic
  beyond the pause/resume tetrad. Left as direct writes rather than forced
  through `resume_workflow`.
- **`project_routes.py`'s budget-limit-cleared resume** (line ~605) —
  migrated to `resume_workflow(..., cascade_to_feature=False, ...)`,
  preserving its exact current behavior of never touching `Feature.status`.
  Whether it *should* cascade (so features don't stay stuck showing
  "Paused" after a project-wide budget clear) is a product question, not
  decided here — pre-existing behavior, not something this item was asked
  to fix.

## Verification

`tests/test_pause_workflow_primitive.py` — 24 tests: the primitive's own
contract (triad-together, cascade on/off, status_reason, missing-workflow,
narrowing, force override), one characterization test per historical bug
commit (9aa2a19, ce0c4a7×2, bacaf6b×2, 22178b1, a333616), plus three added
during the re-audit for sites 9, 10, 11 above (project-reactivation resume,
both review-approve handlers). `tests/test_server_dispatch_endpoints.py`
gained one more for site 12 (`restart_task_endpoint`), reusing that file's
own dispatch-mocking fixtures.

Full targeted run across every file touched, plus every existing test file
that exercises a migrated call site (`test_orchestrator_helpers.py`,
`test_advance_phases.py`, `test_review_mode.py`, `test_budget_enforcement.py`,
`test_budget_enforcement_integration.py`, `test_autopilot_api.py`,
`test_autopilot_api_helpers.py`, `test_cost_tracking.py`, `test_monitor.py`'s
`TestDetectCreditExhausted`/`TestSessionLimitPause`/`TestDetectConnectionErrors`,
`test_agent_manager.py::TestCreateAgentForTaskSessionLimitPause`,
`test_cleanup_worktree_paused_workflow.py`,
`test_cleanup_stale_branches_race.py`, `test_phase0_idempotency.py`,
`test_resume_interrupted_workflows.py`, `test_phase_advancement_sweep.py`,
`test_autopilot_service.py`, `test_queue_service.py`): 802 passed, 5 failed
— all 5 confirmed pre-existing and unrelated via `git stash` against
unmodified `main` (3× `TestIsDesignFullyComplete`, 1×
`test_all_63_routes_survived_the_split`, 1×
`test_monitor.py::TestAutoRestartResetsTask::test_resets_task_before_terminating_agent`
— all fail identically with none of this item's changes applied).

One real regression was caught and fixed during this verification pass:
the first `pause_project_workflows` rewrite passed `status_reason=None`
through `pause_workflow` unconditionally for non-budget pauses, but
`pause_workflow`'s `status_reason=None` means "leave whatever's there
alone" (so callers that don't care about it don't clobber a legitimate
existing reason) — not "clear it." That silently broke
`test_user_pause_clears_stale_budget_reason` (a stale "Budget limit
reached" message survived a user pause). Fixed by writing
`status_reason`'s clear-or-set logic directly in `pause_project_workflows`
after the primitive call, since that mapping (`paused_by == "budget"` →
specific message, otherwise clear) is call-site-specific business logic,
not something the shared primitive's semantics should special-case.

ruff clean on every touched file (verified against `git show HEAD~1`
equivalents — a few pre-existing import-sort/naming findings unrelated to
this diff, unchanged in count). `py_compile` clean on all touched files.

No stored-embedding-style migration concern applies here — this item
doesn't change stored data shape, only future write behavior.

## Out of scope, not touched

- Anything from §4.1–§4.7.
- Any other Phase 2 item (§4.9 onward).
- The FastAPI `stop_workflow`/`resume_workflow` server.py endpoints and the
  `queue_routes.py` requeue/rerun endpoints have no dedicated
  request-level test coverage (TestClient-based) either before or after
  this change — the underlying primitive and every other call site that
  reaches it are covered; adding endpoint-level integration tests for
  these four routes would be a reasonable follow-up but wasn't built here
  given no existing test scaffolding for them.

No commits — left in the working tree for review.
