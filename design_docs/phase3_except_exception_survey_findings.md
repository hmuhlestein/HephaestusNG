# Phase 3 — `except Exception` / manual-session survey findings

**Date:** 2026-08-19 to 2026-08-20 — **all 5 themes / 23 findings fixed.**
**Scope:** SOLID_OO_REVIEW_UPDATE_2026-08-19.md priority #3 ("the single highest-leverage
remaining structural gap") — findings 1.13 (broad `except Exception`, 141+ across
`server/`+`autopilot/`+`frontend/`, 700+ codebase-wide) and 1.15 (manual `get_session()`
vs. `session_scope()`).

## Method

This finding is too large to fix wholesale — a blanket rewrite of exception handling
across 700+ sites would be enormous, high-risk, and wouldn't close the real architectural
gap (no service layer), which needs its own much larger effort. Instead, every real bug
found elsewhere in this refactor has had the same shape: an `except Exception` block
silently swallows an error that should have surfaced a genuine state-consistency problem.
So the survey specifically hunted for that shape, not style violations.

Three parallel agents surveyed disjoint parts of `src/`:
- Agent 1: `src/mcp/server/` + `src/mcp/autopilot/`
- Agent 2: `src/autopilot/orchestrator/`
- Agent 3: everything else (`agents/`, `monitoring/`, `services/`, `core/`, `phases/`,
  `workflow/`, `workflow_engine/`, `validation/`, `cli/`, `sdk/`, `interfaces/`, `auth/`)

Each was told to skip genuinely defensive catches (best-effort broadcasts, re-raises,
documented-safe swallows) and report only sites where a concrete failure scenario exists:
"if X happens, then Y silently breaks."

## Theme A — leaked DB sessions — FIXED 2026-08-19

All 5 sites missing `try/finally` (or `try/except/finally`) around a manually-obtained
`db_manager.get_session()`, where a mid-transaction failure would leak a connection
holding a failed, uncommitted transaction:

1. `src/mcp/server/_create_task_steps.py` — `_persist_new_task` (no try/finally at all)
2. `src/mcp/server/_create_task_steps.py` — `_resolve_phase_and_enrich` (read-only query, no finally)
3. `src/mcp/server/_create_task_steps.py` — `_check_for_duplicate_task` (leaked inside an outer except)
4. `src/mcp/server/_create_task_steps.py` — `_handle_task_processing_failure` (the failure-recovery
   path itself — per an existing comment in the same file, `_apply_enrichment_to_task`'s docstring
   names this function as the likely victim of the exact leak class it was just fixed for)
5. `src/agents/launch_pipeline.py` — `create_agent_for_task`'s stub-Agent-row block (hot,
   per-dispatch-call path)

Verified: 56 targeted tests pass (`test_agent_manager.py`, `test_create_task_guards.py`,
`tests/integration/test_task_deduplication_flow.py`, `test_server_dispatch_endpoints.py`).
Zero behavior change on the success path — only the failure path changed (session now
rolls back and closes instead of leaking).

**Gap-check follow-up (2026-08-20):** added dedicated regression tests for 4 of the 5
sites to `tests/test_apply_enrichment_to_task_session_leak.py` (which already covered
`_apply_enrichment_to_task`'s identical fix from a prior phase, and whose own docstring
names `_handle_task_processing_failure` as the second half of that same documented
incident): `_persist_new_task`, `_check_for_duplicate_task`, `_handle_task_processing_failure`,
`_resolve_phase_and_enrich`. Each mocks a `Session.commit`/`Session.query` failure and
asserts `rollback()`/`close()` are called exactly once, following the existing file's
established pattern. `_check_for_duplicate_task`'s test also asserts the outer
resilience behavior (degrade to "continue without dedup") is preserved unchanged.
`_handle_task_processing_failure`'s test asserts it does NOT re-raise (a deliberate,
reasoned design choice — it's the last line of defense in a fire-and-forget background
task with nothing above it to catch a re-raise, so swallowing-with-a-structured-log is
strictly more visible than the alternative: an unhandled-task-exception warning via
asyncio's default handler, bypassing the application's own logger).

