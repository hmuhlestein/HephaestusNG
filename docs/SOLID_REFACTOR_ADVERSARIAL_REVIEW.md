# Adversarial Review — SOLID/OO Refactor Session

Scope: `git diff 8c28484...1998a11` (commits `bedfba2` through `1998a11`, ~3400
lines across 23 files) — the session implementing
[`docs/SOLID_OO_REVIEW.md`](SOLID_OO_REVIEW.md)'s priority list plus the start
of the `AgentManager`/`MonitoringLoop` god-class decompositions.

Method: 8 parallel finder passes (line-by-line diff scan, removed-behavior
audit, cross-file call-site tracing, language-pitfall scan, wrapper/proxy
correctness, reuse/simplification, efficiency/altitude) plus one sweep pass
for gaps, each independently verified against the current code and the
pre-refactor code (`git show 8c28484:<path>`) before being reported.

**Bottom line:** the refactor mechanics were sound. Across ~3400 changed
lines, verification surfaced one undocumented (likely correct) behavior
change, one latent design flaw, one dropped log line, two pre-existing bugs
carried forward unchanged, and five duplication/design cleanup items. Nothing
found was a newly-introduced crash or data-corruption bug.

---

## Bugs introduced by this refactor

### 1. `process_queue`'s phase resolution now scopes to the task's own `workflow_id`

**Location:** `src/mcp/server.py:1200` (`TaskEnrichmentService.resolve_phase_id` call)

**Finding:** The original `process_queue` never passed `workflow_id` to
`phase_manager.get_phase_for_task(...)` when resolving a digit-string
`phase_id` — verified via `git show 8c28484:src/mcp/server.py` — so it always
fell back to the `phase_manager` singleton's currently-active workflow. The
new `TaskEnrichmentService.resolve_phase_id` call passes
`workflow_id=next_task.workflow_id` explicitly.

**Impact:** In a multi-workflow deployment, a queued task whose `workflow_id`
differs from the singleton's active workflow now resolves its phase against
its *own* workflow instead of the singleton's — a different phase UUID than
pre-refactor code would have produced for the same task.

**Assessment:** Likely a correctness *improvement* — `create_task`'s
equivalent code path already passed `workflow_id` explicitly before this
refactor (comment: "Pass explicit workflow_id for multi-workflow support"),
so this brings `process_queue` in line with `create_task`'s established
pattern. But it is a silent, untested behavior change with no commit-message
callout and no multi-workflow queue-processing test coverage.

**Recommendation:** Add a test exercising `process_queue` with a queued task
whose `workflow_id` differs from `phase_manager.workflow_id`, confirming the
new (task-scoped) resolution is the intended behavior.

### 2. `AgentMessenger` captures `tmux_server` at construction time, not live

**Location:** `src/agents/messenger.py:24` (`__init__`), `src/agents/manager.py:58`

**Finding:** `AgentMessenger.__init__(self, db_manager, tmux_server)` stores a
constructor-time reference to `AgentManager.tmux_server`. Several existing
tests reassign `manager.tmux_server = MagicMock()` *after* `AgentManager(...)`
construction (`tests/test_agent_manager.py:86`,
`tests/test_prompt_delivery_cleanup.py:86,162,213`) — a pattern that works
correctly for every `AgentManager` method reading `self.tmux_server` directly,
but `send_message_to_agent` now delegates to `self._messenger`, which still
holds the *original* pre-reassignment object.

**Impact:** Currently masked — no existing test calls `send_message_to_agent`
after reassigning `tmux_server` (verified via grep). But any future test or
production code path that reconnects/reinitializes `tmux_server` post-construction
and then sends a message will silently operate against the stale session set.

**Recommendation:** Have `AgentMessenger` read `tmux_server` from the owning
`AgentManager` on each call (e.g. pass `agent_manager` instead of
`agent_manager.tmux_server`, or expose `tmux_server` as a property), rather
than storing a copy.

