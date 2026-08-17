# Implementation Status

**Spec:** design_docs/phase_1b_decomposition.md
**Started:** 2026-08-16 17:30 MDT
**Current Target:** 1/4

## Targets

| # | Name | Status | Started | Completed | Notes |
|---|------|--------|---------|-----------|-------|
| 1 | task_completion_service.py | ✅ done+gated | 08-16 | 08-16 | review 0 blockers; full-suite gate PASS (all 81 failures ⊂ HEAD's 178 pre-existing) |
| 2 | mcp/api.py → mcp/frontend/ | ✅ done+gated | 08-16 | 08-16 | review found 1 real BLOCKER (missing `import os` in dashboard_routes — fixed); full name-resolution audit CLEAN; guardrail green |
| 3 | monitoring/monitor.py | ✅ done | 08-16 | 08-16 | worker left _auto_restart_agent as duplicate body — fixed to delegator; 26/26 stubs audited; review: no blockers, 5 NOTES; 160/160 targeted |
| 4 | agents/manager.py | ✅ done+gated | 08-16 | 08-17 | review found 3 BLOCKERS + 5 FIXES — ALL verified vs HEAD and fixed; 191 passed/2 pre-existing post-fix; unit probe confirms B1/B2 |

## Key Decisions
- No git commits (user directive). Sequential ordering substitutes for doc's commit sequencing.
- collect_cost_on_completion → own cost.py (user chose option a).
- FrontendAPI method count drift (42 not 38) — cosmetic only, no design impact.

## Deviations from Plan
| Target | Issue | Impact | Resolution |
|--------|-------|--------|------------|

## Bugs Logged (Phase 3 candidates, DO NOT FIX)
- (from T1 review NOTE 5) tests/test_task_completion_service.py::test_passes_when_no_workflow_id_and_files_in_feature_dir is env-dependent: patches Path.exists but code hits os.listdir; passes here only due to leftover .hephaestus/features/ dir, fails on pristine checkout.
- Stale prose pointers to fire_spec_gate_if_ready in src/phases/phase_manager.py:1256,1526 and worktree_integration.py:669 (pre-existing comments, optional touch-up).
- (full-suite gate) tests/manual_validation_test.py is a manual script that fires HTTP POSTs to localhost:8300 at MODULE IMPORT with no timeout — hangs any bare `pytest tests/` run (faulthandler-confirmed). Pre-existing; excluded from the full-suite gate via --ignore. Candidate Phase 3 fix: rename/guard.
- (T3 review NOTEs, all compat artifacts — do NOT fix in 1b): duplicated constants UNCONFIRMED_COMPLETION_ESCALATE_AFTER (monitor.py:178 vs mechanical_recovery.py:53) and MAX_STUCK_TASK_NUDGES (monitor.py:162 vs health_audit.py:21) — tests read loop copies, code uses collaborator copies, divergence risk; dead lazy-init `if not hasattr(...)` guards in mechanical_recovery.py (init now declares all); string-based _get_regex/_get_constant proxy (typo fails at runtime); split config sources (self.config vs function-scoped get_config) in MechanicalRecoveryDetector; monitor.py still hoards 10 regexes + _SGR_RE + MAX_STUCK_TASK_NUDGES consumed only by proxy/tests.
- (T4 baseline) tests/test_prompt_delivery_cleanup.py::test_agent_and_task_cleanup_on_prompt_delivery_failure and ::test_cleanup_handles_tmux_kill_errors_gracefully — 2 PRE-EXISTING failures at HEAD (confirmed in baseline run, not touched by any target so far).
- (full-suite gate result) HEAD baseline (pristine tree, /tmp/heph-baseline): 75 failed + 81 errors pre-existing. Our T1+T2 tree: 81 failed, 0 errors — strict SUBSET of HEAD failures, zero regressions. Full-suite gate = no-new-failures-vs-HEAD-baseline.
- Orphaned heph-codex-test-* tmux sessions exist from live tmux tests (08:49/09:10/09:50, pre-existing) — live-dispatch tests in this env can be very slow/hang; watch the full-suite run.

## Build & Test Commands
- **Test (full):** `python -m pytest tests/ -x -q`
- **Lint:** `black --line-length 88 src/` ; `flake8 src/`
- **Type check:** `mypy src/` (availability TBC)

## Baseline Failures (pre-existing at HEAD, before any change)
Target 1 scope: 4 failed, 80 passed:
- test_task_completion_service.py::TestVerifyOutputArtifact::test_rejects_when_workflow_has_no_working_directory
- test_task_completion_service.py::TestVerifyGateResultSchema::test_passes_the_documented_qa_shape
- test_goto_reconvergence.py::test_start_next_phase_honors_action_target_phase_skipping_intermediates
- test_update_task_status_response_shape.py::TestGateResultSchemaFloor::test_documented_qa_shape_still_succeeds

## Review Findings Log
(empty)
