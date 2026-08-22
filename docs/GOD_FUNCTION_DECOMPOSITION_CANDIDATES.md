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