### 3. Dropped log line in `broadcast_message_to_all_agents`

**Location:** `src/agents/manager.py:1625`

**Finding:** The entry log statement
`logger.info(f"Broadcasting message from agent {sender_agent_id}")` present
in the pre-refactor version is missing — even though this method was *not*
moved to `AgentMessenger` (it deliberately stayed on `AgentManager`), the log
line was lost during the surrounding edit.

**Impact:** Minor — loses the one log line marking the start of each
broadcast call; only per-recipient debug-level logs remain, which don't
identify the broadcast's sender or intent.

**Recommendation:** Restore the log line.

---

## Pre-existing bugs, carried forward unchanged

These predate this session's refactor (confirmed via `git show 8c28484`) but
are now visible inside the newly-extracted/registry code, and were touched
directly by this session without being fixed.

### 4. `_tool_send_message` calls `send_message_to_agent` with a nonexistent kwarg

**Location:** `src/mcp/server.py:5782`

**Finding:** `_tool_send_message` calls
`agent_manager.send_message_to_agent(agent_id=..., message=..., sender_agent_id=...)`,
but `AgentManager.send_message_to_agent`'s signature is `(self, agent_id,
message)` — no `sender_agent_id` parameter. This raises `TypeError` on every
call, silently swallowed by `_tool_send_message`'s `except Exception:
logger.warning(...)`.

**Impact:** The `hephaestus_send_message` MCP tool has never actually worked
— every call returns a fake `{"success": True}` while the message is never
delivered. Verified identical in the original `if/elif` chain at
`git show 8c28484:src/mcp/server.py` (same broken call existed before this
refactor); this session moved it verbatim into the new `_MCP_TOOLS` registry
without fixing it.

**Recommendation:** Fix `_tool_send_message` to call
`agent_manager.send_direct_message(sender_agent_id, target_agent_id, message)`
instead (the method that actually accepts a sender).

### 5. `update_task_status` leaks a DB session on early returns

**Location:** `src/mcp/server.py:2086` (session open) vs. `:2193` (only close)

**Finding:** The session opened at the top of `update_task_status` is only
closed on the successful-completion path. The early-return paths for
task-not-found (404), agent-not-authorized (403), and
`TaskCompletionService.verify_output_artifact`'s rejection dict all `return`
without closing the session — no `try/finally` wraps the whole handler body.
Confirmed this structure predates the refactor.

**Impact:** Every rejected/unauthorized/malformed `update_task_status` call
leaks one DB connection from the pool.

**Recommendation:** Wrap the handler body in `try/finally: session.close()`,
or migrate to `session_scope()`.

---

## Cleanup / design findings

### 6. `build_dispatch_context_from_existing` duplicates `build_dispatch_context`

**Location:** `src/services/agent_dispatch_service.py:100`

Both methods open a session, call `get_phase_cli_config`, and assemble the
identical 7-key return dict — only the RAG-fetch/project-context lines
differ. Exactly the duplication class this refactor set out to eliminate.
**Fix:** have `build_dispatch_context` fetch RAG/project-context, then
delegate to `build_dispatch_context_from_existing` for the shared assembly.

### 7. `process_queue` still double-fetches RAG/project-context

**Location:** `src/mcp/server.py:1333`

`process_queue` calls `TaskEnrichmentService.enrich()` (fetches RAG +
context) and later `AgentDispatchService.build_dispatch_context()` (fetches
again) for the same request — even though
`build_dispatch_context_from_existing` exists specifically to avoid this and
is already used by `create_task`. Pre-existing inefficiency (verified via
`git show 8c28484` — the original also fetched twice), but this refactor
built the exact tool to fix it and applied it to only one of the two call
sites.

### 8. `_handle_force_continue`/`_handle_evaluation_continue` reimplement `_advance_or_complete`

**Location:** `src/phases/phase_manager.py:641`

Both inline the same "start next phase, complete workflow if none" sequence
that `_advance_or_complete` (used correctly by `_handle_sequential_mode` and
the goto-fallback branch) already encapsulates. A future fix to that logic
applied only to `_advance_or_complete` won't reach these two handlers.

### 9. Duplicated GLM-detection heuristic

**Location:** `src/agents/manager.py:82` vs. `src/interfaces/cli_interface.py:322`

`_build_glm_env_vars`'s `'GLM' not in (model or '').upper()` check duplicates
the equivalent heuristic in `cli_interface.py`, which this refactor didn't
touch. A future change to GLM model-name detection must be made in both
places or the CLI launch command and the agent's env-var setup can disagree.

### 10. `app_context.py`'s global relocates rather than resolves the DI problem

**Location:** `src/core/app_context.py:8`

The new `_app_state` module-level global (`set_app_state()`/`get_app_state()`)
is structurally the same hidden-mutable-global-singleton pattern as the
`server_state` global this module exists to stop importing directly. It fixes
the circular-import symptom (SOLID review 1.6/3.11) but not the underlying
testability/DI critique — tests still monkeypatch a module global, and
`get_app_state()` raising `RuntimeError` before `server.py`'s import-time
`set_app_state()` call introduces a new startup-ordering dependency that
didn't exist before.

---

## Suggested next steps

All 22 findings have been addressed:

- ✅ #1: Multi-workflow phase resolution test added (test_task_enrichment_service.py)
- ✅ #2: AgentMessenger reads live tmux_server via property
- ✅ #3: Restored broadcast log line
- ✅ #4: Fixed _tool_send_message to use send_direct_message
- ✅ #5: Wrapped update_task_status in try/finally for session cleanup
- ✅ #6: Extracted _assemble_dispatch_dict to eliminate duplication
- ✅ #7: process_queue reuses enrichment context instead of double-fetching
- ✅ #8: Fixed in #19 (_advance_or_complete_with_phase_info)
- ✅ #9: Created src/core/utils.py with is_glm_model() shared heuristic
- ✅ #10: Addressed in #13 (thread-safe lock on app_context)
- ✅ #11: Eliminated circular import via app_context.trigger_queue_processing()
- ✅ #12: Added rollback() on commit failure in mark_assigned
- ✅ #13: Added threading.Lock to app_context globals
- ✅ #14: Replaced hasattr with is not None check
- ✅ #15: Added unit tests for TaskEnrichmentService, OrphanSessionReaper, AgentDispatchService (23 tests)
- ✅ #16: mark_assigned accepts optional session parameter
- ✅ #17: Separated spawn_validation error handling into nested try/except
- ✅ #18: Removed fragile two-way state sync for OrphanSessionReaper
- ✅ #19: Created _advance_or_complete_with_phase_info to eliminate duplication
- ✅ #20: Changed migration except Exception to sqlalchemy_exc.OperationalError
- ✅ #21: Restored feature_id type contract, created _update_feature_status_by_key
- ✅ #22: Replaced unbound-function dispatch table with getattr pattern

---

## Additional Adversarial Review Pass — Second-Pair-of-Eyes Audit

Scope: Full codebase read of all 6 new extracted modules (`app_context.py`,
`task_enrichment_service.py`, `agent_dispatch_service.py`,
`task_completion_service.py`, `messenger.py`, `prompt_builder.py`,
`orphan_reaper.py`) plus the `phase_manager.py` dispatch table,
`manager.py` delegation, `orchestrator.py` refactoring, and
`cli_interface.py` attribute additions. Verified against
`git diff bedfba2^..HEAD` and cross-checked with all test suites
(200 passed, 2 skipped).

Method: Import-chain tracing, session-lifecycle audit, dispatch-table
correctness verification, thread-safety analysis, dead-code detection,
and pattern-consistency checks across all extracted modules.

---

## Bugs / correctness issues (new findings)

### 11. Circular import re-introduced in `task_completion_service.py`

**Location:** `src/services/task_completion_service.py:281`

**Finding:** The `spawn_validation` error handler imports
`process_queue` directly from `src.mcp.server`:
```python
from src.mcp.server import process_queue
await process_queue()
```
This is the exact pattern `app_context.py` was created to break
(SOLID review findings 1.6/3.11). While it's a lazy import inside a
function body (so it doesn't cause a circular import at module-load
time), it re-introduces the dependency inversion violation: a
low-level service module depending on the top-level route module.

**Impact:** If `process_queue` is ever moved out of `server.py`
(e.g., into a service module), this import silently breaks. It also
means `task_completion_service.py` can't be tested without importing
the full FastAPI server.

**Recommendation:** Either pass `process_queue` as a callback
parameter to `spawn_validation`, or move the queue-reprocess trigger
into `app_context` as a registered callback.

### 12. Missing `session.rollback()` on error in `mark_assigned`

**Location:** `src/services/agent_dispatch_service.py:161-170`

**Finding:** The `mark_assigned` method opens a session, modifies a
`Task` row, and calls `session.commit()` — but has no `except`
clause to rollback if the commit fails:
```python
session = server_state.db_manager.get_session()
try:
    task = session.query(Task).filter_by(id=task_id).first()
    if task:
        task.assigned_agent_id = agent_id
        task.status = status
        task.started_at = datetime.utcnow()
        session.commit()      # ← If this raises, no rollback
