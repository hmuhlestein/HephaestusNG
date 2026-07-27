---
type: implementation_status
feature_id: des-91c8-pi-extension
status: complete
---

# Implementation Status: Pi Cost Tracker Extension

**Feature ID:** des-91c8-pi-extension
**Date:** 2026-07-26

## Summary

Per `docs/architecture.md`, this feature's original code scope was one
wrong line in a README. Adversarial review then found 2 BLOCKER
data-integrity bugs in the cost-collection pipeline the README documents;
the pipeline's completion gate requires open bug tickets to be fixed and
resolved, not just filed, so both were fixed in this pass (see below) —
superseding architecture.md's original "README only" scope boundary.

## Task 1: README fix — DONE

`extensions/hephaestus-cost-tracker/README.md` line 44 changed from
`POST /cost-entries` to `POST /api/autopilot/cost-entries`, matching
`extensions/hephaestus-cost-tracker/src/index.ts:123` and the route in
`src/mcp/autopilot_api.py`. `git diff` touches only this line.

## Task 2: Live `pi` verification — ACCEPTED RISK, NOT PERFORMED

No environment with a real `pi` binary is available in this sandboxed
worktree. Per `docs/requirements_analysis.md` §10 and `docs/architecture.md`
§6, this is filed as an accepted, explicit risk rather than fabricated or
blocking. No source defect was found or assumed; static inspection of
`index.ts` against pi's documented extension hook shape
(`initialize(ctx)` / `turn_end(ctx, turn)`) shows no inconsistency.

## Task 3: Regression check — DONE (partial, pre-existing unrelated failure noted)

- `pytest tests/test_cost_collection_service.py` — 20 passed, no failures.
- `pytest tests/test_cost_tracking.py` — fails to collect on both this
  branch and `main` (verified via `git stash`), unrelated to this feature:
  `ImportError: cannot import name '_pause_project_workflows' from
  'src.core.cost_derivation'`. The function was renamed/moved to
  `pause_project_workflows` in `src/autopilot/orchestrator.py` in a prior
  merged feature; the test file's import was never updated. Out of scope
  per architecture §2 (no changes to `cost_derivation.py` or budget
  enforcement permitted in this feature) — not fixed here. Pre-existing,
  not a regression introduced by this change.

## Adversarial review response (2026-07-26)

Adversarial review found 2 BLOCKERs in the pre-existing cost pipeline this
README documents. Both filed as tickets, then fixed (the phase gate
requires open bug tickets resolved before completion):

- **B-1 — double-counting** (`ticket-2bde3953-0dce-40cd-92be-206196378d21`,
  resolved): the pi extension's real-time `/cost-entries` POSTs and the
  JSONL fallback tailer both ran unconditionally, so every turn was
  recorded twice whenever the extension was active (`SessionCostCheckpoint`
  was never updated by the real-time path). Fix, in
  `src/services/cost_collection_service.py::collect_task_cost`: before
  tailing the JSONL transcript for a `pi`-CLI task, check whether any
  `source="pi"` `CostEntry` rows already exist for that `task_id`. If so,
  the extension is confirmed active for that session and is treated as
  the sole source of truth — the JSONL fallback is skipped entirely for
  that task instead of running alongside it. README's "Fallback Behavior"
  section updated to describe this instead of the false
  "prevents double-counting" claim.
- **B-2 — batch loss on one bad entry**
  (`ticket-9f215bb8-a85b-46c7-a626-a51da138fd4e`, resolved):
  `collect_task_cost` wrote all of a batch's `CostEntry` rows inside one
  `with get_db()` block; one `record_cost()` exception (e.g. a validation
  error) rolled back every entry already recorded in that batch and
  skipped the checkpoint update, with no retry path — permanent, silent
  data loss for the whole task. Fix: each `record_cost()` call is now in
  its own try/except with an explicit `db.commit()` on success and
  `db.rollback()` on failure; a bad entry is logged at error level and
  skipped without discarding the rest of the batch, and the checkpoint
  still advances afterward.

Both fixes covered by new tests in `tests/test_cost_collection_service.py`
(`TestCollectTaskCostRealtimeVsFallback`, `TestCollectTaskCostPartialFailure`),
stash-verified to fail against the pre-fix code and pass against the fix.

W-1/W-2/W-3 and N-1 (inconsistent cost-cap handling, no task/workflow ID
existence validation, `collect_task_cost`'s CLI-type branching, and
invisible fire-and-forget POST failures) are pre-existing, not BLOCKERs,
and out of scope for this pass — not independently ticketed since the
review did not require it for gate pass.

## Files changed

- `extensions/hephaestus-cost-tracker/README.md` (POST path fix +
  accurate fallback-behavior description)
- `src/services/cost_collection_service.py` (B-1, B-2 fixes)
- `tests/test_cost_collection_service.py` (regression tests for both)
- `docs/implementation_status.md` (this file)
