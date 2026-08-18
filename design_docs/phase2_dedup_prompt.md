# Prompt: Phase 2, §4.1 — task-creation-claim/retry/arbitration consolidation

Paste this to the implementing agent as-is.

---

Execute Phase 2, §4.1 of `docs/AUTOPILOT_REFACTOR_PLAN.md`: consolidate the task-creation-claim/staleness-fallback, retry, and arbitration-dispatch duplication in `src/autopilot/orchestrator/phase_transitions.py`. This is the plan's highest-priority Phase 2 item — the single most-repeated bug class in this codebase's history (`AUTOPILOT_REFACTOR_ANALYSIS.md`'s Cross-Cutting Theme #2, "N-th independent implementation"). Unlike the rest of Phase 2, its safety net already exists: characterization tests for all three duplicate clusters were written in an earlier session pass and are currently green against the *unconsolidated* code. Your job is to make the consolidation land without changing what any of those tests assert — with one deliberate exception, spelled out below.

## Read first, in this order

1. `docs/AUTOPILOT_REFACTOR_PLAN.md` §4.1 (full text). It documents the exact current state, corrects an earlier false claim in this same plan that this work was already done during the orchestrator split (it wasn't — that split shipped as a pure move, verified directly against the code), and records that `fire_spec_gate_if_ready` has *since* been migrated into `phase_transitions.py` by Phase 1b. **Don't redo that migration — it's done.** But note: `fire_spec_gate_if_ready`'s own claim-handling logic (the `fe9a141` fix, a third independent copy of the GOTO-reset stale-execution query — see below) is now physically inside `phase_transitions.py` too, so it's in scope for this consolidation in a way it wasn't when §4.1 was first written.
2. `design_docs/backend_module_decomposition.md` §3.3 for this repo's scripted-verification methodology (not directly a file-move here, but the same discipline applies: verify against the live file with tooling, not memory or an earlier doc's line numbers).
3. `design_docs/phase_1b_decomposition.md` as the most recent worked example of this repo's expected rigor and reporting shape (freshness checks, exhaustive call-site tables, a findings log for anything out of scope).

**Do your own freshness check before touching anything.** `phase_transitions.py` changed materially when `fire_spec_gate_if_ready` (245 lines) was inserted into it during Phase 1b — every line number anyone has cited for this file in any earlier document, including this prompt, may now be stale. Re-derive exact line ranges for every symbol named below via `ast.parse()` before relying on them.

## Scope — four sub-problems, all in `phase_transitions.py`

### 1. Staleness-fallback three-way merge

`_release_stale_task_creation_claims` (the periodic-sweep version), `_create_phase_task`'s inline copy (its `target_already_claimed=False` branch), and `_create_corrective_task`'s inline copy all clear a stale `task_creation_claimed_at` past `CLAIM_STALE_TIMEOUT_SECONDS` (480s / 8 minutes, a module constant).

- The two inline copies (`_create_phase_task`, `_create_corrective_task`) are byte-identical: an ~8-line "bulk-clear the claim via `.update(...)`, then retry `_claim_phase_task_creation` once" pattern. Safe to merge into one shared helper.
- The sweep version does strictly more: it also repairs `PhaseExecution.status` (pending/completed → in_progress) if a task already exists for the stale-claimed phase, and backfills `started_at` from that task's own `created_at` (not `datetime.utcnow()` — anchoring it to "now" was a real historical bug, `75e1e52`).
- **This is a real design decision, not a mechanical dedup.** Either (a) extend the two simpler inline copies to also do the status/started_at repair, so all three call sites get full self-healing, or (b) keep the sweep version's extra repair work separate from a narrower shared "just clear the claim" helper the other two call. Make the choice explicitly, document *why* in the commit message, and don't default silently.

### 2. Retry consolidation — fix a live bug first, then consolidate

`_retry_failed_tasks` (workflow-wide sweep, retries every individual failed task it finds), `_maybe_retry_failed_tasks` (phase-scoped, fires only when 100% of a phase's tasks in the current execution cycle are failed, batch-resets them), and inline retry-cap-marking logic inside `_case_in_progress_complete` are three separate implementations with genuinely different trigger conditions — not simple duplicates of the same function. Understand each one's actual trigger condition before proposing how (or whether) to unify them; they may not fully collapse into one implementation the way the staleness-fallback trio can.

**Before touching any of this, fix `docs/AUTOPILOT_REFACTOR_PLAN.md` Phase 3 Tier 2 item 20 first, as its own separate, narrow commit:** `_retry_failed_tasks`'s orphan-retry-cap exemption is dead code. It reads `task.get("failure_reason")` from a dict returned by `get_tasks()` (`src/autopilot/orchestrator/engine_client.py`), but that function's returned dict never includes the `"failure_reason"` key at all — so `is_orphan` is always `False`, and an orphaned task (never dispatched — a scheduling issue, not an agent failure) gets silently capped at `max_task_retries` instead of retrying indefinitely, contradicting the function's own docstring and inconsistent with `_maybe_retry_failed_tasks`'s working equivalent for the same case.

- Fix: add `"failure_reason"` to `get_tasks()`'s returned dict (alongside its other per-task fields).
- Then flip the assertions in `tests/test_orchestrator_helpers.py::TestRetryFailedTasks::test_orphaned_task_incorrectly_capped_bug` — it currently asserts the *buggy* behavior on purpose (a characterization test documenting a known-live bug, not a spec). Once fixed, it should assert the task retries instead of getting capped. Update its docstring too; it explicitly says "flip its assertions once fixed."
- Only after this lands as its own commit should you start the broader three-implementation consolidation, so the bug fix and the refactor stay bisectable (this repo's stated principle: don't fix a bug and merge implementations in the same commit).

### 3. Arbitration-dispatch consolidation

`_fire_phase_transition` (normal evaluation path — calls `PhaseManager.mark_phase_complete` without `force_action`, then dispatches the resulting target phase) and `_resolve_arbitration_outcome` (forced-decision path — calls `mark_phase_complete` with `force_action` set from the arbiter's decision, then dispatches) share only their final `_create_phase_task` call; everything upstream of that differs by design (one goes through real evaluation, the other is post-arbitration).

- Their `feedback`-derivation logic differs and this is worth resolving explicitly: `_fire_phase_transition` special-cases a `result_missing` gate reason (`"no <phase>_report.md found"`) by substituting the completing task's own `completion_notes` when available, since that's a more accurate account of what happened than a generic "missing" message. `_resolve_arbitration_outcome` does a plain `result.get("reason")` pass-through with no such substitution.
- Decide whether `_resolve_arbitration_outcome` should gain the same `completion_notes`-substitution logic (probably yes, since the underlying problem — a stale/generic reason string reaching the next agent — applies equally to an arbitration-triggered dispatch), or whether there's a reason it shouldn't. Document the decision either way; don't silently pick one without noting it in the commit message.

### 4. GOTO-reset stale-execution query — three independent copies, now two files

A different bug from the claim triad above: it resets `PhaseExecution` *rows* on a goto, not task-*creation* claims. The correct scope for "which executions need resetting" was independently re-derived three times historically: `6d66e0d` (2026-06-26, first version, too narrow), `e15042f` (2026-07-15, widened but then caught the firing phase's own just-closed execution in its net), `084edcf` (2026-08-14, added `PhaseExecution.id != execution.id` to fix that self-exclusion gap) — and the *same* self-exclusion gap was found a third time, the same day, in a third independent copy of this query inside what's now `fire_spec_gate_if_ready` (`fe9a141`). Since that function physically moved into `phase_transitions.py` during Phase 1b, both remaining copies are now in the same file.

- Extract one shared `reset_stale_executions_on_goto(db, target_phase, *, exclude_execution_id)` helper (or equivalent — the plan doc's proposed signature, verify it still fits both call sites' actual needs) and migrate both copies to call it.
- Write a characterization test asserting (a) a goto never re-resets its own firing phase's just-closed `PhaseExecution` row, and (b) a phase at or after the goto target — not just strictly between target and source — gets reset. Both should fail against the current triple-implementation state and pass once the shared helper lands, per the plan doc's own verification note for this item.

## Tests that must stay green, assertions unchanged (except the one flip in §2 above)

- `tests/test_orchestrator_helpers.py::TestCreatePhaseTaskStaleClaimFallback`
- `tests/test_orchestrator_helpers.py::TestCreateCorrectiveTask`
- `tests/test_orchestrator_helpers.py::TestRetryFailedTasks` (all except the one test named above)
- `tests/test_advance_phases.py::TestReleaseStaleTaskCreationClaims`
- `tests/test_advance_phases.py::TestMaybeRetryFailedTasks`
- `tests/test_advance_phases.py::TestFirePhaseTransition`
- `tests/test_advance_phases.py::TestFirePhaseTransitionArbitrate`
- `tests/test_advance_phases.py::TestResolveArbitrationOutcome`
- `tests/test_phase_transitions_spec_gate.py` (the relocated `fire_spec_gate_if_ready` goto-regression tests, moved here during Phase 1b)

If any of these turns out, on closer reading, to be characterizing a bug rather than intended behavior (the way `test_orphaned_task_incorrectly_capped_bug` did), say so explicitly and get a decision before changing its assertions — don't assume you've found another one and just change it.

## Explicitly out of scope — do not touch

- Anything already shipped by Phase 1b (the `orchestrator.py`, `autopilot_api.py`, `api.py`→`frontend/`, `MonitoringLoop`, or `task_completion_service.py` splits, or `create_agent_for_task`/`restart_agent`'s internal step methods). This prompt is scoped to `phase_transitions.py`'s dedup work only.
- `manager.py`'s file-level split (separate, parallel task — see `design_docs/manager_py_decomposition_prompt.md` if that hasn't landed yet; don't wait on it, these are independent).
- Any other Phase 2 item (§4.2 through §4.11). If your work here surfaces something clearly belonging to one of those (e.g. a termination-invariant gap), log it, don't fix it.

## Quality bar, matching the four Phase 1b targets

- Adversarial review against HEAD before calling anything done — verify every claim (including this prompt's own line-number-free descriptions above) against the actual current code via `git show`/direct reads, not assumption.
- `ruff check` clean on every touched file.
- Full targeted-test verification for every test file listed above, not just a subset, plus a full-suite gate against the pristine-HEAD baseline (strict subset of pre-existing failures, zero regressions) — same methodology `FINAL-SUMMARY.md` used for Phase 1b.
- Anything found outside this scope goes in a findings doc (same pattern as Phase 1b's `FINAL-SUMMARY.md`/`status.md`), not fixed inline.
- No commits — leave everything in the working tree for review, same as Phase 1b.