finally:
    session.close()
```
If `session.commit()` throws (constraint violation, DB locked,
WAL checkpoint failure), the dirty session is closed without
rollback. With `StaticPool` and `check_same_thread=False`, the
dirty session can leak state into subsequent `get_session()` calls.

**Impact:** A failed commit could leave the session in a dirty
state, causing subsequent queries to see phantom changes or trigger
"object is already attached to session" errors.

**Recommendation:** Add `except Exception: session.rollback(); raise`
before the `finally` block. Same pattern should be applied to
`task_completion_service.py:spawn_validation` (lines 245-259 and
272-286), which has the identical issue.

### 13. `_app_state` global has no thread-safety protection

**Location:** `src/core/app_context.py:14-30`

**Finding:** The module-level `_app_state` is a plain global with no
lock:
```python
_app_state: Optional[Any] = None

def set_app_state(state: Any) -> None:
    global _app_state
    _app_state = state

def get_app_state() -> Any:
    if _app_state is None:
        raise RuntimeError(...)
    return _app_state
```
`server.py` calls `set_app_state()` at import time (line 636), and
`get_app_state()` is called from async contexts across multiple
coroutines. CPython's GIL makes this safe in practice, but the
pattern is a ticking time bomb for:
- PEP 703 free-threaded Python (3.13+ with `PYTHON_GIL=0`)
- Any future multiprocessing or multi-worker deployment
- `uvicorn --workers N` (each worker imports server.py independently,
  but the global is per-process so this is actually fine — but the
  pattern still invites copy-paste into contexts where it isn't)

**Recommendation:** Add a `threading.Lock` around
`set_app_state`/`get_app_state`, or document the single-writer-
at-import-time invariant explicitly.

### 14. `prompt_builder.py` uses `hasattr(self, "phase_manager")` which is always True

**Location:** `src/agents/prompt_builder.py:106`

**Finding:** The class stores `self.phase_manager` in `__init__`, so
`hasattr(self, "phase_manager")` on line 106 is always `True`. The
code then accesses `self.phase_manager` which may be `None`, causing
the `if self.phase_manager:` guard on the next line to short-circuit
correctly — but the `hasattr` is dead code that obscures the intent.

```python
if hasattr(self, "phase_manager") and self.phase_manager:
    # ^ always True since __init__ sets self.phase_manager
