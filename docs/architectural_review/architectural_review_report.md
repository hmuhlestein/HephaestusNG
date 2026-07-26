---
type: architectural_review_result
feature_id: des-91c8-pi-extension
verdict: PASS
blocker_count: 0
fix_count: 0
defer_count: 1
---

# Architectural Review: Pi Cost Tracker Extension

**Feature ID:** des-91c8-pi-extension
**Reviewed against:** `docs/architecture.md` (Tasks 1-3) and `docs/requirements_analysis.md` (FR-1 through FR-3)
**Implementation commit:** `618804b`
**Diff reviewed:** `git diff main..HEAD -- extensions/hephaestus-cost-tracker/README.md` (the only non-`docs/` file touched)

## Summary

Architecture scoped this feature to exactly one code change (Task 1: fix
`extensions/hephaestus-cost-tracker/README.md`'s wrong documented POST
path) plus two verification-only tasks with no code expected (Task 2: live
`pi` install check, Task 3: regression test re-run). Development did
exactly that and nothing else — `git diff main..HEAD --stat` for
non-`docs/` files shows a single 2-line diff in that one README. No BLOCKER
or FIX findings.

## Task 1: `README.md` POST path fix

**Architecture (Section 3) specified:** change line 44 from
`POST /cost-entries` to `POST /api/autopilot/cost-entries`, no other edits.

**Implementation:** exactly that line changed. Verified independently: the
new text matches `extensions/hephaestus-cost-tracker/src/index.ts:123`'s
literal POST path and resolves against `src/mcp/autopilot_api.py`'s router
prefix (`/api/autopilot`) + `@router.post("/cost-entries")` decorator
(line 2144). `git diff` for this file touches only the one line — no
adjacent doc drift.

Verdict: compliant, no findings.

## Task 2: Live `pi` install verification

**Architecture (Section 6/7) specified:** verify under a real `pi` binary
if available; if no such environment exists in this pipeline, file it as
an accepted, explicit risk rather than fabricate a result or block.

**Implementation:** `docs/implementation_status.md` correctly reports no
`pi` binary is available in this sandboxed worktree and files this as an
accepted risk citing both the requirements doc and architecture doc,
matching the exact fallback path architecture specified. No fabricated
test result, no source changes made under the guise of "fixing" something
unverifiable.

Verdict: compliant, no findings.

## Task 3: Regression check

**Architecture specified:** re-run
`tests/test_cost_collection_service.py` and `tests/test_cost_tracking.py`,
expect both to pass unchanged since no Python was touched.

**Verified independently (not just trusting the dev report):** ran
`python -m pytest tests/test_cost_collection_service.py -q` myself — 20
passed, 0 failed, matching the dev report exactly.
`tests/test_cost_tracking.py` fails to *collect* with
`ImportError: cannot import name '_pause_project_workflows' from
'src.core.cost_derivation'`. Verified via `git show main:...` that this
import mismatch is identical on `main` — the real function lives as
`pause_project_workflows` in `src/autopilot/orchestrator.py` and is
imported under that name inside `cost_derivation.py:306`; the test file's
import was never updated when that rename/move happened in a prior merged
feature. This is a pre-existing break, not a regression from this feature,
and development correctly declined to fix it — architecture explicitly
prohibits touching `cost_derivation.py`/budget enforcement in this
feature's scope.

Verdict: compliant, no findings (see DEFER below for the pre-existing gap
itself).

## Component Boundaries / Interface Contracts / Data Flow

- No new interfaces introduced. The `CostEntry` request/response contract
  documented in architecture §4 (path, headers, body shape) is unchanged
  in the implementation — confirmed by re-reading `index.ts` post-change;
  only the README's prose changed, not any code.
- Zero changes to `PiJsonlCollector`, `ClaudeCodeCollector`, `CostEntry`
  schema, `cost_derivation.py`, or budget enforcement — matches
  architecture §2's explicit prohibition and requirements §9's
  out-of-scope list.
- No `session_id` field was added anywhere (requirements §9's already-
  justified deviation, correctly left alone).
- Data flow (pi turn_end → TUI status update → POST to
  `/api/autopilot/cost-entries` → derivation rollup) is unchanged from the
  architecture doc; nothing in the implementation deviates from it.

## Over-Engineering Check

None found. No JS/TS test framework was introduced (architecture §2
explicitly decided against it, respected). No new abstractions, no
speculative config, no unrelated refactors. The diff is exactly the 1 line
architecture specified — nothing more.

## BLOCKER findings

None.

## FIX findings

None.

## DEFER findings

1. **`tests/test_cost_tracking.py` cannot collect at all** — pre-existing
   on `main`, unrelated to this feature, but worth a tracked follow-up.
   The `ImportError` at collection time means zero tests in that file run,
   silently, unless collection errors are specifically checked for in CI
   output. Not this feature's responsibility to fix (`cost_derivation.py`/
   orchestrator changes are out of scope per architecture), but any future
   feature touching budget enforcement or pause/resume logic currently
   gets zero regression coverage from this file. Fix would be updating the
   import to `from src.autopilot.orchestrator import
   pause_project_workflows` and updating call sites in the test to match.
   Recommend a separate, small ticket — not blocking this feature's
   completion.

## Gate recommendation

**PASS.** 0 blockers, 0 fixes. Implementation matches architecture exactly;
the one DEFER item is a pre-existing, unrelated issue correctly identified
by development and left out of scope, not introduced by this feature.
