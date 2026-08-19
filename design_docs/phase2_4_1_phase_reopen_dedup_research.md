# §4.1's 4th copy-family — phase-reopen dedup research

Research only, no code changes. Covers `docs/AUTOPILOT_REFACTOR_PLAN.md`
§4.1's "still open" note: the phase-reopen claim/status reset hand-copied
at 4 sites, deliberately left unconsolidated because merging them looked
like a design decision about phase-reopen semantics, not mechanical dedup.
This doc traces all 4 sites in full and proposes a way to dedup the part
that's actually safe to dedup, without touching the part that isn't.

## The 4 sites, read in full

| Site | Guard | `status` written | `task_creation_claimed_at` | `started_at` |
|---|---|---|---|---|
| `phase_manager.py:883-892` (`_handle_evaluation_retry`) | none (caller already knows: this is the just-evaluated phase's own execution) | `"pending"` | `None` | `None` (cleared) |
| `phase_manager.py:1010-1035` (`_handle_evaluation_arbitrate`) | none | `"in_progress"` (deliberately not `"pending"` — see below) | `None` | untouched |
| `phase_manager.py:1588-1603` (`_start_next_phase`) | `execution.status in ("pending", "completed")` | `"in_progress"` | `None` | `datetime.utcnow()` |
| `task_admin_routes.py:599-619` (`restart_task_endpoint`) | `task.workflow_id` truthy, then `execution.status != "in_progress"` | `"in_progress"` | `None` | untouched |

`task_creation_claimed_at = None` is the one field every site agrees on,
unconditionally, every time. `status` and `started_at` genuinely differ —
not by oversight, but by documented, load-bearing reasoning:

- `_handle_evaluation_arbitrate`'s comment (`phase_manager.py:1019-1030`)
  explains *why* it must be `"in_progress"` and not `"pending"`:
  `_case_completed_with_successor` picks its target by "next pending phase
  with order > the latest COMPLETED phase's order," not "next phase in
  pipeline order." A phase reopened as `"pending"` while later phases are
  already completed is invisible to that ordering logic and gets skipped
  over. This is a real, previously-debugged invariant, not an arbitrary
  choice — collapsing it into a shared "always pending" default would
  reintroduce the exact bug this comment documents fixing.
- `_start_next_phase` stamps `started_at=utcnow()` because it is
  transitioning a phase from "not yet running" to "running, for the first
  time this cycle." `_handle_evaluation_retry` clears it to `None` because
  a retry cycle's own duration should start fresh from whenever the retry
  task actually gets picked up, not from the original attempt's start.
  `_handle_evaluation_arbitrate`/`restart_task_endpoint` leave it
  untouched because in both cases the phase was already running
  (arbitration fires mid-phase on the same execution; restart reopens a
  task under a phase execution that, per its own guard, might already be
  effectively active) — resetting `started_at` here would understate how
  long the phase has actually been open.
- The two guards (`status in ("pending", "completed")` vs.
  `status != "in_progress"`) are not the same condition — `restart_task`'s
  is broader, also reopening a `"skipped"` or `"failed"` execution, which
  `_start_next_phase`'s guard would refuse to touch. `_handle_evaluation_retry`/
  `_handle_evaluation_arbitrate` have no guard at all because their caller
  (the orchestrator's evaluation-decision dispatch) already guarantees the
  execution is in a definite, known state before either handler runs.

## What's actually safe to dedup, and what isn't

The **write** (which fields get set, together, atomically) is the
duplicated part, and it's genuinely the same shape at all 4 sites: a
2-or-3-field tuple `(status, task_creation_claimed_at=None, started_at)`.
This is exactly what §4.1's own already-shipped consolidation
(`_clear_stale_task_creation_claim(db, phase_id, *, repair_status: bool =
True)`, `phase_transitions.py:54`) did for a structurally identical
problem in the same item: 2 near-identical writes shared a fast merge, a
3rd did meaningfully more, resolved by giving the 3rd behavior an explicit
named flag rather than silently defaulting to either extreme.

The **guard** (when to reopen at all) and the **semantic reasoning behind
each status/started_at choice** are not duplicated logic — they're 4
independently-justified decisions, 2 of them citing specific historical
bugs by name. Merging the guards is the actual "design decision about
phase-reopen semantics" the plan doc flags — and it's not necessary to
close the bug this item exists to prevent. The bug is "someone hand-writes
a *new*, 5th copy of the field-tuple and gets one field wrong or leaves
one out" (mirroring the exact language used for the already-fixed 3-way
claim-fallback case). A shared primitive for the write closes that
specific bug class without touching guards or semantics at all.

## Recommendation

One primitive in `phase_transitions.py` (co-located with
`_clear_stale_task_creation_claim`/`reset_stale_executions_on_goto`, the
two siblings this same item already produced):

```python
def reopen_phase_execution(
    execution: PhaseExecution,
    *,
    status: str,
    started_at: Literal["clear", "now", "leave"] = "leave",
) -> None:
    """Reopen a PhaseExecution for a fresh cycle: write status and reset
    the one-time-per-cycle task-creation claim together. started_at is a
    third, independent axis -- see call sites for why each value differs;
    this function does not decide it, only applies whichever the caller
    already determined is correct for its own reopen reason.
    """
    execution.status = status
    execution.task_creation_claimed_at = None
    if started_at == "clear":
        execution.started_at = None
    elif started_at == "now":
        execution.started_at = datetime.utcnow()
    # "leave": no-op, by design -- not every reopen should touch it.
```

Each of the 4 call sites keeps its own guard and its own comment
explaining *why* it picked the status/started_at it picked (deleting that
context would be a real loss — it's the record of 3+ historical bugs).
Only the 2-3 line write itself is replaced with one call:

- `_handle_evaluation_retry`: `reopen_phase_execution(execution, status="pending", started_at="clear")`
- `_handle_evaluation_arbitrate`: `reopen_phase_execution(execution, status="in_progress", started_at="leave")`
- `_start_next_phase` (guard unchanged): `reopen_phase_execution(execution, status="in_progress", started_at="now")`
- `task_admin_routes.py` (guard unchanged): `reopen_phase_execution(execution, status="in_progress", started_at="leave")`

`started_at` is modeled as a 3-way enum, not a bool, because it is
genuinely 3 distinct behaviors (clear / stamp-now / leave), not one
optional feature — avoids the exact trap §4.2's `kill_tmux` flag fell
into (a single flag conflating two unrelated failure modes). Here the two
parameters (`status`, `started_at`) are orthogonal, each independently
meaningful, matching `repair_status`'s already-validated shape rather
than `kill_tmux`'s already-corrected one.

**Import path**: `phase_manager.py` cannot import `phase_transitions.py`
at module level today (`_reset_stale_executions_on_goto` at
`phase_manager.py:27` is a thin shim around a function-local, deferred
import at line 29 — the existing precedent, presumably avoiding a
circular import between this module and the orchestrator package).
`reopen_phase_execution` should follow the same convention: a deferred
import at each of `phase_manager.py`'s 3 call sites (or one shared shim,
matching the existing one). `task_admin_routes.py` already imports
`resume_workflow` from `engine_client.py` the same way (function-local,
at its own call site, `task_admin_routes.py:611`) — same pattern applies.

**Verification shape**, matching this item's own established discipline:
a characterization test per site asserting the exact same 3 fields end up
with the exact same values before and after the refactor (not a new
behavior assertion — this is a pure extraction, zero intended behavior
change), plus the 3 historical-bug regression tests this item's own
history already produced (`test_reopening_resets_stale_task_creation_claim`,
`TestReleaseStaleTaskCreationClaims`, and whatever covers arbitration's
`"in_progress"`-not-`"pending"` requirement) re-run unchanged against the
refactored code.

## Addendum, 2026-08-19 (gap-check after implementation): a 5th site existed

Implementing the above and then re-sweeping the whole codebase fresh for
every `task_creation_claimed_at = None` write (rather than trusting this
doc's own "4 sites" count, inherited directly from the plan doc's
enumeration) found one more: `_create_phase_task`'s success-path claim
release (`phase_transitions.py`, ~line 2930, right after the new `Task`
row is added). It was never named in the plan doc's scoping note, but its
own existing test class already understood it as a sibling of exactly
this pattern — `tests/test_advance_phases.py::TestCreatePhaseTaskResetsClaim`'s
docstring: "Every OTHER reopen point (`_start_next_phase`,
`_handle_evaluation_retry`, `_handle_evaluation_arbitrate`) resets
`task_creation_claimed_at` when it reopens a phase; this one didn't
[before its own historical fix]."

