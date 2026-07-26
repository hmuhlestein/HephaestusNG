---
type: adversarial_review_result
feature_id: des-91c8-pi-extension
verdict: PASS
blocker_count: 0
warning_count: 1
nit_count: 1
---

# Adversarial Review: Pi Cost Tracker Extension — Verification Pass

This run verifies the fixes for the 2 BLOCKERs from the prior adversarial
review run (development commit `2f9fc73`, `src/services/cost_collection_service.py`
and `extensions/hephaestus-cost-tracker/README.md`). Per instructions,
this is a verify-only pass, not a from-scratch re-review — but one new,
narrower issue introduced by the fix itself is reported below.

## Prior BLOCKER 1 — real-time pi extension costs double-counted by the JSONL fallback tailer

**Was:** `collect_task_cost` unconditionally re-tailed the entire session
JSONL transcript at task completion regardless of whether the pi
extension had already posted the same turns' costs in real time via
`POST /api/autopilot/cost-entries`, because `SessionCostCheckpoint` was
never touched by the real-time path — so every turn got recorded twice
whenever the extension was working normally, contradicting the README's
claim that double-counting was prevented.

**Fix verified:** `cost_collection_service.py:436-451` now checks, before
any JSONL discovery/tailing happens, whether any `CostEntry` with
`source="pi"` already exists for this `task_id`
(`db.query(CostEntry).filter_by(task_id=task_id, source="pi").first()`).
If so, the function returns immediately — the JSONL fallback is skipped
entirely for that task, since the extension is proven active and is
treated as the sole source of truth. Confirmed by reading the code
directly and by the new test
`TestCollectTaskCostRealtimeVsFallback::test_skips_jsonl_fallback_when_realtime_pi_entries_exist`,
which passes. The complementary case — no real-time entries exist, JSONL
fallback still runs — is covered by
`test_jsonl_fallback_still_runs_when_no_realtime_entries_exist`, also
passing. `extensions/hephaestus-cost-tracker/README.md:47-52` was updated
to describe this actual behavior accurately (skip-if-already-posted)
instead of the previous false "`SessionCostCheckpoint` prevents
double-counting" claim.

Verdict: fixed. See W-1 below for a narrower, lower-severity gap the fix
itself introduces.

## Prior BLOCKER 2 — one bad entry silently discarded an entire task's cost batch

**Was:** all entries in a `collect_task_cost` batch were written inside a
single implicit transaction; any exception from `record_cost()` for one
entry rolled back every entry already added in that loop plus the
checkpoint update, and the sole caller
(`TaskCompletionService.collect_cost_on_completion`) swallowed the
exception with only `logger.warning` — permanent, silent loss of the
whole task's cost data with no retry path.

**Fix verified:** `cost_collection_service.py:535-559` now wraps each
`record_cost()` call in its own try/except and calls `db.commit()` per
entry (line 552); a failure rolls back and logs only that one entry
(`logger.error`, line 556) and the loop continues to the next entry. A
summary line reports `failed_count` if any occurred (line 558-559). The
checkpoint update (lines 561-573) runs after the loop regardless, so
successfully-recorded entries and the checkpoint advance are preserved
even if some entries failed. Confirmed by reading the code and by the new
test `TestCollectTaskCostPartialFailure::test_bad_entry_does_not_discard_rest_of_batch`,
which passes.

Verdict: fixed.

## Test verification

`python -m pytest tests/test_cost_collection_service.py -q` → 23 passed,
0 failed (includes the 3 new tests targeting these two fixes).

## WARNING findings

### W-1 (new, introduced by the B-1 fix): an "any real-time entry exists" check is coarser than per-turn, so a single failed real-time POST can now cause a turn's cost to be silently dropped rather than picked up by the fallback

The fix (`cost_collection_service.py:447-451`) skips the *entire* JSONL
fallback for a task the moment *any* `source="pi"` entry exists for it.
The extension's real-time POST is fire-and-forget with no retry
(`index.ts:109-112`, `console.warn` only on failure). So: if turn 1's POST
succeeds, turn 2's POST fails (e.g. a transient API restart mid-session),
and turn 3's POST succeeds, then a `source="pi"` entry exists for the
task (from turns 1 and 3), so the JSONL fallback is skipped wholesale —
turn 2's cost is never picked up by either path and is permanently lost.
This is a real, different-from-before gap, but meaningfully lower severity
than the fixed BLOCKER: it can drop at most the individual turns whose
real-time POST failed, not double-count (and cascade-inflate budget
enforcement on) the entire task. Not blocking this gate. Recommended fix
for a future pass: track coverage at finer granularity than "any entry
exists for this task" — e.g. have the JSONL collector skip only the
specific turns/lines already represented by a `source="pi"` entry
(would need a per-turn identifier on `CostEntry`, which doesn't exist
today), rather than an all-or-nothing per-task boolean.

## NIT findings

### N-1 (carried, not addressed — informational only)

`collect_task_cost` (`cost_collection_service.py:465-506`) still embeds
per-CLI-type, low-level session-file-discovery logic (the `if cli_type ==
"pi": ... elif cli_type == "claude_code": <path sanitization/glob> ...`
chain) directly in the high-level orchestration function rather than
pushing it into `CostCollector` subclasses via polymorphism. Unchanged by
this fix pass; not a blocker or warning on its own, noted for a future
composition cleanup.

## Gate recommendation

**PASS.** 0 blockers. Both prior BLOCKERs are verified fixed in code and
covered by new passing tests. One new WARNING (W-1) is a narrower,
lower-severity residual gap introduced by the fix's per-task (rather than
per-turn) granularity — worth a follow-up ticket, not worth blocking this
gate over, since it replaces a systemic double-counting/budget-inflation
bug with a much smaller single-turn-loss edge case that only occurs on
already-rare mid-session API failures.