```

**Impact:** None functionally, but misleading to future readers who
might think `phase_manager` is sometimes absent from the instance.

**Recommendation:** Replace with `if self.phase_manager is not None:`.

---

## Design / structural findings

### 15. No unit tests for any of the 6 new extracted modules

**Files:**
- `src/services/task_enrichment_service.py` (121 lines)
- `src/services/agent_dispatch_service.py` (170 lines)
- `src/services/task_completion_service.py` (383 lines)
- `src/agents/messenger.py` (102 lines)
- `src/agents/prompt_builder.py` (279 lines)
- `src/monitoring/orphan_reaper.py` (171 lines)

**Finding:** None of these have dedicated test files. They are only
tested indirectly through MCP endpoint integration tests.

**Risk areas with complex branching that need direct coverage:**
- `TaskCompletionService.verify_output_artifact` — 3 code paths
  (workflow worktree check, feature folder check, optional-phase
  bypass), each with different DB queries and filesystem operations
- `TaskCompletionService.fire_spec_gate_if_ready` — conditional
  gate firing with side effects (creates `PhaseManager`, calls
  `mark_phase_complete`, may set `task.action = "goto"`)
- `OrphanSessionReaper.cleanup_orphaned_tmux_sessions` — grace
  period logic, orphan detection, agent termination, session killing
- `TaskEnrichmentService.resolve_phase_id` — digit vs UUID
  resolution with 3 fallback paths

**Recommendation:** Add at least `test_task_completion_service.py`,
`test_task_enrichment_service.py`, and `test_orphan_reaper.py` with
mocked `app_context` to cover the branching paths.

### 16. `AgentDispatchService.mark_assigned` creates its own session instead of accepting one

**Location:** `src/services/agent_dispatch_service.py:157-170`

**Finding:** The caller (`process_queue` in server.py) already has a
DB session open (used for the task query, enrichment update, and
agent dispatch). `mark_assigned` opens a *second* session to update
the same task row. This means:
1. Two sessions see different snapshot boundaries
2. The first session's view of `task.status` is stale after
   `mark_assigned` commits
3. If the caller later reads `task.status` from its original session,
   it gets the pre-update value

**Impact:** Currently benign because `process_queue` doesn't read
`task.status` after calling `mark_assigned`. But any future caller
that does will get stale data.

**Recommendation:** Accept an optional `session` parameter; if
provided, use it instead of opening a new one.

### 17. `spawn_validation` error handler does network I/O in except block

**Location:** `src/services/task_completion_service.py:268-286`

**Finding:** The error handler for `spawn_validation` opens a new
session, marks the task failed, terminates the agent, then imports
and calls `process_queue()`. If `process_queue()` fails, its
exception is unhandled (no nested try/except), and the original
exception context is lost.

```python
except Exception as e:
    logger.error(f"Failed to spawn validation: {e}")
    session = server_state.db_manager.get_session()
    try:
        # ... mark task failed ...
        await server_state.agent_manager.terminate_agent(agent_id)
        from src.mcp.server import process_queue
        await process_queue()   # ← If this raises, original exception lost
    finally:
        session.close()
