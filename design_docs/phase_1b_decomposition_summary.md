# Phase 1b Decomposition — Final Summary

**Date:** 2026-08-17
**Spec:** design_docs/phase_1b_decomposition.md
**Result:** All four targets implemented, reviewed, and gated. No commits made (per user directive); everything is in the working tree.

## Targets

### 1. task_completion_service.py (1,125 lines, 11 static methods)
- `fire_spec_gate_if_ready` migrated to `src/autopilot/orchestrator/phase_transitions.py` (module-level, 4 redundant function-scoped imports deleted, `DatabaseManager()` → `DatabaseManager(None)` per doc lesson 3).
- 10 remaining methods extracted to `src/services/task_completion/` package (memory, verification, tickets, validation, git_link, cost — cost.py per user decision).
- `TaskCompletionService` retained as 10-method delegating facade (stable patch surface; all test @patch targets still resolve).
- 7 patch strings retargeted in test_update_task_status_ordering.py; TestFireSpecGateIfReadyGoto relocated to tests/test_phase_transitions_spec_gate.py.
- Review: 0 blockers, 0 fixes, 5 notes.

### 2. mcp/api.py (3,225 lines, 42 routes) → src/mcp/frontend/
- Guardrail route-set test FIRST (tests/test_frontend_api_routes_guardrail.py, hardcoded 42-route {(method, path)} baseline).
- `stop_workflow` extracted to FrontendAPI method before the split (per doc ordering).
- Split: _shared.py (imports + FrontendAPI + `frontend_api = None`), 4 cluster route files (4/6/15/17), aggregator __init__.py. All routes un-nested to top-level; every reference module-qualified `_shared.frontend_api` (mutable-global rule).
- Original api.py → _review/unwanted/api.py (not deleted).
- 1 BLOCKER found in review and fixed: missing `import os` in dashboard_routes.py (two download routes would 500). Full name-resolution audit of all 6 files clean afterward.

### 3. monitoring/monitor.py (MonitoringLoop, 3,677 lines)
- 5 collaborators per 1998a11 template: auto_restart.py (AutoRestart — shared `_auto_restart_agent`, doc option c), mechanical_recovery.py (13 detectors + _log_agent_event), guardian_dispatch.py (5), health_audit.py, diagnostic_agent.py (5).
- 26 delegator stubs keep old underscored names on MonitoringLoop; 10 @property+@setter bridges for test-visible state (same-object semantics runtime-verified); _monitoring_cycle/_save_conductor_analysis/start/stop byte-identical.
- Worker initially left _auto_restart_agent as a duplicate body — fixed to delegator (script: fix_auto_restart_delegator.py, idempotent).
- Review: no blockers, 5 compat NOTES (see Phase 3 log).

