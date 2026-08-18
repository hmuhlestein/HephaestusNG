# Phase 2, §4.5 — tmux message delivery primitive findings

## What was done

### Migration
Both `AgentCommunicationService` methods now route through `AgentMessenger` instead of shelling out to `tmux` directly:

- **`get_child_logs`** — now uses libtmux (via `agent_manager.tmux_server`) instead of `subprocess.run(["tmux", "capture-pane", ...])`. Synchronous, no executor offload needed.
- **`send_message_to_child`** — now async, delegates to `AgentMessenger.send_message_to_agent`. Closes two gaps: consistent escaping (`"$`` escaping + quote wrapping) and stuck-shell detection (`_pane_is_wedged`).
- **`nudge_child`** — now async (delegates to `send_message_to_child`).
- **`monitor_and_nudge_stuck_children`** — now async (calls `nudge_child`).

### Constructor change
`AgentCommunicationService.__init__` now accepts optional `agent_manager` parameter. Creates `AgentMessenger` internally when `agent_manager` is provided. Backward-compatible — existing callers passing only `db_manager` still work (methods that need the messenger will log an error and return gracefully).

### Callers updated
All 6 `AgentCommunicationService` instantiations in `src/mcp/agents_api.py` now pass `server_state.agent_manager`. The 3 async call sites (`send_message_to_child`, `nudge_child`, `monitor_and_nudge_stuck_children`) now use `await`.

### Async/executor gap
`AgentMessenger.send_message_to_agent` is `async` — it uses `await asyncio.sleep(0.5)` for the Ctrl-C recovery delay in `_pane_is_wedged`. The old `subprocess.run` was synchronous and blocked the event loop. Routing through the messenger closes this gap.

## Characterization tests
5 characterization tests added for `AgentCommunicationService`'s tmux methods:
- `get_child_logs` returns None without agent_manager
- `send_message_to_child` returns False without agent_manager
- `send_message_to_child` rejects non-child agents
- `get_child_logs` rejects non-child agents
- `nudge_child` uses the parent_nudge_child prompt template

All verify behavior through the new `AgentMessenger`-backed implementation.

Escaping comparison: `AgentMessenger`'s escaping (`"$`` escaping + quote wrapping + libtmux `send_keys`) is a strict superset of the old per-character `subprocess.run` approach. The old approach sent each character as a separate argv element; the new approach uses libtmux's `send_keys` with proper escaping. Both prevent shell injection. The new approach additionally handles stuck shells via `_pane_is_wedged`.

## Test results
150 targeted tests pass (zero regressions). 1 pre-existing failure (`test_resets_task_before_terminating_agent`) confirmed on HEAD.

## Ruff
No new issues introduced.

## Out-of-scope findings
- `AgentCommunicationService`'s methods that don't touch tmux (`get_children`, `get_children_status_summary`, `create_child_task`, etc.) were not migrated — they don't use tmux and are out of scope.
- The `_pane_is_wedged` detection is now available to all `send_message_to_child` callers for free — previously only `AgentMessenger` callers had it.