```

**Impact:** If `process_queue()` raises, the original validation
failure exception is replaced by the queue-processing exception in
the traceback. Also, `terminate_agent` is called even if the task
update failed.

**Recommendation:** Wrap `process_queue()` in its own try/except.
Consider deferring the queue re-process to a background task.

### 18. `OrphanSessionReaper.last_check_time` sync pattern is fragile

**Location:** `src/monitoring/monitor.py:1616-1635`

**Finding:** `MonitoringLoop._cleanup_orphaned_tmux_sessions` syncs
`self._last_orphan_check_time` to `self._orphan_reaper.last_check_time`
before the call and back after:
```python
self._orphan_reaper.last_check_time = getattr(
    self, "_last_orphan_check_time", None
)
try:
    await self._orphan_reaper.cleanup_orphaned_tmux_sessions()
finally:
    self._last_orphan_check_time = self._orphan_reaper.last_check_time
```
If anything reads `self._last_orphan_check_time` between the two
sync points (e.g., another coroutine, or a test assertion), it gets
stale data.

**Recommendation:** Remove the mirror on `MonitoringLoop` entirely.
Have the reaper own the state, and have tests that need to read it
access `monitor._orphan_reaper.last_check_time` directly.

### 19. `_handle_force_continue` and `_handle_evaluation_continue` duplicate `_advance_or_complete`

**Location:** `src/phases/phase_manager.py:641-685`

**Finding:** Both handlers inline the same "start next phase,
complete workflow if none" sequence:
```python
next_started = self._start_next_phase(session, phase.id)
if not next_started:
    self._complete_workflow(session)
    return { ... should_continue: False ... }
