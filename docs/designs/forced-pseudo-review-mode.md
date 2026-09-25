# Forced Pseudo-Review Mode (branch-protection review gate in full autopilot)

## Status
Proposed.

## Problem

A full-autopilot project (`AutopilotProject.review_mode = 0`) that targets a
**branch-protected `main`** cannot complete the `git_expert` phase. Branch
protection can require an approving human review (CODEOWNERS /
`reviewDecision = REVIEW_REQUIRED`) and refuse direct pushes to `main`, so the
only merge path is `gh pr merge`, and that merge is **BLOCKED** until a human
approves. No autopilot agent can supply that approval.

Today the `git_expert` done-gate
(`verify_git_expert_merged_and_pushed`, `src/services/task_completion/verification.py`)
has only two branches:

- **review_mode ON:** checks the feature branch is clean + pushed + the PR is
  healthy; the merge-to-main is deliberately deferred to a human via the
  dashboard's yellow **Review** button. Never requires merge-into-main.
- **review_mode OFF (full autopilot):** requires the feature commit to be an
  ancestor of local **or** remote `main` (the recent `gh pr merge --admin`
  fix), else rejects with *"this branch's work is not yet merged into main."*

There is no branch for "full autopilot, but `main` is branch-protected and the
PR is green and BLOCKED purely on a required human review." The gate takes the
review_mode-OFF path, the PR can never merge without a human, and the task
**fails on every retry until the budget is exhausted** — the exact loop
observed live on feature `user-id-composition` (task `3d2445b0`, PR #1238:
`state=OPEN`, `mergeable=MERGEABLE`, all CI green, `mergeStateStatus=BLOCKED`,
`reviewDecision=REVIEW_REQUIRED`). One retry even hallucinated review-mode and
bailed via the goal's escape clause.

Two things are wrong:

1. **No graceful degrade.** When branch protection forces a human into the
   loop, full autopilot should behave like review mode *for the merge step
   only* — surface the Review button, pause the merge for a human — instead of
   failing the task.
2. **The pipeline stalls unnecessarily.** The human gate is only on merging to
   `main`. The feature's *code* already exists, complete and pushed, on its
   feature branch. Downstream features do not need `main`; they need that
   code. They should keep going by branching from the completed feature's
   branch tip, while the PR awaits human approval in parallel.

## Goals

- G1. When a full-autopilot `git_expert` completion is blocked solely on a
  required human review (green CI, no code defect, no conflict), **pause for
  human review instead of failing** — reusing the existing review pause
  (`Workflow.paused_by = "review"`) so the existing yellow Review button
  appears with no new UI.
- G2. **Do not stall the pipeline.** Later features proceed on top of the
  review-stuck feature's branch tip via a **cumulative integration branch**,
  so completed-but-unmerged work is never lost and dependents are not skipped.
- G3. Distinguish "blocked on human review" from "checks failing" from
  "genuinely not merged / not pushed" — only the first triggers pseudo-review;
  the others keep their current reject/needs-work behavior.
- G4. No new operator setting. Pseudo-review is auto-detected from PR state; it
  is not the `review_mode` toggle.

## Non-goals

- Not changing real `review_mode` behavior.
- Not auto-merging or `--admin`-bypassing a protected PR. Honoring branch
  protection is the entire point.
- Not adding a second review UI. Pseudo-review reuses the `paused_by="review"`
  pause and the existing Review button/modal verbatim.
- Not chaining across `depends_on` edges only — per the decision below,
  chaining is a single linear integration branch across the whole design.

## Key decisions

- **D1. Detection, not configuration.** Pseudo-review fires when, in the
  review_mode-OFF branch of the done-gate, the work is not on `main` AND
  `get_pr_status` reports the PR is OPEN, CI `passing`, no `failing_checks`,
  and (`merge_state == "BLOCKED"` OR `review_decision == "REVIEW_REQUIRED"`).
  `get_pr_status` already returns both `review_decision` and `merge_state`
  (`src/services/github_pr_status.py`) — no new gh fields needed.
