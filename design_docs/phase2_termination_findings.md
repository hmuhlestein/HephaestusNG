# Phase 2, §4.2 — Agent-termination primitive findings

## What was done

### Shared primitive created
- **`terminate_agent(agent_id, *, kill_tmux=False, reason="")`** in `src/autopilot/orchestrator/engine_client.py` — the single shared primitive for agent termination.
- Sets all three invariant fields: `status="terminated"`, `current_task_id=None`, `terminated_at=datetime.utcnow()`.
- Resets stray tasks (assigned_agent_id=agent_id, non-terminal status) to `pending` BEFORE flipping the agent row — ordering requirement from two live incidents (91699b1, 92caa82).
- `terminate_agent_direct` kept as backward-compatible alias.
- `kill_tmux` parameter reserved for future use; full tmux teardown stays in `Terminator.terminate_agent` via AgentManager.

### Invariant violations fixed (5 sites)
1. **`src/mcp/frontend/_shared.py:2136`** — missing `terminated_at` ✅
2. **`src/mcp/frontend/_shared.py:2251`** — missing `terminated_at` ✅
3. **`src/monitoring/auto_restart.py:97`** — missing `terminated_at` ✅
4. **`src/monitoring/mechanical_recovery.py:1635`** — missing `current_task_id=None` ✅
5. **`src/monitoring/orphan_reaper.py:123`** — used `datetime.now()` instead of `datetime.utcnow()` ✅

### Already-correct sites annotated (8 sites)
- `src/agents/launch_pipeline.py:1818, 1855, 1913` — all three fields set, invariant comments added
- `src/autopilot/orchestrator/__init__.py:2819` — all three fields set, invariant comment added
- `src/autopilot/orchestrator/engine_client.py:127` — already called `terminate_agent_direct`, now calls `terminate_agent`
- `src/mcp/autopilot/feature_routes.py:267` — all three fields set, invariant comment added
- `src/agents/terminator.py:275` — existing correct primitive, unchanged

### Transactional sites not migrated (9 sites)
These are embedded in larger DB transactions and can't call the standalone `terminate_agent` (which creates its own session). All set the three fields correctly:
- `src/agents/launch_pipeline.py:1818, 1855, 1913` (annotated)
- `src/autopilot/orchestrator/__init__.py:2819` (annotated)
- `src/mcp/autopilot/feature_routes.py:267` (annotated)
- `src/mcp/autopilot/project_routes.py:1268` (all 3 fields set, no annotation yet)
- `src/mcp/autopilot/queue_routes.py:182, 363` (all 3 fields set, no annotation yet)
- `src/mcp/server.py:5055, 5171, 766` (all 3 fields set, no annotation yet)

## Characterization tests
- `TestTerminateAgentInvariant` — 5 tests covering the primitive's invariant, ordering (task-reset-before-agent-flip), task-completion-during-termination race, nonexistent-agent, and backward-compat alias
- Existing `TestTerminateAgentDirectResetsTask` still green (2 tests)
- All 31 targeted tests pass

## DB-level CHECK constraint (optional, not implemented)
A `CHECK` constraint enforcing `status == "terminated" → current_task_id IS NULL AND terminated_at IS NOT NULL` would make the invariant structurally unrepresentable. Tradeoff: changes the DB layer's failure mode (exception on a previously-silent bad write). Not included — flagged for review.

## Ruff
No new issues introduced. All touched files clean.

## Out-of-scope findings
- `src/mcp/autopilot/project_routes.py:1268`, `queue_routes.py:182/363`, `server.py:5055/5171/766` — all set the three fields correctly but are transactional sites not migrated to the primitive. No action needed.
