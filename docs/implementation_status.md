---
type: implementation_status
feature_id: des-91c8-pi-extension
status: complete
---

# Implementation Status: Pi Cost Tracker Extension

**Feature ID:** des-91c8-pi-extension
**Date:** 2026-07-26

## Summary

Per `docs/architecture.md`, this feature's entire code scope was one wrong
line in a README. No runtime code changed.

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

## Files changed

- `extensions/hephaestus-cost-tracker/README.md` (1 line)
- `docs/implementation_status.md` (this file)