- **D2. Reuse the review pause.** On detection, pause via the canonical writer
  `_pause_feature_for_review` (`Workflow.paused_by="review"`, `Feature.status="paused"`,
  `Feature.pr_url` set). The frontend Review button already keys on the
  backend-derived `feature.review_pending` (= `Workflow.paused_by=="review"`),
  so it appears automatically. Human approval flows through the existing
  `POST /features/{id}/review` → `gh pr merge --auto` path unchanged.
- **D3. Linear cumulative integration branch (answers the chaining question).**
  When ANY feature in a design enters pseudo-review, the design switches to a
  cumulative model: each subsequent feature's worktree branches from the
  **most recently completed feature's branch tip**, not `main` — regardless of
  declared `depends_on`. This guarantees all completed-but-unmerged work is
  visible downstream. Independent (`depends_on: []`, parallel) features would
  otherwise branch from `main` and silently miss the stuck feature's code;
  the linear chain prevents that. Trade-off accepted: parallel features become
  effectively sequential once pseudo-review is active for a design (they must
  chain to observe prior unmerged work). This only activates when a PR is
  actually review-stuck; a design whose PRs all merge cleanly keeps today's
  independent-from-main behavior.
- **D4. Dependents of a paused feature no longer skip.** `_run_one_feature`'s
  dependency gate currently treats a `paused` dependency as not-`completed`
  and returns `SKIPPED`. Under an active pseudo-review design, a `paused`
  (review-stuck) dependency counts as satisfied for chaining purposes, because
  its code is on its branch and the dependent will branch from it.

## Design

### Detection (done-gate, verification.py)

In `verify_git_expert_merged_and_pushed`, the review_mode-OFF branch, at the
point where it has computed `merged_local`/`merged_remote` and is about to
`_reject("not yet merged into main")`:

```
if not merged_local and not merged_remote:
    pr = get_pr_status(repo.active_branch.name, cwd=wf.working_directory)
    if _is_blocked_on_human_review(pr):
        # green CI, no failing checks, OPEN, BLOCKED/REVIEW_REQUIRED
        _enter_pseudo_review(session, task, wf, pr)   # pause, don't fail
        return { "status": "pending", "message": "...awaiting human review..." }
    return _reject("this branch's work is not yet merged into main. ...")
```

`_is_blocked_on_human_review(pr)` is true iff:
`pr is not None and pr.state == "OPEN" and pr.ci_conclusion == "passing"
and not pr.failing_checks and (pr.merge_state == "BLOCKED" or
pr.review_decision == "REVIEW_REQUIRED")`.

Note `get_pr_status` currently does NOT surface `REVIEW_REQUIRED` through
`needs_work` (only `CHANGES_REQUESTED`), and `BLOCKED` is not in
`_AGENT_FIXABLE_MERGE_STATES` — both correct: a required-reviewer block is not
agent-fixable. We read the raw fields directly here.

`_enter_pseudo_review` sets `Feature.pr_url`, then calls
`_pause_feature_for_review(feature_id, logger)` (the canonical pause writer).
The task is parked exactly like the review_mode `is_pending` path
(`task.status="in_progress"`, `task.assigned_agent_id=None`) so
`_clean_stale_assigned_tasks` doesn't re-fail it and no fresh agent is spun up.

### Human resolution (unchanged)

The existing yellow Review button appears (its gate `feature.review_pending`
is already `Workflow.paused_by=="review"`). Approve → existing
`POST /features/{id}/review` → `resume_workflow` + `gh pr merge --auto`. This
path already exists and is not modified.

### Cumulative integration branch (chaining)

**State flag.** A design is "in pseudo-review" once any of its features has a
`paused`/review-stuck workflow with a `pr_url`. Rather than a new column,
derive it: `any(Feature.status=="paused" and Feature.pr_url for features of design)`.
(If derivation proves too chatty, a `AutopilotDesign.pseudo_review_active`
boolean is the fallback — deferred until measured.)

