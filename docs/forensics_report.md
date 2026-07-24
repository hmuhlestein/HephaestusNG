# Forensics Report: Budget Enforcement and Pipeline Throttling

**Date:** 2026-07-23
**Workflow ID:** 0acbf2fc-fcf5-4b24-ad2d-31b1db62df6d
**Feature:** Budget Enforcement and Pipeline Throttling (feature/des-91c8/budget-enforcement)
**Parent Design:** Cost Tracking Design (DES-91c8), run `20260722_035441_COST_TRACKING_DESIGN`
**Pipeline Status:** Completed through doc_review — one functional gap (FR-7) shipped unresolved

## Data quality caveat

No `phase_prompts/` directory or workflow-scoped `pipeline_metrics.json` exists for this run — both are keyed to the parent multi-feature design session, not this sub-feature's workflow_id. All iteration counts below are reconstructed from `git log` on this branch and the phase artifacts under `docs/`. The existing `docs/adversarial_review/` and `docs/architectural_review/` reports are themselves capped-summary stubs (workflow.yaml's `max_review_runs: 4`), so per-run detail was reconstructed from commit messages rather than those files.

## 1. Commit history → phase/iteration mapping

1. `d6ecd4a` product_requirements, `dca8d5b` scope_review — clean, 1 run each.
2. `932134a` development run 1 → `93f6127` architectural_review (CONDITIONAL_PASS) → `92b90af` adversarial_review run 1: 2 BLOCKERs (stop endpoint not refactored; race condition in `_run_one_feature`'s budget guard).
3. `bbe52e7` development run 2 (fixes) → `9e8c332` adversarial run 2: 2 low findings remain → `69a95809` security_review run 1: PASS.
4. QA churned repeatedly the same day (`2710962` → … → `046daec`) over a pass-rate formatting bug (`b3d1166`, decimal vs. percentage) plus a SEC-04 ticket check each pass.
5. `044f7bc` adversarial run 3: claims BLOCKER-2 fixed (later shown wrong).
6. `c05c75b` product_validation run 1: CONDITIONAL PASS, flags 3 frontend gaps (G-1/G-2/G-3, including FR-7).
7. `b0c74e2` dev adds UI components claiming to close FR-6/7/8 → architectural runs 4-5 PASS → `075a718` adversarial run 4: BLOCKER-2 still present (prior "fixed" claims were wrong) → `3d87c8e` real fix → `93f7401`/`196b68b` adversarial run 5: PASS, 0 blockers.
8. `09e20d1` security re-verify → `d7f3f26` development: root-causes a false QA failure (libtmux pytest plugin crashing bare `pytest`, reported as pass_rate=0%).
9. Two days later: `418b812`, `555004f`, `93bddde` — three consecutive development-phase commits, all "no code changes made this pass," re-verifying an already-complete state.
10. `9194baa` product_validation run 2: re-confirms FR-7/G-2 still unmet (CONDITIONAL_PASS again) → `dc3b2cc` doc_review: PASS, notes the gap remains open.

Security review's capped-notice claims "19 runs" but only ~6 `phase(security_review)` commits exist on this branch — the counter almost certainly reads from the shared parent design directory (265 tmux logs across sibling features, only 41 mention budget-enforcement) rather than this workflow_id. Treat that number as unreliable.

## 2. Per-phase summary

| Phase | Runs (this branch) | Verdict progression | Notes |
|---|---|---|---|
| product_requirements | 1 | PASS | clean |
| scope_review | 1 | PASS | clean |
| architecture_design | 1 | PASS | clean |
| development | 2 substantive + 1 root-cause fix + 3 no-op re-dispatches | NEEDS_WORK → PASS | see Issue 1 |
| architectural_review | 5 | CONDITIONAL_PASS → PASS | converged on same DB-session issue adversarial review took longer to close |
| adversarial_review | 5 | 2 BLOCKERs → false "fixed" (run 2/3) → real fix (run 4) → clean (run 5) | see Issue 3 |
| security_review | ~6 | PASS (SEC-04 fixed via pydantic validator; 5 pre-existing lows ticketed out of scope) | capped-notice run count unreliable, see Issue 5 |
| qa_validation | churned same-day | 84/84 pass at final state | churn from tooling/formatting, not real bugs |
| product_validation | 2 | CONDITIONAL_PASS both times | FR-7 never fixed, shipped unresolved — see Issue 2 |
| doc_review | 1 | PASS | correctly declined to paper over the open FR-7 gap |

## 3. Issues and proposed prompt rewrites

### Issue 1 — Development phase re-dispatched 3x for zero-diff work

**Evidence:** `418b812` (21:17), `555004f` (21:22), `93bddde` (21:51) on 2026-07-23, each stating no code changes were needed — a repeat dispatch of an already-finished task, most likely an orchestrator goto/cycle-scoping bug rather than a prompt issue.

**Proposed change — `config/workflows/autopilot/development.yaml`:** add a re-dispatch guard as the first step.
- Before: orientation step unconditionally says "Before writing code, orient yourself with this structured context."
- After: *"STEP 0 — CHECK FOR RE-DISPATCH: if your task description states this phase already completed successfully and no new blocker/ticket references this feature, run `git log -1 --stat` and compare against the last commit's file list. If nothing changed and no new instruction was given, call `complete_my_task` with a one-line summary immediately — do not re-run the full verification checklist."*

### Issue 2 — Invented, non-schema verdict string bypassed the product_validation gate

**Evidence:** `product_validation.yaml`'s schema only allows `PASS | PASS_WITH_MINOR_GAPS | NEEDS_WORK | ARCHITECTURE`. Both product_validation runs wrote `"verdict": "CONDITIONAL_PASS"` — an undefined fifth value — despite `unmet_requirements` being non-empty (FR-7), which per the yaml's own rule should have forced a return to development. Instead the pipeline advanced through doc_review with FR-7 still broken. This is the highest-value fix: it directly explains why a real functional gap shipped.

**Proposed change — `config/workflows/autopilot/product_validation.yaml`:**
- Before: `verdict is one of: "PASS" | "PASS_WITH_MINOR_GAPS" | "NEEDS_WORK" | "ARCHITECTURE"`.
- After: *"verdict MUST be exactly one of these four strings — do NOT invent variants like 'CONDITIONAL_PASS'. A partial pass with open functional-requirement gaps is NEEDS_WORK, not a fifth option; PASS_WITH_MINOR_GAPS is only for ≤2 purely cosmetic items. The gate string-matches exactly — an unrecognized value is not guaranteed to trigger a return to development."*

### Issue 3 — Adversarial review's BLOCKER-2 survived 3 runs on false "fixed" claims

**Evidence:** Run 1 flags the race condition (BLOCKER-2). Runs 2 and 3 both claim it's fixed. Run 4 finds it's still present — the earlier "fixed" claims were wrong; the actual fix landed only at that point. Architectural review had independently flagged the same DB-session issue earlier, but the two review phases weren't cross-referencing each other's open findings.

**Proposed change — `config/workflows/autopilot/adversarial_review.yaml`:** *"Before writing a new report, read `docs/architectural_review/architectural_review_report.md` for FIX items touching the same code path you're re-checking (DB sessions, race conditions, etc.). If a prior run of THIS phase claimed a BLOCKER was fixed and it still reproduces, don't just update the status — quote the diff hunk you inspected and state why the earlier verification was wrong, so development doesn't get a false all-clear."*

### Issue 4 — False QA failure from an uncommitted config fix

**Evidence:** A QA run crashed with 0 tests collected because the libtmux pytest plugin auto-loads and errors on fixture marks, reported upstream as `pass_rate=0%`. A prior pass had already claimed to fix this via a new `pytest.ini` that was never actually committed, so the failure silently reappeared and burned a full QA→development cycle before the real root cause (missing `-p no:libtmux` in the already-tracked `pyproject.toml`) was found.

**Proposed change — `config/workflows/autopilot/development.yaml`:** *"If a fix depends on a config file (pytest.ini, .flake8, etc.), confirm with `git status`/`git diff --stat` that the file is actually staged/committed before claiming the fix is done. Prefer editing an already-tracked config file (pyproject.toml) over creating a new untracked one when both achieve the same effect."*

### Issue 5 — Security review capped-notice run count likely cross-feature-contaminated

**Evidence:** The capped notice claims "19 runs," but only ~6 security_review commits exist for this feature. The parent design directory ran many sibling features through the same phase machinery, suggesting the run-counter is keyed to the parent design session rather than this feature's workflow_id.

**Proposed change (orchestrator, not prompt):** verify that the counter driving `docs/*/​*_capped_notice.md` run-count reporting is keyed by `workflow_id`, not the parent design/session ID, so forensics reports aren't misled about actual per-feature retry counts.

## 4. Summary of recommended actions

1. Add a re-dispatch guard to `development.yaml` (Issue 1).
2. Tighten the verdict enum instruction in `product_validation.yaml` to forbid invented values (Issue 2) — highest priority, directly caused a shipped gap (FR-7 / BudgetStatusCard.tsx built but never mounted).
3. Require cross-referencing prior review findings in `adversarial_review.yaml` before declaring a blocker fixed (Issue 3).
4. Require config-file fixes be verified as committed before being claimed done (Issue 4).
5. Fix run-counter scoping so capped-notice reporting is per-workflow, not per-parent-session (Issue 5, orchestrator-side).
