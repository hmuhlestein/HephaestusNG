# `output_capture.py` reconciliation — findings

Pre-Phase-4 fix, requested directly ("fix it before phase 4"), not part of a
numbered Phase 3 tier. Closes a gap in Phase 1b's `AgentManager` decomposition
(commit `87a221f`) that `docs/AUTOPILOT_REFACTOR_PLAN.md` §3.3 had reported
as complete.

## What was wrong

`87a221f` split `AgentManager` into `manager.py` + three collaborators
(`launch_pipeline.py`, `terminator.py`, `output_capture.py`), with
`output_capture.py`'s docstring claiming `AgentManager` "delegates to an
AgentOutputCapture instance instead of implementing the transcript plumbing
itself." That delegation never happened:

- `AgentManager.__init__` never constructed `self._output_capture`.
- `output_capture.py` was imported/instantiated nowhere in `src/` (confirmed
  via `grep -rn "from src.agents.output_capture import\|AgentOutputCapture("`).
- `manager.py` kept full duplicate inline implementations of all 9
  transcript-capture methods (`get_agent_output`, `_resolve_tmux_transcript_dir`,
  `_read_transcript_log`, `_find_tmux_session`, `_capture_pane_lines`,
  `_append_lines`, `_poll_stable_transcript`, `_flush_stable_transcript`,
  `_get_orchestrator_output`).
- `terminator.py`/`launch_pipeline.py` each had an `_output_capture` `@property`
  forwarder (`return self._agent_manager._output_capture`) pointing at an
  attribute that didn't exist — dead on both ends, since nothing called the
  property either.
- The two copies had drifted: `manager.py`'s `_read_transcript_log` gained a
  chrome-filtering fix (`chrome_re` — status-bar counters, `MCP:` lines,
  braille spinners, shell-prompt lines) that `output_capture.py`'s stale copy
  never received.

## Fix

1. Ported `manager.py`'s chrome-aware `_read_transcript_log` into
   `output_capture.py`, byte-for-byte identical (diffed via `difflib`, 309
   lines each) — including the `↑↓`/`⠀-⣿` escape-sequence
   style of the source, not literal Unicode characters.
2. Wired real construction: `AgentManager.__init__` now does
   `self._output_capture = AgentOutputCapture(self.db_manager, self.tmux_server)`.
3. Replaced all 9 duplicate method bodies in `manager.py` with one-line
   delegators (`return self._output_capture.<method>(...)`), matching the
   existing `self._launch.<method>(...)` / `self._terminator.<method>(...)`
   delegator style already used elsewhere in the file. `_append_lines` stays
   a `@staticmethod`, delegating via a local import (matching the other
   collaborators' locally-scoped imports, avoiding a module-level circular
   import).
4. `terminator.py`/`launch_pipeline.py`'s `_output_capture` forwarder
   properties now resolve correctly with no code changes to those two files.
5. `manager.py`: 1,363 → 435 lines. `output_capture.py`: 725 → 748 lines
   (chrome-filter port).

## Test fallout

State ownership genuinely moved from `AgentManager` to the `AgentOutputCapture`
collaborator, which broke tests written against the old (never-delegating)
structure:

- `tests/test_transcript_processing.py` — two `AgentManager.__new__(AgentManager)`
  bypass-construction helpers (`_run`, `_make_manager_and_agent`) never ran
  `__init__`, so `_output_capture` didn't exist. Fixed by constructing
  `AgentOutputCapture(mgr.db_manager, MagicMock())` explicitly in both helpers.
- `tests/test_stable_transcript.py` — 7 direct reads of
  `agent_manager._pane_stability_cache[...]` and 11 `monkeypatch.setattr(agent_manager,
  "_capture_pane_lines"/"_poll_stable_transcript"/"_read_transcript_log", ...)`
  calls targeted `AgentManager` directly. Since these methods now internally
  call each other via `self.` on the `AgentOutputCapture` instance, not
  `AgentManager`, the monkeypatches were silently bypassed and the cache read
  raised `AttributeError`. Redirected all of them to
  `agent_manager._output_capture`.
- `tests/test_agent_manager.py` — 4 tests patched
  `"src.agents.manager.asyncio.sleep"`, a target that had already been dead
  since the Phase 1b `launch_pipeline.py` split (the real `asyncio.sleep`
  calls live in `launch_pipeline.py`; other tests in the same file already
  correctly patch `src.agents.launch_pipeline.asyncio.sleep`). The patches
  silently did nothing because `manager.py` still had an unused `import
  asyncio` keeping the patch target resolvable — `ruff --fix` removing that
  orphaned import turned the silent no-op into an `AttributeError` at patch
  setup, surfacing the staleness. Fixed by redirecting all 4 to
  `src.agents.launch_pipeline.asyncio.sleep`. This was pre-existing test debt
  from an earlier phase, not introduced by this task, but was surfaced by it.

Targeted suite (`test_agent_manager.py`, `test_agent_output_capture.py`,
`test_agent_output_integration.py`, `test_stable_transcript.py`,
`test_transcript_processing.py`, `test_orphan_reaper.py`): **101 passed, 1
skipped**, zero regressions. `ruff check` clean on both touched source files.

## Known pre-existing, out-of-scope ruff findings

`tests/test_transcript_processing.py` has 3 pre-existing ruff findings
(unsorted import block, ambiguous variable name `l`, unused `real_open`)
confirmed via `git stash` to predate this task — left untouched per
minimal-touch scope.

## Not done here

`docs/AUTOPILOT_REFACTOR_PLAN.md` §3.3 was corrected in place (appended
correction note, matching the doc's existing style) to reflect that the
`output_capture.py` split is now real. Phase 4 (dead-code deletion) has not
started.