**Base selection.** `_resolve_base_commit` gains an optional explicit base:

```
def _resolve_base_commit(self, *, use_current_branch=False,
                         base_ref: Optional[str] = None) -> str:
```

`base_ref` (a branch name or SHA) wins when provided; it is `rev-parse
--verify`d and returned, falling back to the base-branch path if it can't
resolve. `use_current_branch` and the main+fetch default are unchanged.

**Chaining decision** lives in `run_feature_pipelines` (which knows ordering)
and is threaded into `_run_one_feature` → `_create_integration_worktree`:

- When the design is in pseudo-review, before launching feature N, resolve the
  **predecessor tip** = the branch of the most-recently-completed feature in
  the design's execution order (`feature/<design[:8]>/<prev_feature_key>` —
  branch names are deterministic, reconstructable from `feature_key`, so no new
  column is needed). Pass it as `base_ref` to `_create_integration_worktree`.
- The first feature (no predecessor) still branches from `main`.
- Because the branch base is frozen at first creation
  (`_create_integration_worktree` swallows the "branch exists" error on
  resume), the chain is stable across restarts.

**Dependency gate (D4).** In `_run_one_feature`, when the design is in
pseudo-review, a dependency whose status is `paused`-with-`pr_url` counts as
satisfied (its code is on its branch, which is in this feature's base chain),
instead of returning `SKIPPED`.

### The merge target stays `main`

Every feature branch's eventual PR still targets `main` (branch base choice
never changes the merge target — see `_resolve_base_commit` docstring). When
the human approves the PRs (in order), each merges to `main` normally. The
cumulative branches mean later PRs will show as already containing earlier
features' commits until those merge — acceptable, since the human approves in
dependency order and GitHub collapses already-merged commits.

## Edge cases

- **PR later goes red / CHANGES_REQUESTED after entering pseudo-review.** The
  existing `_resolve_pending_pr_status` sweep (`pr_resolution.py`) already
  re-checks parked git_expert PRs and marks the task `failed` (→ retry/
  arbitration → back to development) on `needs_work`. Pseudo-review parks the
  task in the same `in_progress`+null-agent state that sweep watches, so a PR
  that regresses is caught and routed to development — no special-casing.
- **PR merges out of band (human merges before sweep).** Same as review mode
  today: the approve path / `auto_merge_sync` reconciles local main; the design
  advances.
- **A pseudo-review feature is a dependency of a later feature that then also
  gets review-stuck.** Both sit paused; the chain still holds (feature C
  branches from B's tip which branches from A's tip). Human approves A→B→C.
- **Design has exactly one feature.** Enters pseudo-review, pauses, no chaining
  needed (no successors). Deploy waits behind human approval.
- **`get_pr_status` returns None** (gh down/transient). Per its contract, None
  is "unknown/pending, never a rejection" — do NOT enter pseudo-review on None;
  fall through to the existing reject (the retry will re-probe). Avoids parking
  a task for review on a transient gh blip.
- **Non-branch-protected repo that merged fine.** `merged_local`/`merged_remote`
  is true, the whole block is skipped — pseudo-review only reached when not
  merged.

## Alternatives considered

- **Chain only along `depends_on` edges.** Rejected (D3): independent/parallel
  features declare no dependency, would branch from `main`, and silently miss
  the review-stuck feature's code. A linear chain is the only model that
  guarantees no completed work is invisible downstream.
- **Auto-merge with `--admin` bypass.** Rejected: defeats the branch protection
  the user explicitly wants honored.
- **A new operator toggle for pseudo-review.** Rejected (G4): the condition is
  a fact about the PR (branch-protected + review-required), auto-detectable; a
  toggle would just be another thing to forget to set.

## Affected code

- `src/services/task_completion/verification.py` — `verify_git_expert_merged_and_pushed`:
  add the blocked-on-review detection + `_enter_pseudo_review` in the
  review_mode-OFF branch (the current `get_pr_status` call only exists in the
  review_mode-ON branch and must be added here).
