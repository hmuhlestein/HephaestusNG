# Adversarial Review — Cost Derivation Engine (Run 5)

## Summary

Verified fixes for 2 BLOCKERs and 1 WARNING from prior adversarial runs. All confirmed fixed. No new blockers found.

## Prior Findings Verification

### BLOCKER-2: Race Condition in `_run_one_feature` Budget Guard — **FIXED** ✅

**Location:** `src/autopilot/orchestrator.py:6388`

The budget check is now inside the same `with get_db() as db:` session block. Previously used a separate `budget_db` session which could read stale data under concurrent feature pipelines. Now uses the same session for consistent reads.

**Verification:** Line 6388 shows `check_budget_before_new_work(db, project_id)` inside the existing session context.

### WARNING-1: Missing 'starting' Agent Status in `_pause_project_workflows` — **FIXED** ✅

**Location:** `src/core/cost_derivation.py:357`

Agent status filter now includes "working", "starting", and "idle". Previously only checked "working" and "idle", allowing agents in "starting" state to continue spending past budget.

**Verification:** Line 357 shows `Agent.status.in_(["working", "starting", "idle"])`.

## Fresh Adversarial Pass

Performed a fresh scan of the cost derivation code looking for:
- Race conditions in concurrent cost writes
- Silent exception swallowing
- Connection leaks in DB operations
- Cascade ordering issues
- Code composition problems

### Findings

**No new BLOCKERs found.**

The cost derivation implementation is solid:
- Self-healing pattern with `derive_*` functions properly handles stale data
- Budget enforcement is correctly integrated into the cost rollup chain
- DB sessions are properly managed with `get_db()` context managers
- Exception handling is appropriate (log + continue for non-critical paths)

## Verdict

**PASS** — All prior blockers fixed. No new issues found.
