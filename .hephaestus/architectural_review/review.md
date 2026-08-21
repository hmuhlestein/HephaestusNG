---
type: architectural_review
feature_id: des-91c8-pi-extension
verdict: PASS
blocker_count: 0
fix_count: 0
defer_count: 3
---

# Architectural Review: Pi Cost Tracker Extension

**Reviewer:** Architect (design author — re-review after adversarial/security/QA phases)
**Feature:** des-91c8-pi-extension
**Branch:** feature/des-91c8/pi-extension (00fd619)
**Design artifacts:** `docs/architecture.md`, `docs/requirements_analysis.md`
**Base commit for diff:** ec6dcd3 (architecture_design output)

## Summary

- **BLOCKERS:** 0 — no architecture invariant violations
- **FIX:** 0 — deviations from architecture scope were all justified by adversarial/security findings
- **DEFER:** 3 — minor items
- **Overall:** PASS

The architecture scoped this feature to a single README line-fix plus two
verification-only tasks. The development phase correctly executed that scope
(commit 618804b). Subsequent adversarial review then discovered two real
bugs in the *pre-existing* `cost_collection_service.py` code (double-counting
between the extension's real-time POST and the JSONL fallback; batch data
loss on partial `record_cost` failure), and a security review found a High-
severity suppression vector. Development fixed all three. These changes touch
files the architecture explicitly said not to touch (`cost_collection_service.py`,
`index.ts`), but the architecture's prohibition was premised on the code being
"already correct, tested-by-inspection" — the adversarial and security reviews
proved that assumption wrong. The fixes are correct, tested, and necessary.

## Findings

### Scope Analysis (non-finding — architecture's "do not touch" directive evaluated)

Architecture §2 stated:

> Development should not use this phase as license to touch
> `cost_collection_service.py`, `cost_derivation.py`, the `CostEntry` schema,
> budget enforcement, or the extension's `index.ts` logic. All of that is
> correct, tested-by-inspection, and out of scope.
>
> Decision: the only file this feature edits is
> `extensions/hephaestus-cost-tracker/README.md`.

**Post-development reality:** the code was *not* correct. Adversarial review
found:

1. **B-1 (double-counting):** `collect_task_cost` always ran the JSONL
   fallback regardless of whether the extension had already POSTed real-time
   `source="pi"` CostEntry rows for the same turns. Every pi session with the
   extension active recorded every turn twice. Fixed at `cost_collection_service.py:436-458`.

2. **B-2 (batch data loss):** a single `record_cost` exception mid-batch
   called `db.rollback()`, silently discarding all entries already flushed in
   that collection pass and skipping the checkpoint update — permanently losing
   the task's cost data with no retry path. Fixed at `cost_collection_service.py:536-568`.

3. **Security (High, ticket-5a75167a):** a forged `source="pi"` CostEntry
   from an unrelated agent_id could suppress the real-time check and cause
   the task's real JSONL-derived costs to be collected twice or zero times.
   Fixed by scoping the realtime check to `agent_id=agent.id`
   (`cost_collection_service.py:453-456`).

4. **pi API breaking change:** `ctx.ui.setStatus(message)` →
   `ctx.ui.setStatus(key, text)`. Without this fix, the extension crashes on
   every `initialize` and `turn_end` call. Fixed in `index.ts:15,71,90`.

All four fixes are correct, tested (24/24 passing), and ruff-clean. The
architecture's "do not touch" directive was wrong — not scope creep. The
adversarial/security phases have authority to override a narrow scope when real
defects surface. No BLOCKER or FIX classification against the implementation.

### Task 1: README.md POST path fix (architecture §3)

**Architecture specified:** change line 44 from `POST /cost-entries` to
`POST /api/autopilot/cost-entries`.

