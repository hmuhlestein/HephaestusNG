# PhaseExecution Status: Centralize the State Machine

## Goal

`PhaseExecution.status` (`src/core/database.py:620`) is the field the whole
autopilot pipeline trusts to answer "is this phase currently running, done,
failed, or untouched." In one debugging session (2026-08-31 to 2026-09-02,
workflow `72ed4df8`) it drifted out of sync with reality **three separate
times**, each a distinct code path, each requiring a live incident to
notice:

1. A goto rewound past a still-running later phase without terminating its
   agent — two agents (`qa_validation` and `development`) mutated the same
   worktree concurrently for about an hour. Fixed in `f9c50d72`.
2. A phase execution that ever recorded `"failed"` became a permanent
   tombstone: `derive_workflow_status` requires every execution to be
   `"completed"`/`"skipped"`, but nothing ever reset a `"failed"` row even
   after later retries of that same phase genuinely succeeded. The
   workflow (and the feature behind it) could never complete. Fixed
   (independently, twice — by this session and, more thoroughly, by a
   concurrent one) in `9546ac08` / `6c13cd02` / `bb0fd759`.
3. `_create_phase_task`'s reopen-to-`"in_progress"` step didn't include
   `"failed"` in its condition, so a phase execution stuck `"failed"` (bug
   #2) never became `"in_progress"` even while its retried task ran for
   real — invisible to every cross-phase concurrency guard that checks the
   workflow-wide in-progress list, letting a **later** phase
   (`product_validation`) get dispatched twice while an **earlier** one
   (`development`) was still genuinely active. `4d2f2005` fixed this in
   `_create_phase_task` — **and, when this document was first drafted, only
   there.** A closer read (§"Writers" #4 below) found the identical
   `("pending", "completed", "skipped")` gate list, missing the same
   `"failed"` case, independently copy-pasted into three siblings
   (`_clear_stale_task_creation_claim`, `_release_phase_task_creation_claim`,
   `_start_next_phase`) — the last of which is the *main* forward-progress
   path, not an edge case. All four are now fixed (same session, follow-up
   pass), but the fact that patching "the exact site" left three
   structurally-identical live bugs behind, undetected until a second
   review re-read the inventory this document itself compiled, is close to
   the strongest evidence available for doing the refactor below rather
   than continuing to hand-patch each gate list as its own gap surfaces.

All three are the same shape: **no single function owns writing
`PhaseExecution.status`.** Roughly ten independent call sites each
hardcode their own idea of which statuses are "stale," "reopen-eligible,"
or "terminal" — the recurring bug is one of those lists silently missing a
value a sibling list already has. This document proposes closing that
class of bug structurally, not by continuing to patch individual lists as
each new gap surfaces.

**Why `PhaseExecution` specifically, not `Feature.status`,
`Workflow.status`, or `AutopilotDesign.status`.** This same session
separately found the identical disease at three other levels of the
hierarchy:

- `Feature.status` stuck `"active"` needing a manual completion cascade,
  gated by a sibling mechanism (`AutopilotProject.is_active`) that also
  drifted.
- `AutopilotDesign.status` stranded in the transient value `"decomposing"`
  (`run_phase0` sets it before Phase 0 starts; `pick_next_design` only ever
  selects `"pending"`). Any interruption between those two points — a
  backend restart, a kill, an exception on a path that fails to write the
  outcome back — left the design in no queue, with no live workflow, and
  invisible to `_sync_stale_design_statuses` (which only looks at
  `"active"`). It disappeared from the pipeline entirely, twice in one
  session, each time needing a manual status reset. Fixed in `50a37f7c` by
  the recovery-policy route, not the centralization route:
  `_recover_designs_stuck_mid_decomposition` resets it to `"pending"` when
  no live Phase 0 workflow exists, bounded by the same
  `MAX_DESIGN_RETRIES` counter `pick_next_design`'s own retry uses.

These are the same architectural problem — **a status value that no
selector picks up, making the row invisible to the machinery that owns it**
— and are deliberately **not** in scope here. `PhaseExecution` is where
all three of *this* document's concretely diagnosed, reproduced bugs live,
it's the highest-frequency write target (every phase transition, every
retry, every goto touches it), and fixing it first gives a template — and
a track record — to point at before proposing the same treatment
elsewhere. Revisit the others as follow-ups once this pass has shipped and
held up, not as an expansion of this one.

Worth noting for that follow-up: the design-level instance was fixed with
a *bounded recovery policy*, not a centralized writer, and that was the
right call there — `"decomposing"` is a legitimately transient state, so
the fix is "notice it stopped being transient and act," not "prevent the
write." Which of the two treatments a given level needs is a per-level
judgement, not a foregone conclusion once this document's approach is
proven. See also the drift-vs-policy split under Step 3 item 5.

**The read side has the same disease, and this pass does not address it.**
This document's thesis is entirely about *writers* ("no single function
owns writing `PhaseExecution.status`"). But every consumer of
`_get_phase_statuses` also hardcodes its own *selection predicate* over
those statuses, and those drift independently in exactly the same way. A
live example from this session, distinct from bugs #1-#3 and not fixed by
anything proposed here: `_case_completed_with_successor` picks the phase to
advance *from* by most-recent completion, then searches for a pending phase
with `order > last_completed.order`. When the most-recently-completed phase
is also the highest-order one in the workflow (`deploy`), that search can
never match, and the case returned `None` — "no pending work" — with an
obviously actionable pending phase sitting right there at a lower order.
Fixed in `8a2e8b48` by adding a lowest-order-pending fallback. No amount of
write-centralization would have prevented it: every status involved was
correct; the *query* was wrong. Cataloguing the read-side predicates the
way §"Writers" catalogues the write sites is a natural sequel to this work
and is called out again under Non-Goals.

## Current State: Roughly Ten Places Read or Write This Field Independently

### Writers

1. **`_create_phase_task`** (`src/autopilot/orchestrator/phase_transitions.py:3407-3410`)
   Reopens to `"in_progress"` on dispatch, gated on
   `status in ("pending", "completed", "skipped", "failed")` — the
   `"failed"` branch was missing until today (bug #3 above).

2. **`reset_stale_executions_on_goto`** (`phase_transitions.py:157-260`)
   Resets executions at/after a goto target back to `"pending"`, gated on
   `status.in_(["in_progress", "completed", "failed"])` — `"failed"` was
   missing until today (bug #2). Also has its own separate live-task
   carve-out logic (added today, `f9c50d72`) distinguishing the goto's own
   target phase from strictly-later phases.

3. **`_close_execution`** (`src/phases/phase_manager.py:725-738`)
   The one already-shared helper — writes `status`/`completed_at`/
   `completion_summary` together. Used by `mark_phase_complete`'s handlers
   (`_handle_force_continue`, `_handle_force_fail`, the goto/retry
   handlers) to close a phase as `"completed"` or `"failed"`. The closest
   thing that exists today to what this document proposes generalizing.

4. **Reopen-to-`"in_progress"` sites** — at least four independent copies
   of the same gate list, each written out by hand rather than shared:

   | Site | Gate (before this session's fixes) |
   |---|---|
   | `_create_phase_task` (`phase_transitions.py:3424`) | `("pending", "completed", "skipped")` |
   | `_clear_stale_task_creation_claim` (`phase_transitions.py:142`) | `("pending", "completed", "skipped")` |
   | `_release_phase_task_creation_claim` (`phase_transitions.py:1584`) | `("pending", "completed", "skipped")` |
   | `_start_next_phase` (`phase_manager.py:1750`) | `("pending", "completed", "skipped")` |

   All four had the identical list, all four were missing `"failed"`, and
   fixing one (`_create_phase_task`, in `4d2f2005`) did nothing for the
   other three until a follow-up pass caught the divergence — see bug #3
   above. This is the concrete case for centralizing: not four call sites
   that happen to overlap, but four **copies of one list** that had
   already drifted once (a comment at `phase_manager.py:1759` shows
   `"skipped"` was added to all four together, previously) and drifted
   again for `"failed"` without anyone noticing until now.

5. **`mark_skipped_over_phases`** (`phase_transitions.py:233` area)
   Downgrades `"pending"` → `"skipped"` when a jump advances the pipeline
   past intervening phases.

6. **`reset_failed_phase_executions`**
   (`src/autopilot/orchestrator/engine_client.py:407`)
   A dedicated "un-fail" helper resetting every `"failed"` execution for a
   workflow back to `"pending"` — used by the two user-triggered resume
   paths (`resume_feature`, `_resume_stuck_workflow_tasks`) and by the
   self-heal sweep below.

7. **`_retry_exhausted_failed_workflows`** (`phase_transitions.py:718`)
   Sweep-driven self-heal: finds workflows failed by a phase exhausting
   its retry cap and calls #6 automatically, on a cooldown.

8. **`_release_pending_phases_with_done_tasks`** (`phase_transitions.py:1332-1401`)
   Self-heal for `"pending"` stuck despite an already-`"done"` task —
   a *third*, independent variant of the same drift class, discovered and
   fixed before this session even started ("Observed live: two workflows'
   phases sat 'pending' with a done task for days").

9. **`_release_pending_phases_with_orphaned_task`** (`phase_transitions.py:1410+`)
   A fourth variant: repairs a `"pending"` execution whose task's agent
   died without any completion ever being recorded.

### A data-integrity gap underneath all of the above (preventive, not yet triggered)

None of the ten writers above can fully guarantee which row they're
mutating. `PhaseExecution.phase_id` (`src/core/database.py:626`) is a
plain `ForeignKey`, not `unique=True`, and there is no composite unique
index either. Every read site — `_get_phase_statuses`, `_create_phase_task`,
and the transition function proposed below — does
`.filter_by(phase_id=phase_id).first()` with no `order_by`. If more than
one `PhaseExecution` row ever existed for the same phase, `.first()` would
silently return an arbitrary one and every write would "succeed" against
the wrong row.

**Checked directly: this has not happened yet.** All 765 rows in the live
`phase_executions` table today have distinct `phase_id`s — zero
duplicates. So Step 0 below is preventive hardening, not a fix for
observed corruption, and its migration/backfill step should find nothing
to consolidate. That's exactly what makes it cheap: adding the constraint
now, while it's a no-op, is free compared to discovering the gap after
something has actually created a second row.

### The one reader nearly everything depends on

10. **`_get_phase_statuses`** (`phase_transitions.py:1480-1497`)
    ```python
    def _get_phase_statuses(db, workflow_id: str) -> list:
        phases = db.query(Phase).filter_by(workflow_id=workflow_id).order_by(Phase.order).all()
        phase_statuses = []
        for phase in phases:
            exec = db.query(PhaseExecution).filter_by(phase_id=phase.id).first()
            phase_statuses.append(
                {"phase": phase, "execution": exec, "status": exec.status if exec else "pending"}
            )
        return phase_statuses
    ```
    Every one of `_advance_phases`'s four dispatch cases (`_case_start_first_phase`,
    `_case_in_progress_no_tasks`, `_case_completed_with_successor`,
    `_case_in_progress_complete`) partitions this list into
    `completed`/`pending`/`in_progress` buckets and is documented as
    "inert whenever any in_progress phase exists" — a guarantee that is
    only as good as every writer above keeping `.status` accurate. Bug #3
    is exactly this guarantee silently failing because one writer (#1)
    didn't reopen a `"failed"` row.

11. **`derive_workflow_status` / `derive_feature_status`**
    (`src/core/status_derivation.py:423+`) — the completion-detection
    consumer. Requires every tracked `PhaseExecution` to be `"completed"`
    or `"skipped"`; any other value blocks the whole workflow (and the
    feature behind it, and whatever queued feature depends on it) from
    ever completing. This is the check bug #2's tombstone silently defeated
    for a full day.

### Why this keeps recurring

`PhaseExecution.status` is doing two jobs at once:

- **Mirroring real task state** ("is a task actually running under this
  phase right now") — which is, in principle, fully derivable from `Task`
  rows (`Task.status.in_(LIVE_STATUSES)` for a given `phase_id`).
- **Recording the last gate/orchestrator decision** (goto vs. retry vs.
  skip vs. continue, and the target phase) — genuine additional state that
  doesn't live on `Task` rows at all.

Because the first job is *supposed* to be a pure mirror but is
implemented as ten independently-mutated copies instead of one derivation,
every one of today's bugs was a missing case in exactly one of those ten
places while a sibling place already had it right.

## Design Options Considered

- **Option A — Derive, don't store.** Stop persisting the
  "is-it-running" half of `.status` at all; compute it live from `Task`
  rows every time `_get_phase_statuses` is called. Keep `PhaseExecution`
  only for the genuinely-stateful half (last decision, target, skip
  reason). This is the architecturally "correct" end state — it makes the
  whole bug class structurally impossible, since there would be nothing
  left to drift. But it touches the *semantics*, not just the mechanics,
  of every one of the ten call sites above (several of them read
  `execution.status` for reasons beyond "is it running," e.g. `"skipped"`
  for `derive_workflow_status`'s completeness check), and it's the
  riskiest, largest-blast-radius option in a file that is under heavy,
  actively-changing concurrent development right now (three external
  commits landed in `phase_transitions.py` during this session alone).

- **Option B — Centralize the writes.** Keep the schema and the two-jobs
  reality as-is, but route every write through one function that owns the
  full transition table, so "which statuses can become `X`" is answered
  in exactly one place instead of re-derived ad hoc at each of the ten
  call sites. Directly fixes the recurring defect (a missing case in one
  writer's private list) without touching read-side semantics anywhere.
  Moderate, incremental effort; each migrated call site keeps its existing
  test coverage as a safety net.

- **Option C — Detect, don't yet cure.** Add a cheap, additive-only
  invariant check — "any `Task` with a live status whose phase's
  `PhaseExecution.status != 'in_progress'` is a bug, log it now" — without
  changing any write path. Would have surfaced bug #3 within one sweep
  tick instead of requiring hours of live-incident investigation to
  notice. Zero risk to ship (it only adds a log line), and valuable
  immediately, but doesn't fix anything on its own.

**Recommendation: ship C first, then B.** C is nearly free and gives an
immediate safety net — including for whatever fourth variant of this bug
class hasn't surfaced yet. B is the real fix, undertaken deliberately and
incrementally once C is watching for regressions. A is the right long-term
shape but should be revisited only after B has been live long enough to
show whether centralizing the writers alone eliminates the drift, or
whether the two-jobs-in-one-field design itself still needs unwinding.

## Proposed Design

### Step 0 — Close the data-integrity gap first

Add the missing constraint before anything above depends on rows being
unique per phase: `UniqueConstraint("phase_id")` on `PhaseExecution`
(`src/core/database.py:626`), plus a one-off migration/backfill script
that finds any phase_id with more than one row today, logs what it finds,
and consolidates (keep the most-recently-updated row; this needs a real
look at whatever the backfill turns up before deciding merge vs. delete,
not a blind rule written in advance). Cheap, independent, and ships before
Step 1 so the invariant check and the transition function below are both
built on a foundation that can't silently pick the wrong row.

### Step 1 — Invariant check (Option C), ships next

One query, run both (a) immediately after every dispatch in
`_advance_phases` and (b) once per sweep tick as a standalone pass:

```python
from src.core.database import PhaseExecutionStatus  # database.py:97 -- reuse, don't reinvent

# "pending" included: a task that exists at all but hasn't been picked up
# yet is still real, pending work under this phase (see
# _release_pending_phases_with_orphaned_task's own "never dispatched to an
# agent, stale >1min" case) -- omitting it from this list would make the
# detector blind to exactly the failure mode that self-heal exists for.
LIVE_TASK_STATUSES = ["pending", "assigned", "in_progress", "queued", "blocked", "needs_work", "under_review"]

def find_phase_execution_drift(db, workflow_id: str) -> list[tuple[Phase, PhaseExecution, Task]]:
    """Any phase with a live task whose PhaseExecution isn't 'in_progress'
    is drift -- log it immediately rather than waiting for it to manifest
    as a stuck workflow hours or days later."""
    return (
        db.query(Phase, PhaseExecution, Task)
        .join(PhaseExecution, PhaseExecution.phase_id == Phase.id)
        .join(Task, Task.phase_id == Phase.id)
        .filter(
            Phase.workflow_id == workflow_id,
            Task.status.in_(LIVE_TASK_STATUSES),
            PhaseExecution.status != PhaseExecutionStatus.IN_PROGRESS,
        )
        .all()
    )
```

**This still misses the two states that actually cost a full day in this
session, and needs a second, task-independent check.** Both bugs #2 and #3
involved a phase whose *task was already `"done"`* (not live) while the
execution sat `"failed"` — the query above, joined on a live task, cannot
see that case at all; there is no live task to join against by the time
anyone would run this check. The cheap fix is a second predicate that
doesn't go through `Task` at all:

```python
def find_stuck_active_workflows(db) -> list[tuple[Workflow, Phase, PhaseExecution]]:
    """A workflow marked 'active' with any 'failed' PhaseExecution is
    stuck by definition -- nothing in _advance_phases's four dispatch
    cases will ever look at a 'failed' execution, live task or not. This
    catches the exact shape of bugs #2 and #3 (a done task, a 'failed'
    execution, no live task for find_phase_execution_drift to key off),
    which is the single highest-cost failure mode this session produced."""
    return (
        db.query(Workflow, Phase, PhaseExecution)
        .join(Phase, Phase.workflow_id == Workflow.id)
        .join(PhaseExecution, PhaseExecution.phase_id == Phase.id)
        .filter(
            Workflow.status == "active",
            PhaseExecution.status == PhaseExecutionStatus.FAILED,
        )
        .all()
    )
```

This is a check on the `Workflow` ↔ `PhaseExecution` relationship, not a
change to `Workflow.status` handling itself — it stays inside this
document's stated scope (detecting drift), not the explicitly-deferred
scope of refactoring `Workflow`/`Feature` status writers.

**Debounce `find_phase_execution_drift` before logging, don't fire on the
first sighting** (this doesn't apply to `find_stuck_active_workflows`,
which has no such transient window — an active workflow with a failed
execution is never a normal, momentary state). A task legitimately spends
a few hundred milliseconds `"pending"`/newly-dispatched before
`_create_phase_task` reopens its phase's execution to `"in_progress"` —
checking in that normal window is a false positive, not drift. Require the
SAME `(phase_id, task_id)` mismatch to still be present on a second, later
check (e.g. the *next* sweep tick, ~seconds later) before logging it as
real drift, rather than reacting to a single snapshot.

Log a `WARNING` (not yet an error, not yet a hard-fail) for every
confirmed result from either check, naming the workflow/phase/task ids and
the mismatched statuses. Watch both against real traffic for a window
before doing anything else — both to confirm the debounce actually
eliminates false positives and to see how often each drift class fires in
practice.

**Implementation bug caught after this shipped, not by the unit tests
that shipped with it:** the debounce state must be keyed per `workflow_id`
(`Dict[str, set]`), not a single shared set. The sweep calls
`check_and_log_phase_execution_drift` once per active/paused workflow per
tick (`background_loops.py`), and a single shared set had every
workflow's call `.clear()` out whatever the *previous* workflow in that
same tick's iteration had just recorded — so a workflow's own genuine,
persistent drift got compared against some *other* workflow's keys on the
next tick and could never reach "second sighting." With more than one
monitored workflow (the normal case — 9 active/paused workflows were live
when this was caught), this silently defeated the debounce entirely: real
drift never got logged. None of the original unit tests exercised calling
this function for two different `workflow_id`s without clearing state in
between, so all 12 passed anyway. Fixed by scoping the dict per workflow;
a regression test covering exactly this two-workflow interleaving was
added alongside the fix.

### Step 2 — `transition_phase_execution`, additive, unused at first

A single function + explicit transition table, added as pure new code
with its own exhaustive unit tests, not yet wired into any existing call
site.

**Must be atomic, not check-then-act.** A naive `SELECT` → check the
transition is valid → mutate → `commit()` is exactly the race
`_claim_phase_task_creation` (`phase_transitions.py:1497`) already exists
to close elsewhere in this same file: "a plain check... is a race — no
matter how the two paths interleave" (its own docstring). Two concurrent
callers can both read the same `from_status`, both see their transition
as valid, and both write — silently re-creating the exact class of bug
this document exists to eliminate, just moved into the new "centralized"
function instead of fixed by it. The transition must be a single
`UPDATE ... WHERE status = :from_status`, mirroring
`_claim_phase_task_creation`'s own pattern, with the row count telling the
caller whether it actually won:

```python
from src.core.database import PhaseExecutionStatus  # database.py:97 -- the existing constants class; do not reintroduce a parallel enum

_VALID_TRANSITIONS: dict[str, set[str]] = {
    PhaseExecutionStatus.PENDING:     {PhaseExecutionStatus.IN_PROGRESS, PhaseExecutionStatus.SKIPPED},
    PhaseExecutionStatus.IN_PROGRESS: {PhaseExecutionStatus.COMPLETED, PhaseExecutionStatus.FAILED, PhaseExecutionStatus.PENDING},  # pending: goto rewind
    PhaseExecutionStatus.COMPLETED:   {PhaseExecutionStatus.IN_PROGRESS, PhaseExecutionStatus.PENDING},  # goto re-entry redo
    PhaseExecutionStatus.FAILED:      {PhaseExecutionStatus.IN_PROGRESS, PhaseExecutionStatus.PENDING},  # retry or un-fail
    PhaseExecutionStatus.SKIPPED:     {PhaseExecutionStatus.IN_PROGRESS},  # goto sends work back through it
}

# Per-transition field resets, reconciled ONCE here instead of ad hoc at
# each of the ten existing call sites -- e.g. reset_stale_executions_on_goto
# today clears completed_at/started_at/task_creation_claimed_at when
# resetting to "pending"; _create_phase_task's reopen sets started_at="now"
# when moving to "in_progress" but leaves completed_at alone. Any (from, to)
# pair not listed here defaults to leaving started_at/completed_at/claim
# untouched -- reviewed against real call-site behavior during Step 3's
# migration of each site, not guessed in advance.
_FIELD_RESETS: dict[tuple[str, str], dict] = {
    (PhaseExecutionStatus.COMPLETED, PhaseExecutionStatus.PENDING): {"completed_at": None, "started_at": None, "task_creation_claimed_at": None},
    (PhaseExecutionStatus.FAILED, PhaseExecutionStatus.PENDING):    {"completed_at": None, "started_at": None, "task_creation_claimed_at": None},
    (PhaseExecutionStatus.IN_PROGRESS, PhaseExecutionStatus.PENDING): {"completed_at": None, "started_at": None, "task_creation_claimed_at": None},
    (PhaseExecutionStatus.PENDING, PhaseExecutionStatus.IN_PROGRESS): {"started_at": "now", "task_creation_claimed_at": None},
    (PhaseExecutionStatus.COMPLETED, PhaseExecutionStatus.IN_PROGRESS): {"started_at": "now", "completed_at": None},
    (PhaseExecutionStatus.FAILED, PhaseExecutionStatus.IN_PROGRESS): {"started_at": "now", "completed_at": None},
    (PhaseExecutionStatus.SKIPPED, PhaseExecutionStatus.IN_PROGRESS): {"started_at": "now"},
    (PhaseExecutionStatus.PENDING, PhaseExecutionStatus.SKIPPED): {"completed_at": "now"},
}

def transition_phase_execution(db, phase_id: str, to_status: str, *, reason: str) -> Optional[PhaseExecution]:
    """Atomically move phase_id's PhaseExecution to to_status. Returns the
    (freshly re-read) row on success, None if the row wasn't in a state
    this transition is valid from (someone else already moved it, or the
    caller's assumption about current state was wrong) -- callers treat
    None the same way _claim_phase_task_creation's False is treated today:
    skip, don't retry blindly, let the next sweep tick re-evaluate.
    """
    execution = db.query(PhaseExecution).filter_by(phase_id=phase_id).first()
    if execution is None:
        raise ValueError(f"No PhaseExecution for phase {phase_id}")
    from_status = execution.status
    allowed = _VALID_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        logger.error(
            f"[PHASE-TRANSITION] Invalid {from_status!r} -> {to_status!r} "
            f"for phase {phase_id} ({reason}) -- allowed: {sorted(allowed)}"
        )
        # Initial rollout: log and return None (treat as "not ours to make")
        # rather than raise, so a pre-existing bad state found on day one
        # doesn't turn into a hard outage the moment this ships. Escalate
        # to raising once Step 1's drift check has run clean for a while.
        return None

    values = {"status": to_status}
    for field, val in _FIELD_RESETS.get((from_status, to_status), {}).items():
        values[field] = utc_now() if val == "now" else val

    # The atomic step: succeeds only if the row is STILL from_status right
    # now -- closes the exact race a SELECT-then-mutate would reopen.
    changed = (
        db.query(PhaseExecution)
        .filter(PhaseExecution.phase_id == phase_id, PhaseExecution.status == from_status)
        .update(values, synchronize_session=False)
    )
    db.commit()
    if changed == 0:
        logger.info(f"[PHASE-TRANSITION] Lost the race on phase {phase_id}: no longer {from_status!r} ({reason})")
        return None
    # This session has expire_on_commit=False (see database.py), so the
    # `execution` object loaded above is still cached in the identity map
    # with its pre-update attribute values -- a plain re-query would return
    # that same stale in-memory object rather than the row this call just
    # wrote. db.refresh() forces it to reload from the database.
    db.refresh(execution)
    return execution
```

**Implementation note (caught by the Step 2 unit tests, not anticipated in
the sketch above):** the naive `return db.query(...).filter_by(...).first()`
silently returns stale data on this codebase's session configuration.
`expire_on_commit=False` (see Conventions in CLAUDE.md) means SQLAlchemy's
identity map keeps returning the SAME in-memory `execution` object for a
given primary key rather than re-populating it from a fresh SELECT — so the
caller would see the pre-transition status even though the UPDATE
succeeded. Fixed with `db.refresh(execution)` before returning it, which
forces an actual reload from the row this call just wrote.

`_FIELD_RESETS` above is a first pass built from re-reading the four call
sites this document already cites (`reset_stale_executions_on_goto`,
`_create_phase_task`'s reopen, `_close_execution`, `reopen_phase_execution`)
— it is explicitly **not** exhaustive or verified yet. Step 3 below treats
reconciling it against each real call site's current behavior as part of
migrating that site, not as something finished by this document.

### Step 3 — Migrate one call site at a time

In order of how cleanly each one maps onto the table above, not by risk:

1. `_close_execution` (`phase_manager.py:725`) — already the closest thing
   to a shared writer; migrating it covers the `"completed"`/`"failed"`
   close paths from `mark_phase_complete` in one move.

   **Bug caught migrating this, deployed live for ~10 minutes before being
   caught and fixed:** `_FIELD_RESETS` has no entry for
   `(in_progress, completed)` or `(in_progress, failed)` — the two
   transitions this function actually performs — so the naive migration
   silently stopped setting `completed_at` at all, despite that being this
   function's whole purpose (`execution.completed_at = utc_now()` was
   unconditional in the original). `PhaseExecution.completed_at` isn't
   just cosmetic: `phase_transitions.py` sorts/selects on
   `.completed_at.desc()` in at least two places to find "the last
   completed phase," so a `NULL` there is a functional correctness bug,
   not only a reporting gap. All 414 tests in the Step 3 regression set
   passed anyway, because none of them asserted `completed_at` gets set —
   only `status`/`action`. Fixed by passing `completed_at` through the
   `extra_fields` extension point explicitly at this call site (not by
   adding a blanket `_FIELD_RESETS` entry, since a future call site
   reaching "completed"/"failed" from some other state may have different
   needs). Confirmed via the live DB that no real completion happened
   during the exposure window, so no data was actually lost — but the
   window existed. A dedicated `TestCloseExecution` class was added
   asserting `completed_at` directly, since caller-level tests alone had
   missed it.
2. The four reopen-to-`"in_progress"` sites in the table above
   (`_create_phase_task`, `_clear_stale_task_creation_claim`,
   `_release_phase_task_creation_claim`, `_start_next_phase`) — all four
   now carry the identical, correct `("pending", "completed", "skipped",
   "failed")` gate as of this session's follow-up fix.

   **Revised after actually reading all four call sites, not just their
   shared gate:** only the GATE (which from-statuses are eligible) is
   identical across all four. The `started_at` write is not — three
   distinct behaviors, not one copy-pasted four times:
   - `_create_phase_task` and `_start_next_phase` both call the shared
     `reopen_phase_execution(status="in_progress", started_at="now")` and
     are genuinely identical. Migrated together.
   - `_clear_stale_task_creation_claim` does NOT go through
     `reopen_phase_execution` at all — it backfills `started_at` from the
     phase's latest task's `created_at`, but only if `started_at` isn't
     already set (`execution.started_at or latest_task.created_at`).
   - `_release_phase_task_creation_claim` also bypasses
     `reopen_phase_execution` and anchors `started_at` to the EARLIEST
     task's `created_at` — its docstring documents a real live incident
     (a duplicate self-heal task) caused by a prior version of this exact
     code using `utc_now()` instead. Blindly applying `_FIELD_RESETS`'s
     `started_at: "now"` default here would silently reintroduce that
     bug.

   Also found in the process: `_FIELD_RESETS` itself only cleared
   `task_creation_claimed_at` for `(pending, in_progress)`, not for
   `(completed, in_progress)`, `(failed, in_progress)`, or
   `(skipped, in_progress)` — even though `reopen_phase_execution` clears
   it unconditionally for every one of those. Never exercised in
   production (`transition_phase_execution` had no caller reaching
   `in_progress` yet), so no live impact, but fixed before migrating
   anything that depends on it, with dedicated tests added for each of
   the three previously-missing entries.

   Given the divergent `started_at` handling, this item is being done as
   two-plus migrations, not one: `_create_phase_task` + `_start_next_phase`
   together first (their behavior is provably identical and now fully
   covered by `_FIELD_RESETS`), then `_clear_stale_task_creation_claim`
   and `_release_phase_task_creation_claim` separately, each passing its
   own computed `started_at` through `extra_fields` to preserve its
   specific, hard-won semantics exactly.

   **Second bug caught auditing this migration after it had already
   shipped:** `_FIELD_RESETS[(completed, in_progress)]` and
   `[(failed, in_progress)]` both cleared `completed_at` — but
   `reopen_phase_execution`, the actual shared writer behind both
   currently-migrated callers, never touches `completed_at` for any
   transition (confirmed by reading its source, not assumed). That
   `completed_at: None` was an unverified guess carried over from this
   document's original Step 2 sketch, never checked against the real
   function before Step 3.2 started actually exercising it in
   production. Traced every reader of `PhaseExecution.completed_at` in
   the codebase before concluding this was safe to have shipped
   unnoticed: every one of them filters to `status == "completed"` or
   `"failed"` first, so a freshly-reopened `"in_progress"` row is
   excluded from all of them regardless of which value `completed_at`
   holds — no functional consumer was ever affected either way. Fixed to
   match `reopen_phase_execution`'s real behavior (leave it untouched),
   which also restores consistency with `_escalate_unresolvable_goto`'s
   still-unmigrated direct `reopen_phase_execution` call. Two new tests
   added at both the `transition_phase_execution` unit level and the
   `_create_phase_task` call-site level, asserting a prior cycle's
   `completed_at` survives a reopen unchanged — this is now the second
   time an unverified `_FIELD_RESETS` entry shipped before being checked
   against the function it was supposed to model; each future migration
   in this list should diff the ENTIRE set of fields the real call site
   touches against the table before wiring it in, not just the fields
   that seem relevant.

   **`_clear_stale_task_creation_claim` and `_release_phase_task_creation_claim`,
   migrated next, following this item's own guidance above.** Both are
   two-part functions: an already-atomic claim-release step (untouched by
   either migration) plus a conditional status-repair step, which is what
   moved to `transition_phase_execution`. Neither shares `started_at`
   semantics with `_create_phase_task`/`_start_next_phase` or with each
   other:
   - `_clear_stale_task_creation_claim`: leave `started_at` if already
     set, else backfill from the phase's LATEST task's `created_at`.
   - `_release_phase_task_creation_claim`: always overwrite `started_at`
     with the guarded task's EARLIEST `created_at` (or `utc_now()` if
     somehow no task exists) — its own docstring documents a real live
     incident (a duplicate self-heal task) from a prior version using
     `utc_now()` unconditionally instead.

   Both computed values are passed through `extra_fields`, bypassing
   `_FIELD_RESETS`'s `started_at="now"` default entirely for these two
   call sites.

   **A third, more fundamental gap found here, in `transition_phase_execution`
   itself, not in the per-site table:** `_release_phase_task_creation_claim`'s
   own docstring documents a real, previously-fixed live incident
   (`test_maybe_retry_failed_tasks_is_claim_protected`) caused by this
   project's `expire_on_commit=False` sessions combined with a
   `synchronize_session=False` claiming UPDATE elsewhere — a PhaseExecution
   already loaded into the session's identity map (as `_get_phase_statuses`
   does for nearly every caller) stays stale after such an UPDATE, unless
   the read that follows uses `.populate_existing()`. That function was
   already fixed to use it; `transition_phase_execution`'s own initial
   read was not, since none of the first three migrated sites happened to
   need it. Fixed by adding `.populate_existing()` to
   `transition_phase_execution`'s own query, protecting every current and
   future caller, not just this one.

   Traced the actual severity before treating this as launch-blocking: the
   atomic UPDATE's own `WHERE status = :from_status` clause always queries
   the real row, never the possibly-stale Python object, so a stale
   `from_status` read can never cause an incorrect WRITE — at worst it
   makes `_VALID_TRANSITIONS` wrongly REJECT a transition the row's real
   current status would actually allow (a legitimate transition silently
   skipped, not a corrupted one). Still worth fixing outright given the
   documented precedent. Verified with a synthetic unit test (confirmed to
   fail without the fix, pass with it) plus the pre-existing
   `test_clears_claim_when_execution_was_already_loaded_in_the_same_session`,
   which already covered this exact scenario for the real call site and
   also fails without the fix.
3. `reset_stale_executions_on_goto` (`phase_transitions.py:157`) — covers
   "rewind resets to `pending`," the exact site of bug #2, including
   today's added live-task-termination logic (`f9c50d72`), which stays as
   a wrapper around the centralized transition rather than being absorbed
   into it (terminating agents is a side effect the transition table
   itself shouldn't own).

   **Migrated.** Structurally different from every site above: this one
   operates on a BATCH of `PhaseExecution` rows at once, not a single
   `phase_id`, with the live-task/agent-termination logic above deciding
   which rows in the batch qualify. That decision logic is left
   completely untouched (it is call-site business logic, exactly what
   `transition_phase_execution` was never meant to own) — only the
   final write loop (`s.status = "pending"; s.completed_at = None; ...`
   for each qualifying row) moved to a per-row `transition_phase_execution`
   call. No `extra_fields` needed: `_FIELD_RESETS[(in_progress/completed/
   failed, pending)]` already clears exactly the three fields this loop
   cleared directly (verified against the real function's source during
   Step 3.2's gap-check, before this migration even started).

   One deliberate behavior change: one batch `db.commit()` at the end
   became N independent atomic commits, one per row. A mid-loop crash now
   leaves a consistent PREFIX of resets committed instead of losing all
   of them to an uncommitted transaction — matching this refactor's
   per-row atomicity goal rather than preserving incidental batch
   atomicity nothing actually depended on. The return value (count of
   rows reset) now reflects transitions that actually succeeded rather
   than candidates identified by the batch query, which only diverge
   under a genuine concurrent race on one of these rows — in which case
   the atomic UPDATE's own check is correct and the batch query's earlier
   read isn't. Existing test coverage was thorough for the qualification
   logic (exclusion of the firing phase, target-order inclusion, stale
   "failed" tombstones, live-task protection, agent termination for
   phases rewound past) but had no test exercising more than one
   qualifying row per call — added
   `test_goto_resets_multiple_stale_phases_in_one_call` to cover the
   loop itself, not just the filtering.
4. `reset_failed_phase_executions` (`engine_client.py:407`) and the
   remaining `reopen_phase_execution` call sites.
5. The self-heal functions split into two categories, not one — conflating
   them was an earlier mistake in this document:

   - **Drift repair — becomes redundant once steps 1-4 land, delete after
     a monitoring window:** `_release_pending_phases_with_done_tasks` and
     `_release_pending_phases_with_orphaned_task`. Both exist purely
     because some writer failed to keep `.status` in sync with a task that
     already finished — exactly the defect class steps 1-4 close by
     construction. Once the invariant holds by construction, these have
     nothing left to repair.
   - **Recovery policy — keep regardless of centralization:**
     `_retry_exhausted_failed_workflows`. This does not exist because a
     writer forgot a case — it exists because a phase *genuinely*
     exhausted its retry cap, `"failed"` is the *correct* status for that,
     and someone has to decide whether the workflow gets a bounded second
     chance (its own cooldown + hard retry-cycle cap, "left alone
     permanently" once exhausted). No amount of write-centralization
     produces that decision; it's policy, not bookkeeping. Deleting it
     would reintroduce exactly the "a human must click Resume" gap this
     session's work was about closing. The same reasoning covers
     `_recover_abandoned_workflows_with_completed_phase`
     (`worktree_integration.py:824`): the phase's work is genuinely done,
     but nothing ever *ran* the evaluation that would have advanced it
     (a backend restart racing `fire_spec_gate_if_ready`) — centralizing
     who writes `.status` doesn't make an evaluation run that never
     started.

**Sequencing between transitions is a separate concern this doesn't
solve, and callers still own it.** Centralizing *who writes* a transition
doesn't validate the *order* multiple transitions are invoked in.
Concretely: `mark_skipped_over_phases` only ever downgrades a phase that
is currently `"pending"` (by design — a genuinely `"completed"` phase must
not be overwritten). If a goto's own reset-to-`"pending"` for a `"failed"`
phase hasn't run yet when `mark_skipped_over_phases` is evaluated for that
same phase, the skip silently no-ops instead of skipping it, and the
`"failed"` row is left exactly as stuck as before. `transition_phase_execution`
makes each individual write safe and self-consistent; it does not
guarantee `_handle_evaluation_goto` and its siblings call things in the
right order. That ordering audit — confirming every multi-step caller
resets before it skips, closes before it reopens, etc. — is real,
separate work each Step 3 migration needs to check for its own call site,
not something Step 2 solves by existing.

**This isn't hypothetical — fixing the three sibling gate lists (bug #3's
full picture, above) immediately surfaced exactly this shape of bug.**
`_case_in_progress_complete` (`phase_transitions.py:2296-2320`) has a
`try/finally` that unconditionally calls `_release_phase_task_creation_claim`
after `_retry_failed_tasks_with_done` — including on that function's
exhaustion branch, which deliberately sets `execution.status = "failed"`
as a terminal decision (retry cap exhausted, workflow failed, stop).
Before this session's fix, `_release_phase_task_creation_claim` didn't
treat `"failed"` as reopen-eligible, so the terminal status happened to
survive by accident. The moment it *was* made reopen-eligible (correctly,
for every other caller of that function), this one caller's `finally`
immediately flipped the just-set `"failed"` status back to `"in_progress"`
— caught by `test_exhausted_retry_cap_fails_the_workflow_instead_of_firing_transition`,
fixed by making the `finally` branch on the retry outcome instead of
calling the reopen-capable release unconditionally. The bug wasn't in the
gate list fix itself; it was a caller relying on a side effect of the old,
incomplete gate list that nothing had named as a dependency. A centralized
`transition_phase_execution` does not remove the need for this kind of
audit — if anything, this is the argument for doing the per-call-site
migration in Step 3 slowly, one commit at a time, with full test runs
between each, rather than trusting the gate-list fix alone.

**Partial migration is a safe intermediate state.** Each Step 3 site acts
on one `phase_id` at a time and only interacts with sibling phases through
already-committed rows (never in-memory state shared across call sites),
so having some sites migrated to `transition_phase_execution` while others
still write `.status` directly is not itself a hazard — the two styles
don't conflict, they just haven't been unified yet. This is what makes
migrating one site per commit (rather than one all-or-nothing rewrite)
viable at all: any individual commit can be paused, reverted, or delayed
without leaving the system half-broken.

Each step: run the full existing suite covering this area
(`test_advance_phases.py`, `test_phase_transitions_spec_gate.py`,
`test_condition_evaluation_fails_loudly.py`, `test_goto_reconvergence.py`,
`test_status_derivation.py`, `test_status_derivation_wiring.py`,
`test_phase_manager.py`, `test_retry_exhausted_failed_workflows.py`,
`test_reset_failed_phase_executions.py`,
`test_abandoned_workflow_failed_execution.py`,
`test_recover_designs_stuck_mid_decomposition.py` — green as of
`3adcff64`) plus the new transition-table tests, deploy, watch Step 1's
invariant check for new violations before starting the next site.

Note the last three cover the *recovery-policy* paths (Step 3 item 5's
"keep" list), not the write paths being migrated. They are the regression
net for the thing most easily broken by accident during this refactor:
a migration that makes a status un-writable in some state, and in doing so
silently disables the self-heal that depended on writing it.

## Non-Goals

- Not deriving `PhaseExecution.status` live from `Task` rows (Option A)
  in this pass — a larger, separate effort to revisit only if centralizing
  the writers (Option B) still shows drift after it has been live for a
  while.
- Not changing `PhaseExecution`'s columns or the meaning of its existing
  values — Step 0's `UniqueConstraint("phase_id")` is the one schema
  change in scope, added because it's a precondition every other step
  implicitly relies on (see the data-integrity section above), not an
  expansion of scope.
- Not changing orchestrator *decision* logic (what counts as a goto vs.
  retry vs. skip, or which phase is selected next) — only who writes the
  resulting status and how consistently. This is a real boundary, not a
  formality: `8a2e8b48` fixed a live stall in this session where every
  status was correct and `_case_completed_with_successor`'s *successor
  search predicate* was what failed. Nothing in this document would have
  caught it, and a reader who assumes "centralize the phase state machine"
  covers "phases not advancing" will be wrong in exactly that way.
- Not auditing or centralizing the **read-side** predicates over
  `PhaseExecution.status` (the four `_advance_phases` dispatch cases and
  `derive_workflow_status`/`derive_feature_status`, each partitioning
  `_get_phase_statuses` by its own hardcoded status list). Same drift
  class, opposite side of the field; see the read-side note in Goal. A
  natural sequel, deliberately not bundled in — mixing a write-path
  refactor with read-path semantic changes in one pass is how a
  "mechanical" migration turns into a behavioural one.

## Risks

- **This file is under heavy, active concurrent development.** Three
  external commits (`bb0fd759`, `8a2e8b48`, `6c13cd02`) landed in
  `phase_transitions.py` during this session alone, on top of this
  session's own two. A large single refactor PR here has a high chance of
  merge-conflicting with concurrent work. Mitigation: Step 1 is small and
  additive (low conflict surface); each Step 3 migration is its own small,
  independently-mergeable commit, not one big rewrite.
- **Surfacing a backlog of pre-existing bad states.** The moment
  `transition_phase_execution` starts checking transitions, workflows that
  are *already* sitting in an invalid state (there may be more than the
  three found so far) will start logging errors immediately. Initial
  rollout logs and returns `None` rather than raising (see the sketch
  above) so this doesn't turn into new hard failures on day one — a
  caller that gets `None` back skips and lets the next sweep tick
  re-evaluate, exactly like a lost `_claim_phase_task_creation` race
  today. Escalate to raising only once the invariant check has run clean
  for a while.

## Effort Estimate

- Step 0 (unique constraint + backfill): ~half a day, most of it spent
  looking at whatever the backfill query actually finds rather than
  writing the migration itself.
- Step 1 (invariant check, with debounce): ~half a day. Low risk, ships
  independently, immediate value even if nothing else in this document
  happens.
- Step 2 (transition table + tests): ~1-1.5 days — the atomic-update
  mechanics are straightforward, but reconciling `_FIELD_RESETS` against
  each real call site's current behavior (not guessed in advance) is
  where the time actually goes.
- Step 3 (per-call-site migration, ~6-8 sites): roughly half a day to a
  day each, including verification and a monitoring window before moving
  to the next site — realistically spread over 1-2 weeks given the file's
  current churn, not done as a single sprint.
- Total: on the order of 2-3 weeks of attention, deliberately spread out
  rather than front-loaded, given the concurrency risk noted above.

## Open Questions

- Should the "last gate decision" metadata (goto reason, target phase)
  move out of `PhaseExecution` into its own table, fully separating "what
  the orchestrator decided" from "current run state"? This is really the
  precursor question to Option A, and doesn't need answering before
  Option B ships.
- Is there an existing metrics/observability pipeline Step 1's drift
  check should feed into, or is a log line sufficient at today's
  operational scale?
