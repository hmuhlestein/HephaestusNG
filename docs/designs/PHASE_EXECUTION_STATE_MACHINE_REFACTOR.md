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

**Why `PhaseExecution` specifically, not `Feature.status` or
`Workflow.status`.** This same session separately found the identical
disease one level up the hierarchy — `Feature.status` stuck `"active"`
needing a manual completion cascade, gated by a sibling mechanism
(`AutopilotProject.is_active`) that also drifted. Those are real instances
of the same architectural problem and are deliberately **not** in scope
here: `PhaseExecution` is where all three of *this* session's concretely
diagnosed, reproduced bugs live, it's the highest-frequency write target
(every phase transition, every retry, every goto touches it), and fixing
it first gives a template — and a track record — to point at before
proposing the same treatment for `Feature`/`Workflow`. Revisit those two
as a follow-up once this pass has shipped and held up, not as an
expansion of this one.

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
    return db.query(PhaseExecution).filter_by(phase_id=phase_id).first()
```

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
2. The four reopen-to-`"in_progress"` sites in the table above
   (`_create_phase_task`, `_clear_stale_task_creation_claim`,
   `_release_phase_task_creation_claim`, `_start_next_phase`) — all four
   now carry the identical, correct `("pending", "completed", "skipped",
   "failed")` gate as of this session's follow-up fix, which is exactly
   why they're a single migration item: one transition
   (`X -> "in_progress"` on dispatch), copy-pasted four times, is the
   textbook case for collapsing into one call.
3. `reset_stale_executions_on_goto` (`phase_transitions.py:157`) — covers
   "rewind resets to `pending`," the exact site of bug #2, including
   today's added live-task-termination logic (`f9c50d72`), which stays as
   a wrapper around the centralized transition rather than being absorbed
   into it (terminating agents is a side effect the transition table
   itself shouldn't own).
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
`test_phase_manager.py`, `test_retry_exhausted_failed_workflows.py` — all
green as of `4d2f2005`) plus the new transition-table tests, deploy, watch
Step 1's invariant check for new violations before starting the next
site.

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
  retry vs. skip) — only who writes the resulting status and how
  consistently.

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