Its shape is structurally close to `_start_next_phase`'s
(`status in ("pending", "completed") → "in_progress"`, `started_at=now`),
with one real, documented asymmetry: the claim-clear fires even when the
status guard doesn't, because a second, different caller path
(`_case_in_progress_no_tasks`) can reach this function for a phase
another path already flipped to `"in_progress"` before any task existed
— for that case there's no status transition to gate on, but the claim
this call took must still be released. Migrated by wrapping only the
guarded branch in `reopen_phase_execution(execution, status="in_progress",
started_at="now")`, leaving the unguarded claim-only clear as its own
line — this preserves the asymmetry exactly rather than distorting the
primitive to accommodate it.

A fresh full sweep of every `task_creation_claimed_at`-writing site in
`phase_transitions.py` (10 total, across both `x.field = None` and
`.update({...})` forms) confirmed the remaining 4 non-primitive sites are
correctly out of scope, each with its own documented, distinct reason:
`_release_phase_task_creation_claim` (a different already-existing shared
primitive, with its own task-anchored `started_at` strategy), and three
single-field, claim-only `finally:`-block releases (`_resolve_arbitration_outcome`,
`_create_phase_task`'s own failure/bail-out path, `_create_corrective_task`,
`_case_in_progress_complete`) that guard a different kind of region
(evaluation-in-flight, or task-creation-attempt-in-flight) and deliberately
never touch `status` — reusing a status-touching helper there would be
wrong, not just unnecessary, exactly as several of their own comments
already say.

## What this does not do

Does not touch the 4 guards. Does not attempt to answer whether
`restart_task_endpoint`'s broader guard *should* match `_start_next_phase`'s
narrower one, or whether arbitration's `started_at`-preserving choice
should extend to `_handle_evaluation_retry`. Those are the genuine
phase-reopen-semantics questions this item originally flagged, and
nothing in the current bug-fix history or live code suggests any of the 4
sites is currently wrong for its own situation — only that a *5th*,
future hand-copy is one field away from a silent bug, which this
narrower dedup closes.