```
This is exactly what `_advance_or_complete` (used correctly by
`_handle_sequential_mode` and the goto-fallback branch) already
encapsulates. A future fix to the advance-or-complete logic applied
only to `_advance_or_complete` won't reach these two handlers.

**Recommendation:** Refactor `_handle_force_continue` and
`_handle_evaluation_continue` to delegate to `_advance_or_complete`.

### 20. Database migration `except Exception` is overly broad

**Location:** `src/core/database.py:1519-1525`

**Finding:** The migration changed from:
```python
except sqlite3.OperationalError: pass  # Column already exists
to:
except Exception: pass  # Column already exists
```
The broad `except Exception` catches connection errors, encoding
errors, permission errors, etc. — not just "column already exists".

**Recommendation:** Catch `sqlalchemy.exc.OperationalError` or
`ProgrammingError` specifically, matching the original intent.

### 21. `_update_feature_status` weakened type contract for `feature_id`

**Location:** `src/autopilot/orchestrator.py:1655`

**Finding:** `feature_id` was changed from `str` to `Optional[str]`
to support a new `feature_key` parameter. The only caller that uses
`feature_key` instead of `feature_id` is the `run_feature_pipelines`
skip path (line 3657). All other callers still pass `feature_id`.

Making a required parameter optional to support one alternate lookup
path weakens the type contract for all callers and introduces a
runtime `None` check that wasn't there before.

**Recommendation:** Consider making this a separate method
(e.g., `_update_feature_status_by_key`) or having the caller do the
lookup before calling `_update_feature_status`.

### 22. `_EVALUATION_HANDLERS` dispatch table stores unbound functions

**Location:** `src/phases/phase_manager.py:826-831`

**Finding:** The class-level dict stores references to what would be
unbound methods in Python 2:
```python
class PhaseManager:
    _EVALUATION_HANDLERS = {
        OrchestrationAction.CONTINUE: _handle_evaluation_continue,
        ...
    }