The 5th site, `launch_pipeline.py`'s `create_agent_for_task` stub-Agent-row block, was
consciously left without a dedicated failure-injection test: isolating just that block
would require either extensive mocking of the surrounding 200+ line method's
preconditions (duplicate-agent check, `git_commit_push` review-mode guard, CLI/phase
config resolution) for a low-value, fragile test, or an unrequested extraction refactor
beyond this fix's scope. The success path is already exercised by 33 references across
`test_agent_manager.py` (all passing); the failure-path cleanup fix itself is a direct
analog of the other 4, verified correct by inspection (same try/except/finally shape).

## Themes B through E — all fixed 2026-08-19 to 2026-08-20

Grouped by theme, worst first within each. File:line citations were current as of the
original survey date and have since drifted (the codebase moves fast, multiple concurrent
sessions) — each entry below is updated with what was actually fixed, in file-level terms
rather than exact line numbers.

### Theme B — transient error silently treated as a definitive negative, triggering a destructive action — FIXED 2026-08-20

The clearest "real bug" pattern — matches every other live bug found this refactor.
Each site needed individual judgment on the right failure-mode behavior, not a
mechanical fix.

- **`src/mcp/autopilot/control_routes.py`** (`_start_pipeline_reserved`, zombie-pipeline
  check) — a DB read error during the zombie check was treated the same as a confirmed
  zombie, stopping a healthy, actively-running pipeline and killing in-flight agent work.
  **Fixed**: the failure path now raises `HTTPException(409, "already running")`, matching
  the existing non-zombie branch's behavior, instead of calling `service.stop()`. New
  regression test: `test_zombie_check_db_failure_fails_conservative_not_destructive` in
  `tests/test_autopilot_api_helpers.py`.
- **`src/autopilot/orchestrator/engine_client.py`** (`get_tasks`), feeding
  `__init__.py`'s `run_single_workflow` poll loop — a transient DB error on one status
  query got silently treated as "no tasks exist," which after 300s elapsed permanently
  killed a healthy, actively-progressing workflow with a misleading "No tasks exist"
  error hiding the real (swallowed) cause. Changing `get_tasks()`'s own contract was
  ruled out as too invasive (38 call sites across the codebase). **Fixed at the decision
  point instead**: the "no tasks exist" HARD_ERROR verdict now requires the same
  all-empty condition on 2 consecutive polls (a `no_tasks_streak` counter, reset the
  moment any list is non-empty) before acting, matching this exact file's own
  established pattern for the identical false-positive class
  (`STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS`). Verified by inspection + 254 targeted
  tests (`test_orchestrator_helpers.py`); no dedicated failure-injection test — isolating
  this specific branch would require mocking the entire poll-loop machinery in a
  massive, side-effect-heavy `while True` loop, the same cost/complexity tradeoff
  already documented for Theme A's `launch_pipeline.py` site.
- **`src/autopilot/orchestrator/queue.py`** (`_has_resumable_active_design`) — investigated
  further than the original survey suggested: this function's OWN exception handling
  (return `False` on error) actually degrades safely in isolation at both its direct call
  sites in `run_continuous_pipeline` (both correctly fall back to a more conservative
  "wait" or "re-verify" path on `False`, traced line-by-line). **The real destructive path
  was the OUTER `except Exception as e: logger.warning(...)` in `__init__.py`**, wrapping
  the entire protective-gating section (`still_blocking` check, both
  `_has_resumable_active_design` calls, and `current_workflow_id` verification) — ANY
  failure inside that whole section fell through unconditionally to `pick_next_design()`
  with every protection bypassed, and `run_single_workflow`'s default
  `pause_existing=True` terminates every other active workflow's agents project-wide.
  **Fixed**: the except handler now sleeps and `continue`s (skips this scan cycle)
  instead of falling through, matching the "wait and retry" pattern already used
  elsewhere in the same loop. Verified by inspection + 254 targeted tests.
- **`src/autopilot/orchestrator/__init__.py`** (`_should_pause_for_review`) — on any DB
  exception, silently returned `False`, fail-open across all 5 of its call sites
  (Phase-0 decomposition review, feature-completion review, and 2 more), silently
  skipping a human-approval gate the project was configured to require, with no
  independent backstop for most of them (only the `git_commit_push` manual-only path is
  separately backstopped, by `launch_pipeline.py`). **Fixed**: now fails safe (`True`)
  instead — traced all 5 call sites individually to confirm each one routes a
  wrongly-`True` result into a normal, human-clearable "paused for review" wait, never
  anything destructive or irreversible. New regression tests:
  `TestShouldPauseForReview` (3 tests) in `tests/test_orchestrator_helpers.py`.