- `src/services/github_pr_status.py` — no change (fields already exposed);
  possibly a small helper `is_blocked_on_human_review` property on `PRStatus`
  for reuse.
- `src/autopilot/orchestrator/pipeline.py` — `run_feature_pipelines` /
  `_run_one_feature`: pseudo-review detection for the design, predecessor-tip
  resolution, pass `base_ref` into `_create_integration_worktree`, and the D4
  dependency-gate relaxation.
- `src/autopilot/orchestrator/worktree_integration.py` —
  `_create_integration_worktree`: accept and forward an explicit `base_ref`.
- `src/core/worktree_manager.py` — `_resolve_base_commit`: add `base_ref` param.
- `src/autopilot/orchestrator/pipeline.py` — `_pause_feature_for_review` reused
  as-is (no change) by the new pseudo-review entry point.
- Frontend: **no change** — the Review button already keys on
  `feature.review_pending` (`Workflow.paused_by=="review"`).

## PR link at the top of the feature report

Related requirement: the feature report HTML should show a link to the PR at
the top. This matters most in pseudo-review mode, where the open PR *is* the
pending deliverable a human needs to reach.

**Why not the doc_review prompt.** `feature_report.html` is authored by the
doc_review agent (phase 12), but the PR is not created until git_expert
(phase 14). At report-authoring time the PR URL does not exist, so instructing
doc_review to embed it cannot work for the normal flow.

**Decision: serve-time injection, keyed on `Feature.pr_url`.** The report file
is served by `feature_record_routes.py` endpoints
(`get_workflow_feature_report`, `get_feature_record_report`,
`get_feature_report`, and the `/docs/{doc_name}` JSON path). Rather than
mutate the stored HTML, inject a PR banner at serve time:

- Resolve the PR URL from `Feature.pr_url` (populated by the git_expert
  done-gate), falling back to `_extract_pr_url` (which derives it from
  git_expert `key_learnings`) when the column is unset. Both already exist.
- If a PR URL is found, prepend a small banner immediately after `<body>`
  (e.g. `<div class="heph-pr-banner"><a href="{url}">View Pull Request →</a></div>`,
  with a review-pending note when the owning feature/workflow is
  `paused_by="review"`). If no URL, inject nothing (report unchanged).
- Centralize the injection in one helper (`_with_pr_banner(html, pr_url,
  review_pending) -> str`) called by every endpoint that returns the report
  HTML, so all serving paths agree — the same "one resolver, all callers"
  discipline `_resolve_live_feature_report` already established.

**Why serve-time, not write-time:** single source of truth (`Feature.pr_url`),
always current, no timing coupling to phase order, works identically for the
live-worktree copy and the archived-gallery copy, and it cannot be silently
dropped by an agent that ignores a prompt instruction. The stored artifact
stays exactly as doc_review wrote it.

Affected code (additive to the list above):
`src/mcp/autopilot/feature_record_routes.py` (the injection helper + its use in
each HTML-serving endpoint); no change to the doc_review prompt or the report
template.

## Testing

- Unit: `_is_blocked_on_human_review` truth table across `state`/`ci_conclusion`/
  `failing_checks`/`merge_state`/`review_decision`, incl. `None`.
- Unit: `_resolve_base_commit(base_ref=...)` resolves an explicit branch and
  falls back correctly on an unresolvable ref.
- Integration: a git_expert done-gate call with a mocked `get_pr_status`
  returning OPEN/passing/BLOCKED parks the task (`in_progress`, null agent) and
  pauses the workflow (`paused_by="review"`) instead of failing.
- Integration: with a design in pseudo-review, feature N's worktree is created
  from feature N-1's branch tip (assert the base commit), and a `paused`
  dependency no longer yields `SKIPPED`.
- Regression: a genuinely-not-merged PR (no PR, or red CI) still rejects; a
  cleanly-merged feature still passes; real `review_mode` is unchanged.
- The existing forensics/`test_forensics_gating.py`, git_expert, and
  task-completion suites must stay green.
