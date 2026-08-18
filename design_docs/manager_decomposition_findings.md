# Manager Decomposition — Phase 1b Findings

**Date**: 2026-08-21
**Status**: output-capture extraction complete; remaining clusters not yet extracted

## Freshness check (AST-verified against live HEAD)

The live `src/agents/manager.py` is **3,430 lines, 48 methods** on `AgentManager` — the headline in the design prompt is exact. Several line-range estimates in the planning docs are stale:

| Method | Design doc's estimate | Actual (AST-verified) | Delta |
|---|---|---|---|
| `create_agent_for_task` | 985 lines (259-1243) | **417 lines** (804-1220) | -568 — the shared-step extraction work already done cut it roughly in half |
| `restart_agent` | 399 lines (2257-2655) | **222 lines** (2232-2453) | -177 — same reason |
| `terminate_agent` | 270 lines (1939-2209) | **267 lines** (1917-2183) | -3 — nearly exact |

The design doc expected `get_agent`, `get_agents`, `update_agent_status` in the utility cluster — **none of these exist** on `AgentManager`. The actual utility method is only `get_active_agents` (19 lines). The doc also listed methods that were already extracted as shared step helpers during the earlier create/restart work — those are present but were counted as a "launch pipeline" cluster that's already factored.

## What was extracted

**9 methods, 680 lines** into `src/agents/output_capture.py` as `AgentOutputCapture`:

| Method | Lines | Role |
|---|---|---|
| `get_agent_output` | 141 | Public entry point: resolves transcript source |
| `_resolve_tmux_transcript_dir` | 55 | Finds `.hephaestus/tmux/` directory |
| `_read_transcript_log` | 285 | ANSI-strip, dedup, redraw-collapse pipeline |
| `_find_tmux_session` | 8 | libtmux session lookup by name |
| `_capture_pane_lines` | 17 | capture-pane full scrollback |
| `_append_lines` | 10 | Static helper for file appending |
| `_poll_stable_transcript` | 119 | Stability-tracking poll (N consecutive identical reads) |
| `_flush_stable_transcript` | 26 | Unconditional final flush before session kill |
| `_get_orchestrator_output` | 19 | Orchestrator log-file reader |

## Extraction methodology

Scripted via `scripts/split_manager.py` (modeled on `scripts/split_autopilot_api.py`):
- AST-parsed verified line ranges for all 48 methods
- Extracted method bodies copied verbatim (no hand-editing)
- Lossless reassembly assertion: non-extracted lines preserved byte-for-byte
- `py_compile` check on both generated files
- Delegator stubs on `AgentManager` with lazy-init accessor (`_get_output_capture`) to handle tests that bypass `__init__` via `__new__`

## Test verification

All targeted tests pass with zero regressions:

| Test file | Result |
|---|---|
| `tests/test_agent_output_capture.py` | **7 passed** (4 pre-existing failures fixed in this session) |
| `tests/test_agent_output_integration.py` | 5 passed |
| `tests/test_transcript_processing.py` | 9 passed, 1 skipped, **3 pre-existing failures** (libtmux `attached_window` deprecation) |
| `tests/test_agent_manager.py` | 32 passed, **1 pre-existing failure** (libtmux deprecation) |
| `tests/test_monitor.py` | **134 passed** |
| `tests/test_orphan_reaper.py` | 9 passed, **1 pre-existing failure** (libtmux deprecation) |

No `@patch(...)` retargeting was needed — all test mocks use `mock_agent_manager.get_agent_output = Mock(...)` on an instance mock (not a string-patch on the module), so the delegator transparently forwards.

## Remaining clusters (not yet extracted)

These clusters remain on `AgentManager` and could be extracted in a future pass:

1. **Termination cluster** (~313 lines): `terminate_agent`, `_commit_wip_in_shared_worktree` — lower priority since termination logic has heavy cross-calls with other methods.
2. **Launch pipeline** (already factored into ~15 shared step methods) — further extraction would move the step methods into a `LaunchPipeline` collaborator, but the current structure is already clean.
3. **Messaging** — already delegated to `AgentMessenger` (pre-existing extraction). `broadcast_message_to_all_agents` and `send_direct_message` stay on `AgentManager` because they call `self.send_message_to_agent` which tests patch at the instance level.

## Findings for Phase 3

- **libtmux `attached_window` deprecation**: 6 pre-existing test failures across `test_stable_transcript.py`, `test_transcript_processing.py`, `test_agent_manager.py`, `test_orphan_reaper.py`. The codebase uses `session.attached_window.attached_pane` extensively — needs a migration to `session.active_window.active_pane` (libtmux ≥ 0.31).
- **`test_prompt_delivery_cleanup.py`**: 3 pre-existing failures (confirmed on HEAD). Not related to this extraction.
