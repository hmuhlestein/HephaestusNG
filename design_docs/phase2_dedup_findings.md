# Phase 2, §4.1 — Task-creation-claim/retry/arbitration consolidation findings

## What was done

### 1. Bug fix: orphan-retry-cap exemption (§2 — separate commit)
- **Root cause**: `get_tasks()` in `engine_client.py` never included `failure_reason` in its returned dict, so `_retry_failed_tasks`'s orphan detection (`is_orphan = "Orphaned" in (task.get("failure_reason") or "")`) was always `False`.
- **Fix**: Added `"failure_reason": t.failure_reason` to `get_tasks()`'s returned dict.
- **Test**: Flipped `test_orphaned_task_incorrectly_capped_bug` assertions — now asserts the task retries (status=`in_progress`) instead of being capped.
- **Files**: `src/autopilot/orchestrator/engine_client.py`, `tests/test_orchestrator_helpers.py`

### 2. Staleness-fallback three-way merge (§1)
- **Shared helper**: `_clear_stale_task_creation_claim(db, phase_id, *, repair_status=True)` — clears a stale `task_creation_claimed_at` and optionally repairs `PhaseExecution.status` (pending/completed → in_progress) + backfills `started_at` from the latest task.
- **Design decision**: Option (a) — extended the two inline copies to also do the sweep version's repair work. All three call sites now get full self-healing. The `repair_status=False` path is used by `fire_spec_gate_if_ready` (which deliberately must not flip status since `mark_phase_complete` already left the execution in whatever state the evaluation decided).
- **Call sites updated**:
  - `_release_stale_task_creation_claims` (sweep) — delegates to helper
  - `_create_phase_task` (inline) — delegates to helper
  - `_create_corrective_task` (inline) — delegates to helper
  - `fire_spec_gate_if_ready` — delegates with `repair_status=False`

### 3. GOTO-reset stale-execution query consolidation (§4)
- **Shared helper**: `reset_stale_executions_on_goto(db, workflow_id, target_phase_order, *, exclude_phase_id)` — resets `PhaseExecution` rows at/after a goto target to "pending", clears `completed_at`, `task_creation_claimed_at`, and `started_at`.
- **Call sites updated**:
  - `fire_spec_gate_if_ready` in `phase_transitions.py`
  - `_handle_evaluation_goto` in `phase_manager.py`
  - `_handle_force_goto` in `phase_manager.py`
- **Import**: `phase_manager.py` uses a lazy-import wrapper (`_reset_stale_executions_on_goto`) to avoid circular dependency with `phase_transitions.py`.

### 4. Arbitration-dispatch consolidation (§3)
- **Decision**: `_resolve_arbitration_outcome` now gains the same `completion_notes`-substitution logic as `_fire_phase_transition`. When the gate reason is `result_missing` and the completing task has `completion_notes`, those notes are used as feedback instead of the generic "missing" message. The underlying problem (a stale/generic reason string reaching the next agent) applies equally to an arbitration-triggered dispatch.
- **What was NOT consolidated**: The two paths share only their final `_create_phase_task` call; everything upstream differs by design (one goes through real evaluation, the other is post-arbitration). Extracting a shared helper would add indirection without reducing duplication.
- **Files**: `src/autopilot/orchestrator/phase_transitions.py`

### 5. GOTO-reset characterization tests (§4 verification)
- **Tests added** to `tests/test_phase_transitions_spec_gate.py`:
  - `test_goto_never_resets_its_own_firing_phase_execution` — asserts the firing phase's "completed" execution survives the goto-reset (self-exclusion works).
  - `test_goto_resets_phase_at_target_order` — asserts a phase at the target's own order gets reset to "pending" (at-or-after scope works).
- Both test `reset_stale_executions_on_goto` directly, verifying the shared helper's behavior.

## Test results
All 357 targeted tests pass (zero regressions). All 47 characterization tests from the prompt's test list pass.

## Ruff
No new ruff issues introduced. Pre-existing E731 (lambda assignments) in `phase_transitions.py` unchanged.

## Out-of-scope findings
- `_retry_failed_tasks` and `_maybe_retry_failed_tasks` are three separate retry implementations with genuinely different trigger conditions — they don't fully collapse into one implementation. Further analysis needed before attempting consolidation.
