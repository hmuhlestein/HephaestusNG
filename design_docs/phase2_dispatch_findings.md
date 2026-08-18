# Phase 2, §4.3 — Dispatch pipeline reconciliation findings

## What was done

### Step 0: Validator-race fix (separate commit: `ea42a5e`)
- **`src/mcp/memory_api.py:submit_result_validation`** — scoped the validator agent lookup by `workflow_id` via a join on `Task`. Previously used `ORDER BY created_at DESC LIMIT 1` on all `result_validator` agents with no scoping, allowing concurrent validation runs across different workflows to cross-wire outcomes. Now joins `Agent.current_task_id == Task.id` and filters by `Task.workflow_id == result.workflow_id`.

### Consolidation choice: Option 2 (give each path the same guard)
Chose option 2 over option 1 because the three dispatch implementations serve genuinely different callers (HTTP route, in-process orchestrator, validator spawn) with different calling conventions. Unifying them into one function would add indirection without reducing behavioral risk.

**Phase-sibling guard extracted as shared utility:**
- `check_phase_sibling_active(session, task_id, phase_id, *, created_by_filter, orchestrator_agent_id)` in `engine_client.py`
- Checks for another active task (status in `pending/assigned/in_progress/queued`) on the same `phase_id`
- `created_by_filter=True` scopes to orchestrator-created tasks only (prevents blocking legitimate subtasks)
- `created_by_filter=False` blocks ANY active task on the phase (for validator spawn path and LaunchPipeline)

**Guard added to all three dispatch paths:**
1. `create_agent_for_task_direct` (`engine_client.py`) — refactored from inline to shared (created_by_filter=True)
2. `LaunchPipeline.create_agent_for_task` (`launch_pipeline.py`) — new guard, protects ALL callers including `AgentDispatchService.dispatch` (created_by_filter=False)
3. `spawn_validator_agent` (`validator_agent.py`) — new guard (created_by_filter=False)

### Guard audit

**1. In-memory per-phase cooldown dict**: **REMOVED from codebase.** Commit `9a2fd48` added `_phase_last_created[phase_id]` (30s cooldown) to `src/monitoring/monitor.py`. During the collaborator extraction (`7619936`), `monitor.py` was split into 5 collaborators — the cooldown dict was not migrated to any of them. It no longer exists anywhere in the current codebase.

**Impact assessment**: The phase-sibling guard (state-based, checks for another active task on the same phase) covers the same race the cooldown was designed to catch (same-cycle double-fire). The state-based guard is arguably better because it catches the race regardless of timing. The cooldown's 30s window was a weaker, time-based approximation of the same protection. No behavioral regression — the guard that remains is strictly more robust than the one that was lost.

**2. Same-task active-agent guard**: `_check_duplicate_active_agent` in `launch_pipeline.py` — confirmed working. Checks `Agent.current_task_id == task.id AND Agent.status IN (working, idle)`. Fires correctly through the new call path (called before the phase-sibling guard in `create_agent_for_task`).

**3. Status-transition idempotency**: `task_creation_claimed_at` in `phase_transitions.py` — confirmed working. Already well-documented, no changes needed. The phase-sibling guard complements this by catching races the claim mechanism can't reach (different dispatch code paths targeting the same phase).

## Test results
32 targeted tests pass (zero regressions). Orphan reaper, termination, and phase-transitions tests all green.

## Ruff
No new issues introduced. Pre-existing I001 import-ordering findings in `validator_agent.py` unchanged.

## Out-of-scope findings
- `src/monitoring/mechanical_recovery.py` has ~8 `await self.agent_manager.terminate_agent(agent.id)` calls that could potentially use the new `terminate_agent` primitive from `engine_client.py` (for the DB-only path). Not migrated — out of scope for this item.