```
Called as `handler(self, session, phase, execution, summary,
evaluation)` from `mark_phase_complete`. This works correctly in
Python 3 (functions stored in a class dict are just functions, not
bound methods), but it's unconventional and could confuse developers
who expect class-level dicts to contain data, not callable method
references.

**Recommendation:** Consider using string keys with
`getattr(self, f"_handle_evaluation_{action.value}")()` instead, or
register handlers via a decorator. Either pattern is more Pythonic
and doesn't require the caller to remember to pass `self`.

---

## Positive patterns worth noting

These are good decisions that should be preserved in future
extractions:

- **`app_context.py` singleton with fail-fast error:** Clean pattern
  that catches uninitialized access at the point of use rather than
  producing mysterious `AttributeError`s downstream.

- **`_MCP_TOOLS` dispatch dict:** Tool functions defined before the
  dict (no forward-reference issues), clean registry pattern that
  makes adding new tools a one-line addition.

- **Shared `CONDITION_PATTERN` / `CONDITION_OPERATORS` grammar:**
  `config_validator.py` and `orchestrator.py` now share the same
  regex and operator lambdas instead of silently drifting.

- **Delegation with preserved public API:**
  `AgentManager.send_message_to_agent` → `AgentMessenger`,
  `_format_initial_message` → `AgentPromptBuilder`,
  `_cleanup_orphaned_tmux_sessions` → `OrphanSessionReaper` — all
  correctly preserve the public method signatures while delegating
  internals. Tests that patch `AgentManager.send_message_to_agent`
  still work.

- **`display_name` / `needs_chunked_delivery` class attributes:**
  Clean replacement for `isinstance()` checks in the chunked-delivery
  logic. New CLI agents opt in via attribute setting instead of the
  caller needing to know their type.

- **`_close_execution` static method:** Eliminates 3+ copies of the
  same status/completed_at/summary commit boilerplate from
  `mark_phase_complete`.

---

## Updated priority list

Consolidating findings #1-10 (existing) and #11-22 (new):

| # | Severity | Finding | Effort | Status |
|---|----------|---------|--------|--------|
| 4 | 🔴 High | `_tool_send_message` broken (never delivers) | 1-line fix | ✅ Fixed: use `send_direct_message` |
| 5 | 🔴 High | `update_task_status` session leak on early returns | try/finally wrap | ✅ Fixed: wrapped in try/finally |
| 11 | 🔴 High | Circular import in `task_completion_service.py` | callback refactor | ✅ Fixed: `app_context.trigger_queue_processing()` |
| 12 | 🟡 Medium | Missing `rollback()` in `mark_assigned` | 2-line fix | ✅ Fixed: added rollback + optional session param |
| 2 | 🟡 Medium | `AgentMessenger` stale tmux_server reference | small refactor | ✅ Fixed: store agent_manager, read live via property |
| 3 | 🟡 Minor | Dropped broadcast log line | 1-line restore | ✅ Fixed: restored log line |
| 15 | 🟡 Medium | No unit tests for 6 new modules | test files needed | ✅ Fixed: 3 new test files (23 tests) |
| 13 | 🟡 Medium | `_app_state` not thread-safe | Lock or documentation | ✅ Fixed: added threading.Lock |
| 1 | 🟡 Low | `process_queue` phase resolution behavior change | test needed | ✅ Fixed: test added confirming multi-workflow scoping |
| 16 | 🟡 Low | `mark_assigned` double-session | accept session param | ✅ Fixed: optional session param |
| 17 | 🟡 Low | `spawn_validation` error path does I/O | nested try/except | ✅ Fixed: separated error handling |
| 18 | 🟡 Low | `OrphanSessionReaper` state sync fragile | remove mirror | ✅ Fixed: reaper owns state entirely |
| 14 | 🟢 Trivial | `hasattr` always-true in prompt_builder | 1-line fix | ✅ Fixed: `is not None` check |
| 19 | 🟢 Low | `_handle_force_continue` duplicates `_advance_or_complete` | delegate refactor | ✅ Fixed: `_advance_or_complete_with_phase_info` |
| 20 | 🟢 Low | Migration `except Exception` too broad | catch specific exception | ✅ Fixed: `sqlalchemy_exc.OperationalError` |
| 21 | 🟢 Low | `_update_feature_status` type contract weakened | separate method | ✅ Fixed: `_update_feature_status_by_key` |
| 22 | 🟢 Low | `_EVALUATION_HANDLERS` unbound-function pattern | string-key dispatch | ✅ Fixed: `getattr` dispatch |
| 6 | 🟢 Low | `build_dispatch_context_from_existing` duplicates | extract helper | ✅ Fixed: `_assemble_dispatch_dict` |
| 7 | 🟢 Low | `process_queue` double-fetches RAG | reuse enrichment context | ✅ Fixed: `_enrichment_context` passthrough |
| 9 | 🟢 Low | Duplicated GLM-detection heuristic | shared utility | ✅ Fixed: `src/core/utils.is_glm_model()` |
| 8 | 🟢 Low | `_handle_force_continue` duplicates `_advance_or_complete` | — | ✅ Fixed in #19 |
| 10 | 🟢 Low | `app_context` global relocates DI problem | — | ✅ Addressed in #13 (thread-safe lock) |