### 4. agents/manager.py — create_agent_for_task (~985) × restart_agent (~399)
- **Restart characterization tests written FIRST** (tests/test_restart_agent_characterization.py, 15 tests: model resolution, session-id gen, prompt delivery, worktree incl. the silent-None pin) — passed against pre-extraction code.
- `get_launch_rejection_patterns()` on CLIAgentInterface: concrete base [command not found, No such file or directory]; PiAgent + model.{0,60}not found; ClaudeCodeAgent + Bypass Permissions mode; 4 CLIs inherit base (spec-mandated scoping — HEAD applied the model pattern to all CLIs; documented as intentional in review F4).
- 11 step methods; both functions now thin orchestrators (create 419 lines, restart ~220). Public signatures unchanged.
- Two documented gap-closings added to restart (each with new regression test): `_check_termination_race` and `_detect_launch_failure` (wording mechanism = _CLAUDE_CODE_CONFIRMATION_PATTERN constant; restores HEAD's exact two error wordings).
- **Adversarial review found 3 BLOCKERS + 5 FIXES in the extraction's parameter wiring — all verified against HEAD and fixed:**
  - B1: derived Phase-row cli_model dropped (model gate keyed on original param) → PhaseConfig.phase_cli_tool carries the derived raw value; unit-verified incl. the model-without-tool discard case.
  - B2: caller cli_type param dead (validator_agent passes cli_type="claude") → threaded as middle fallback.
  - B3: restart-only worktree elif leaked into create → gated on `not create_if_missing`.
  - F1: fallback recursion lost derived glm-token-env/thinking → now passes phase_config values.
  - F2: restart reloaded the shared branch_manager singleton → throwaway WorktreeManager (HEAD behavior).
  - F3: restart output dir named by phase_id → phase-name resolution moved before _prepare_launch_environment.
  - F5: restart gained undocumented codegraph pre-warm → `prewarm_codegraph` param (restart=False).
- After fixes: 191 passed / 2 pre-existing failures (unit probe confirms B1/B2 semantics).

## Gates (all pass)
| Gate | Result |
|---|---|
| Targeted tests per target | T1: baseline-identical; T2: 9/9; T3: 160/160; T4: 191 pass / 2 pre-existing |
| Full suite vs pristine-HEAD baseline | HEAD: 178 failed/errored (env). Our final tree: 80 failed — STRICT SUBSET, zero regressions |
| Adversarial review per target | T1: clean; T2: 1 blocker fixed; T3: clean (5 notes); T4: 3 blockers + 5 fixes, all verified & fixed |
| §3.3 exit criteria | god-objects decomposed (sizes above); no stale imports (src.mcp.api, fire_spec_gate refs outside allowed files); guardrail route test green |

## Excluded from gates (environment, pre-existing)
- tests/manual_validation_test.py — fires un-timed HTTP POSTs at module import; hangs bare `pytest tests/` (faulthandler-confirmed). Excluded via --ignore.
- TestCodexTmuxLifecycle — drives a real codex CLI in tmux; hangs 15+ min in this env. Deselected everywhere (orphaned heph-codex-test-* sessions predate this work).

## Phase 3 bug log (surfaced, NOT fixed — per spec)
1. `src/mcp/frontend/_shared.py:2139,2254` — `agent.status = "terminated"` where "stalled" was intended (doc's two bug sites, moved verbatim).
2. `src/mcp/frontend/phase_routes.py:93` — hardcoded `DatabaseManager("hephaestus.db")` (doc site, moved verbatim).
3. Dead routes `get_agents`/`get_agent_output` (shadowed by agents_api; moved verbatim, not deleted per spec).
4. `src/monitoring/auto_restart.py` — `_auto_restart_agent` never sets `terminated_at` (violates agent-termination invariant; doc site, preserved + documented in docstring).
5. `src/monitoring/auto_restart.py` — task-reset uses raw get_session() (no session_scope).
6. T3 compat notes: duplicated constants (UNCONFIRMED_COMPLETION_ESCALATE_AFTER monitor.py vs mechanical_recovery.py; MAX_STUCK_TASK_NUDGES monitor.py vs health_audit.py — tests read loop copies, code uses collaborator copies), dead lazy-init hasattr guards, string-based _get_regex/_get_constant proxy, split config sources in MechanicalRecoveryDetector, monitor.py hoards moved regexes/constants.
7. `src/agents/manager.py` — arbitration exclusion-list mismatch (create excludes 'arbitration' in session-id gen, restart does not; pinned by TestRestartSessionId).
8. `src/agents/manager.py` — restart silent-None worktree (no fail-loudly; pinned by TestRestartWorktreeResolution).
9. Stale comments: src/phases/phase_manager.py:1256,1526 and worktree_integration.py:669 reference fire_spec_gate_if_ready in task_completion_service.
10. tests/test_task_completion_service.py::test_passes_when_no_workflow_id_and_files_in_feature_dir — env-dependent (passes only with leftover .hephaestus/features/).
11. tests/manual_validation_test.py import-time HTTP POSTs (see Excluded).
12. Stale docstring: src/services/queue_service.py:173-178 cites the old cli_type resolution line.
13. `src/agents/manager.py` restart: redundant prepare_working_directory call (restart + _prepare_launch_environment); harmless/idempotent.

## Files (working tree, uncommitted)
- Modified: src/agents/manager.py, src/interfaces/cli_interface.py, src/autopilot/orchestrator/phase_transitions.py, src/core/constants.py, src/mcp/server.py, src/monitoring/monitor.py, src/services/task_completion_service.py, tests/{test_agent_manager,test_frontend_api_workflow_selection,test_monitor,test_task_completion_service,test_update_task_status_ordering}.py
- New: src/mcp/frontend/ (6 files), src/services/task_completion/ (7 files), src/monitoring/{auto_restart,mechanical_recovery,guardian_dispatch,health_audit,diagnostic_agent}.py, tests/{test_frontend_api_routes_guardrail,test_phase_transitions_spec_gate,test_restart_agent_characterization}.py
- Moved (not deleted): _review/unwanted/api.py