**Implementation:** exactly that change at line 44, plus a rewrite of the
"Fallback Behavior" section to describe the double-counting prevention
behavior that was added in the adversarial fix. The Fallback Behavior section
now correctly describes reality (extension-active sessions skip JSONL tailing)
rather than the previous incorrect claim ("SessionCostCheckpoint prevents
double-counting").

Verdict: compliant. The additional Fallback Behavior text is correct
documentation of existing behavior, not scope creep.

### Task 2: Live pi-install verification (architecture §6/7)

**Architecture specified:** verify under real `pi` if available; if not,
file as accepted risk.

**Implementation:** `docs/implementation_status.md` correctly documents no
`pi` binary is available in the sandbox and files this as an accepted risk.

Verdict: compliant.

### Task 3: Regression check (architecture §7)

**Architecture specified:** re-run
`tests/test_cost_collection_service.py` and `tests/test_cost_tracking.py`.

**Verified:** `pytest tests/test_cost_collection_service.py` — 24/24 pass.
The adversarial-review additions (3 new test classes) provide regression
coverage for the bugs that were fixed. `test_cost_tracking.py` still has a
pre-existing `_pause_project_workflows` ImportError unrelated to this feature
(per the original architectural review, DEFER #1).

Verdict: compliant.

### Component Boundaries

No new components introduced. The extension's boundary (TypeScript, hooks
`turn_end`, POSTs to API) is unchanged. The Python-side boundary
(`collect_task_cost` called from `task_completion_service`) is unchanged.
The double-counting guard only adds a DB query at the entry of
`collect_task_cost` — no new coupling introduced.

### Interface Contracts

- `POST /api/autopilot/cost-entries` contract unchanged (path, headers,
  body shape, auth via `X-Agent-ID`, rate limiting at 60/min per client IP).
  Verified against `autopilot_api.py:2144-2196`.
- `CostEntry` schema unchanged — no new columns added (no `session_id`,
  per requirements §9).
- `record_cost(db, ...)` contract unchanged — still flushes + derives,
  caller commits.

### Data Flow

The architecture's data flow (§5) is unchanged in shape:
```
pi extension turn_end → POST /api/autopilot/cost-entries → record_cost → derivation rollup
JSONL fallback at task completion → collect_task_cost → collector.collect → record_cost → derivation rollup
```
The new addition is the early-exit guard in `collect_task_cost` that checks
for existing realtime pi entries before invoking the JSONL collector. This
is a refinement of the existing flow, not a new path.

### Design Invariants

All design invariants from `requirements_analysis.md` hold:

- Extension never blocks pi turns on failure ✓ (fire-and-forget POST with
  catch, `console.warn` only)
- `install.sh` never aborts on extension build failure ✓ (not touched)
- No `session_id` field added anywhere ✓
- No JS/TS test framework introduced ✓
- Cost attribution uses env vars (`HEPHAESTUS_AGENT_ID`/`TASK_ID`/`WORKFLOW_ID`) ✓

### Over-Engineering Check

None. The per-entry commit/rollback pattern is proportional to the problem
(it prevents batch data loss). The realtime-pi-detection guard is a single
query. No new abstractions, no speculative config, no framework additions.

### Under-Engineering Check

None from the architecture's scope. Pre-existing gaps noted as DEFER below.

## Architecture Deviations

None that constitute a design violation. The architecture's stated boundary
("only README.md, do not touch cost_collection_service.py") was a deliberate
scope restriction based on the premise that the existing code was correct.
That premise was falsified by the adversarial and security reviews. The
pipeline's review phases have the authority and responsibility to fix real
bugs, and development correctly responded to their findings.

## Design Invariants

All hold. No invariant violations found.

## Assumptions & Gaps

1. The architecture assumed the existing `cost_collection_service.py` was
   correct. It wasn't — it had double-counting and batch data loss bugs.
   The adversarial review caught this; the architecture did not.
2. The architecture assumed pi's extension API (`setStatus`) was stable.
   It changed. The development phase tracked the change.
3. `record_cost`'s `cost_usd > 1000.0` cap silently truncates genuinely
   expensive turns. Not in this feature's scope but worth noting.

## Positive Observations

1. **Adversarial review worked as designed.** The pipeline correctly
   identified bugs that the architecture missed. The "trust existing code"
   assumption was tested and found wanting.
2. **Security review caught a real High-severity suppression vector.** The
   agent-scoping fix at `cost_collection_service.py:453` is correct and
   well-scoped — it doesn't over-correct by blocking legitimate cross-task
   entries.
3. **Per-entry commit/rollback is the right pattern** for batch cost
   collection. A permanently-bad JSONL line would otherwise block all future
   cost collection for the session (infinite retry on the same bad checkpoint).
4. **Tests are well-structured.** The `TestCollectTaskCostRealtimeVsFallback`
   and `TestCollectTaskCostPartialFailure` classes use in-memory SQLite fixtures,
   mock the DB session, and test the exact interaction paths that the bugs
   existed in. The `test_unrelated_agent_entry_does_not_suppress_fallback` test
   specifically validates the security fix.
5. **The extension code (`index.ts`) is clean and minimal.** Fire-and-forget
   POST, no blocking, graceful error handling. Matches the architecture's
   intent exactly.