### Theme C — data-loss/corruption risk (standalone, most severe) — FIXED 2026-08-20

Each was a distinct root cause, fixed individually.

- **`src/core/worktree_manager.py:994-998`** (`cleanup_all_stale_branches`) — a failed
  branch checkout was swallowed (`except Exception: pass`), then the code proceeded to
  merge into whatever branch happened to actually be checked out and deleted the source
  branch on "success" — real, silent branch corruption/data loss. The *same* git
  operation elsewhere in this file (`merge_to_main`, line ~500-501) was already left
  unguarded so it fails loudly — this site was an inconsistency with the file's own
  established pattern, not a deliberate design choice. **Fixed**: removed the swallow, so
  a checkout failure now aborts the whole cleanup pass instead of silently proceeding on
  the wrong branch. Both call sites already handle a raised exception correctly
  (`control_routes.py`'s route has its own `except Exception` → `HTTPException(500, ...)`;
  `queue_routes.py`'s background thread just logs to stderr on an uncaught exception).
  146 targeted tests pass.
- **`src/autopilot/orchestrator/engine_client.py:62-73`** (`update_task_status`) +
  **`src/autopilot/orchestrator/phase_transitions.py:373`** — the status-reset-to-"pending"
  call swallows all exceptions and returns `False`, but its only caller
  (`_maybe_retry_failed_tasks`) didn't check the return value before dispatching a new
  agent on the next line — risking a live agent working on a task whose `Task.status` is
  still `"failed"` in the DB, with every downstream consistency check keying off that
  stale status. **Fixed**: the call site now raises on a `False` return, routing the
  failure into the same `except Exception` block that already exists a few lines below
  (which correctly logs and reverts the task to `"failed"` for a later retry pass) —
  reused the existing recovery path instead of building a new one. 366 targeted tests
  pass.
- **`src/monitoring/health_audit.py:184-202`** — a stuck task promoted to `"done"` had a
  "fire spec gate for gated phases" block immediately after, wrapped in a swallow.
  **Reachability check confirmed this block was genuinely dead code**: it re-checked
  `_phase.name in GATED_PHASES`, an identical condition already established `False` to
  even reach this branch (it's nested inside the `else` of `if is_gated:`, computed a few
  lines above from the same `task.phase_id` with no intervening writes) — so
  `pm.mark_phase_complete(...)` could never execute. Not a live bug: the *actual* design
  is that a gated phase's stuck task takes the sibling `if is_gated:` branch instead,
  marking it `"failed"` specifically so a proper re-run goes through real gate validation
  rather than this heuristic short-circuiting it — exactly right, and unrelated to the
  dead block. **Fixed by removing the dead block** (and its now-orphaned `Workflow`
  import), replaced with a comment explaining why no gate-firing belongs here, so a
  future reader doesn't reintroduce the same confusion. No test coverage possible or
  needed for a removal of genuinely-unreachable code; 138 targeted tests confirm no
  behavior change elsewhere in the file.

### Theme D — silent no-op / fictitious success reported to the caller — FIXED 2026-08-20

Mechanical, low risk, similar shape to Theme A but about caller-visible correctness
rather than session hygiene.

- **`src/mcp/autopilot/message_routes.py`** (`unarchive_message`, `unarchive_all_messages`,
  `cleanup_old_archives`) — each swallowed a write failure *inside* a `with get_db() as
  db:` block without calling `db.rollback()` first, so the outer context manager's own
  `commit()` could run against a session left in "pending rollback" state, and the
  endpoint unconditionally reported success regardless. **Fixed**: removed the inner
  `try/except: pass` entirely, matching the sibling `archive_message` endpoint right
  above it in the same file, which never wrapped its own mutation this way — lets a
  genuine failure propagate naturally to FastAPI's default 500 handling, and `get_db()`'s
  own except clause correctly rolls back before re-raising.
- **`src/mcp/server/_mcp_tool_registry.py`** (`_tool_send_message`) — swallowed and
  unconditionally returned `{"success": True, ...}` for a targeted agent-to-agent message
  the caller may depend on for coordination (distinct from the broadcast variant, which
  is legitimately best-effort). **Fixed**: raises `HTTPException(500, ...)` on failure,
  matching this same function's own existing validation-error pattern one branch above.
- **`src/cli/commands/workflow.py`** (`heph workflow stop <id>`, single-workflow path) —
  per-agent `terminate_agent` calls and the whole discovery+termination loop were wrapped
  in bare `except Exception: pass` with no printed warning, while the sibling `--all` path
  in the same file already prints per-agent status. **Fixed**: matched the `--all` path's
  convention, printing a warning on failure instead of swallowing silently.
- **`src/sdk/client.py`** (`_register_workflow_definitions`, called from `start()`) — zero
  logging at any level; a failed registration meant `start_workflow(definition_id=...)`
  later failed with an opaque "not found," no trace back to the real cause. **Fixed**:
  added `logger.error(...)` naming the specific definition and cause. Also fixed the two
  lower-confidence read paths in the same file, `get_tasks`/`list_workflow_executions`
  (swallow and return `[]`, indistinguishable from "genuinely empty"): added logging
  without changing the return contract, since nothing is mutated and a caller-visible
  behavior change wasn't warranted for a read path.

### Theme E — real failures logged at `debug` (invisible in production) — FIXED 2026-08-20

Mostly mechanical (bump the log level); one site (`verification.py`) needed a real
fail-closed-vs-fail-open judgment call, not just a log-level change.

- **`src/mcp/server/_shared.py`** (`_resolve_agent_current_phase`) — a DB failure here
  re-surfaced as a misleading client-input-validation 400 ("supply phase_id explicitly")
  instead of a visible 500/DB failure. **Fixed**: bumped `debug` → `warning`.
- **`src/autopilot/orchestrator/__init__.py`** (`_clean_stale_assigned_tasks`'s caller) —
  this periodic self-heal call's own failures were demoted to debug, so a persistent
  failure would go unnoticed until the stale-task symptom was investigated separately.
  **Fixed**: bumped `debug` → `warning`.
- **`src/agents/terminator.py`** — `collect_task_cost` failure during agent termination
  logged only at debug; billing/cost data for that run silently lost. **Fixed**: bumped
  `debug` → `warning`.
- **`src/mcp/autopilot/intervention_routes.py`** (`_find_pending_input`,
  `get_human_input_request`) — a malformed-but-not-yet-stale pending-input file was
  silently skipped with zero logging (not even debug), so the UI never showed the pending
  question and the orchestrator stayed blocked until the stale-timeout eventually cleaned
  it up. **Fixed**: added `logger.warning(...)` to both swallow sites.
- **`src/services/task_completion/verification.py`** (`verify_output_artifact`) — the
  mandated check that a `security_review` phase's report contains its required
  "Automated Scan Results" section was wrapped in a bare `except Exception: pass`; an I/O
  hiccup silently skipped a security-relevant validation entirely. **This one needed more
  than a log bump**: fixed to fail closed, not open — a read failure now adds to
  `invalid_frontmatter` (rejecting the phase completion) instead of silently passing,
  matching this same function's own established philosophy for every other
  "couldn't-verify" case in the file (its own comment a few lines above explicitly
  describes rejecting clearly rather than deferring to a later, more confusing failure).
  New regression test: `test_security_review_ash_scan_check_fails_closed_on_a_read_error`
  in `tests/test_task_completion_service.py`.
- **`src/mcp/server/_create_task_steps.py:327-333`** — already fixed as part of Theme A
  (`_resolve_phase_and_enrich`'s working-directory lookup); listed here only because the
  original survey also flagged its debug-adjacent silence.

## Not flagged (confirmed safe, for context)

All three agents explicitly ruled out the large majority of `except Exception`/manual-
session sites in their scope as genuinely defensive and already correct: best-effort
broadcasts, idempotent migrations, JSON-parse-and-skip loops, multi-step teardown
sequences where each step is independently logged by design, and manual sessions that
correctly wrap in `try/finally` with proper commit/rollback. `background_loops.py`'s
phase-advancement sweep, `_update_task_status_steps.py` (the core task-completion path),
and everything in `src/workflow/`, `src/workflow_engine/`, `src/validation/`,
`src/interfaces/`, `src/auth/` came back clean.
