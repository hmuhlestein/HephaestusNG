# God-Function Decomposition Candidates

Found 2026-08-22 while gap-reviewing `docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md`.
Distinct from that document's file-size findings: every oversized *file* this
refactor has found so far (`project_routes.py`, `feature_routes.py`,
`_mcp_tool_registry.py`, `task_admin_routes.py`) turned out to be either
genuinely mixed concerns (split) or a single concern that just doesn't
decompose along file boundaries (`_mcp_tool_registry.py`'s registry+handlers
circular-import constraint; `task_admin_routes.py`'s one oversized function).
This document is the second half of that pattern, generalized: the largest
remaining files in the repo, checked for whether their size comes from
*mixed concerns* (a file problem) or *one function doing too much* (a
function problem) — via `ast`, not by reading line counts alone.

**Method:** for each of the four largest files not yet reviewed this pass
(`launch_pipeline.py`, `spec.py`, `mechanical_recovery.py`,
`phase_transitions.py`), parsed top-level functions/class methods and sorted
by size. A file whose size is one or two outlying functions is a
god-*function* problem — splitting the file into siblings doesn't fix
anything, since the actual complexity stays in one place; the fix is
decomposing that function into named steps, the pattern already established
in this codebase for `update_task_status`
(`src/mcp/server/_update_task_status_steps.py`, "verbatim-logic extraction of
one section of the original body — behavior-preserving, not a rewrite").

None of the three below have been decomposed yet. Nothing in this document
has been attempted — it's a scoped, evidence-based backlog, not a plan.

---

## Status: executed 2026-08-22 (3 of 5+2 items done, proven by tests)

Executed in one session following each entry's own "characterization
tests first, then verbatim extraction" guidance. All extracted code is
byte-identical to the original at its original indentation (verified by
script against pre-extraction snapshots); the only deltas are the
`self` → explicit-parameter renames and early-`return` → return-value
conversions documented in each step function's docstring.

**Independently re-verified, 2026-08-22** — every line-count and test-count
claim above checked out exactly against the actual code (534→250, 491→86,
409→232/385→319/247→129, 20/49/166/223/745 test counts all matched or beat
what was claimed); the except/fallback block in `create_agent_for_task`
confirmed to have zero diff lines, not just claimed unchanged; the lazy-
import lesson for `_trigger_arbitration` confirmed genuinely applied. One
gap found: `ruff` caught 4 leftover/redundant imports the extraction left
behind (`DESIGN_CONTEXT_SUBDIR`/`GOTO_REASON_PREFIX` no longer used in
their original files, a stray `import uuid` shadowed by a later local
import, `get_max_task_retries` redundantly re-imported locally after
already being imported at module level in `_phase_case_steps.py`) — fixed
via `ruff --fix`, re-verified compile + 241 tests green. Also fixed
`mechanical_recovery.py`'s `new_agent` unused-variable warning (pre-existing
at HEAD, not introduced by this extraction) by adding the same
"[CONTEXT-OVERFLOW] Fallback agent {id} created" log line its two sibling
call sites (session-limit, connection-error) already have — a real,
pre-existing observability gap, not just lint noise.

**#1 `create_agent_for_task` — DONE (534 → 250 lines).**
`src/agents/_create_agent_for_task_steps.py` holds five named steps
(`_phase_sibling_guard`, `_insert_stub_agent_row`,
`_run_launch_preparations`, `_prepare_tmux_and_prompt`,
`_send_launch_command_and_record_agent`, `_deliver_initial_prompt_flow`;
the fallback/cleanup failure path deliberately stayed in the orchestrator
because its `"tmux_session" in locals()` checks depend on the orchestrator's
own binding scope). Proof: `tests/test_agent_manager.py` — 18
characterization tests pass identically before and after extraction,
including the pre-existing `assign_to_task` same-commit race regression
(simulated mid-dispatch process kill); two guard tests (duplicate-active
agent, phase-sibling skip) were added and verified against the
pre-extraction code first. 735 further dispatch/monitor tests green after.

**#2 `mechanical_recovery_for_agent` — DONE (492 → 86 lines).**
Five sections became named class methods on `MechanicalRecoveryDetector`
(`_check_spend_or_session_limit`, `_update_stuck_signature`,
`_check_context_overflow`, `_attempt_recovery_nudge`,
`_abandon_exhausted_agent`); the orchestrator keeps the check ORDER and the
frozen-state threading. The doc's "ordered list of (check_fn, action) pairs"
idea was not used — the sections have non-uniform early-return semantics
and the order is itself load-bearing, so a data-driven loop would have been
a rewrite. Proof: `tests/test_monitor.py` +
`tests/test_mechanical_recovery_offloading.py` 164/164 before; 166/166
after, including two new firing-order characterization tests
(session-limit beats frozen-nudge; context-overflow beats frozen-nudge),
each verified against the pre-extraction code first.

**#3 `phase_transitions.py` — 3 of 5 passes DONE; 2 deferred.**
New sibling `src/autopilot/orchestrator/_phase_case_steps.py`:
- `_case_in_progress_complete`: 409 → 232
  (`_mark_orphaned_and_stale_pending_tasks_failed`,
  `_retry_failed_tasks_with_done` extracted)
- `_create_phase_task`: 385 → 319
  (`_review_run_cap_and_findings`, `_build_phase_task` extracted)
- `fire_spec_gate_if_ready`: 247 → 129
  (`_handle_spec_gate_result` extracted — its four-action dispatch)

  Deferred: `_retry_failed_tasks` (316) and `_maybe_retry_failed_tasks`
  (270). Rationale: they call each other, `_retry_failed_tasks` grew ~120
  lines in commit `29f6b99` (ticket-blocked routing) while a concurrent
  session was actively working this file, and a clean extraction wants a
calm version of both. Proof for the three done passes:
`tests/test_advance_phases.py`, `tests/test_phase_manager.py`,
`tests/test_phase_transitions_spec_gate.py`,
`tests/test_phase_advancement_sweep.py`,
`tests/test_attempt_recovery_strategies.py`, `tests/test_goto_reconvergence.py`,
`tests/test_update_task_status_ordering.py` — 223/223 after extraction,
identical to the pre-extraction baseline; plus a 532-test
orchestrator/monitor wide sweep green.

  One lesson recorded for the deferred passes: step functions that call
sibling functions defined in `phase_transitions.py` must import them
lazily FROM `phase_transitions` (not from their definition modules) so
tests' `patch("...phase_transitions.<name>")` targets keep resolving —
importing `_trigger_arbitration` from `arbitration` instead let the real
function run against a mocked test and SQLite locked up.

---

## 1. `launch_pipeline.py::LaunchPipeline.create_agent_for_task` — 534 lines

**Location:** `src/agents/launch_pipeline.py:1663-2196` (file is 2433 lines
total — this one method is 22% of it).

**Shape:** 14 parameters, an extensive docstring already documenting a real
race it closes (`assign_to_task`'s same-commit task-assignment fix, "closes a
real race... observed live: exactly this sequence orphaned a task's
assigned_agent_id forever"). Internal sections, by their own comments: the
git_expert review-mode dispatch note, a phase-sibling-active guard, a stub
`Agent` row insert wrapped in try/except/finally specifically because "this
is a hot, per-dispatch-call path" where a bare commit failure used to leak a
DB connection, then worktree creation, tmux session creation, and prompt
dispatch. Each of these is a distinct concern with its own already-documented
failure history — exactly the shape `_update_task_status_steps.py` extracted
from `update_task_status`.

**Why this one, not the file:** `LaunchPipeline` is a coherent single
responsibility (launching agents); the other 42 methods in the class are
reasonably sized (next-largest is `restart_agent` at 235 lines). Splitting
the file would just move the same 534-line method into a new file.

**Proposed next step:** characterization tests first (this method's own
docstring names a live race it fixes — any extraction has to prove that race
stays closed), then extract into `src/agents/_create_agent_for_task_steps.py`
following `_update_task_status_steps.py`'s pattern: one function per section
(stub-agent-insert, worktree-resolution, tmux-session-creation,
prompt-dispatch), `create_agent_for_task` itself becomes the orchestrator
calling them in sequence.

---

## 2. `mechanical_recovery.py::MechanicalRecoveryDetector.mechanical_recovery_for_agent` — 491 lines

**Location:** `src/monitoring/mechanical_recovery.py:110-600` (file is 2011
lines total — this one method is 24% of it).

**Shape:** the dispatcher that runs every individual `detect_*`/`verify_*`
check this class defines (`detect_cli_model_fallback`,
`detect_connection_errors`, `detect_unconfirmed_task_completion`,
`detect_mcp_disconnected`, `detect_agent_never_started`,
`detect_repetition_loop`, etc. — 19 methods total in the class, the next-
largest already a reasonable 265 lines). The individual detectors are
correctly separated; `mechanical_recovery_for_agent` is the one place that
sequences all of them against one agent's current pane state, and per
`docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md` §"Effort B" is also where the new
`_within_resume_replay_grace` guard (commit `e059c17`) got wired into three
of the highest-blast-radius checks — meaning every future false-positive fix
in this territory has been landing inside this same 491-line function,
compounding the problem rather than a stable interface absorbing it.

**Why this one, not the file:** same shape as #1 — one class, one real
responsibility (mechanical recovery), 18 reasonably-sized sibling methods,
one method carrying the sequencing logic for all of them.

**Proposed next step:** the individual `detect_*` methods are likely already
extractable as-is (they're independently callable, self-contained checks);
the actual decomposition target is `mechanical_recovery_for_agent`'s own
sequencing/dispatch logic — plausibly a small ordered list of
`(check_fn, recovery_action)` pairs iterated in a loop, similar in spirit to
this project's own history of replacing hardcoded if/elif detector chains
with list iteration elsewhere (SOLID_OO_REVIEW_UPDATE_2026-08-19's item 3.5).
Needs characterization tests per detector's current firing order first —
this function's *order* of checks is almost certainly load-bearing (an agent
matching two failure patterns at once should hit whichever this function
checks first today), and that order isn't visible from any single detector's
own code.

---

## 3. `phase_transitions.py` — five functions over 200 lines, not one outlier

**Location:** `src/autopilot/orchestrator/phase_transitions.py`, currently
**3375 lines** (the largest file in the repo — see
`docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md` §4.13 for why `pipeline.py`, the
previous largest, was left at ~3000 lines rather than fully decomposed; this
file has already had `arbitration.py`, `config.py`, `human_escalation.py`,
`agent_registration.py`, and `runtime_registries.py` extracted from it or its
sibling `orchestrator/__init__.py` in earlier passes and is *still* the
biggest file after all of that).

| Function | Lines |
|---|---|
| `_case_in_progress_complete` | 409 |
| `_create_phase_task` | 385 |
| `_retry_failed_tasks` | 305 (grew ~120 lines today — commit `29f6b99`'s ticket-blocked-routing fix landed here) |
| `_maybe_retry_failed_tasks` | 251 |
| `fire_spec_gate_if_ready` | 247 |

**Why this is different from #1/#2:** not one outlier absorbing new fixes —
five separate functions, each already large before today, each a plausible
next place a bug-fix session adds another 50-100 lines (as `_retry_failed_tasks`
just did). This is the same "recurring bug-fix landing zone" pattern
`_update_task_status`, `create_agent_for_task`, and
`mechanical_recovery_for_agent` all share, just spread across five functions
in one file instead of concentrated in one.

**Proposed next step:** this is the largest, riskiest item in this document
— five separate characterization-then-extract efforts, in the file this
refactor has already partially decomposed twice. Not a first pick; sequence
after #1/#2 prove the pattern works cleanly on a single function each. If
picked up, treat each of the five as its own independent
characterize-then-extract pass (matching this plan's own "size XL, sequence
near the end" treatment of comparably large past items), not one combined
effort — they don't obviously share sub-steps with each other the way
`create_agent_for_task`'s internal sections do.

---

## Not flagged: `spec.py`

**Location:** `src/autopilot/spec.py`, 2281 lines, 40 top-level functions.
Checked and *not* included above: no single function is an outlier (`score_qa`
at 162 lines is the largest, reasonable for a scoring function evaluating
multiple independent checks). This file's size comes from having many
distinct, individually-reasonable concerns — output-path resolution,
per-phase scoring, artifact consumption, independent test verification — not
from any one function or god-object. Lower priority than #1-#3; if it's ever
worth revisiting, the question is whether those concerns warrant separate
modules (a file-level split, like `project_routes.py`), not a function
decomposition.
