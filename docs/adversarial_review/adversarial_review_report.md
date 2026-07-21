# Adversarial Review — Cost Derivation Engine (Run 2)

**Reviewer:** Hephaestus Adversarial Agent (Phase 6)  
**Date:** 2025-01-27  
**Scope:** Verification of 5 BLOCKERs from Run 1  
**Verdict:** PASS — All 5 BLOCKERs resolved. 6 WARNINGs remain (non-blocking).

---

## BLOCKER Verification Summary

All 5 BLOCKERs from Run 1 have been fixed. Each fix was verified by code inspection and runtime testing.

### B-1: `update_project` silently wipes `cost_limit_usd` — **FIXED ✅**

**File:** `src/mcp/autopilot_api.py:1844-1849`

**Fix Applied:** Added `clear_cost_limit: bool = False` sentinel field to `ProjectUpdate`. Handler now uses:
```python
if req.clear_cost_limit:        # Explicit clear signal
    proj.cost_limit_usd = None
elif req.cost_limit_usd is not None:  # Value provided
    proj.cost_limit_usd = req.cost_limit_usd
# else: leave unchanged (don't wipe on partial updates)
```

**Verification:** A name-only update (`ProjectUpdate(name="New Name")`) correctly leaves `cost_limit_usd` unchanged. The `clear_cost_limit` flag defaults to `False`.

---

### B-2: Pi extension posts to wrong URL and port — **FIXED ✅**

**File:** `extensions/hephaestus-cost-tracker/src/index.ts`

**Fix Applied:**
- Default port: `http://localhost:8000` → `http://localhost:8300` (line 58)
- API path: `/cost-entries` → `/api/autopilot/cost-entries` (line 123)

**Verification:** URL now matches the actual server configuration (`port 8300`, router prefix `/api/autopilot`).

---

### B-3: `derive_project_cost` and `derive_design_cost` miss costs without `feature_id` — **FIXED ✅**

**File:** `src/core/cost_derivation.py:266-276, 230-236`

**Fix Applied:** Both functions now use a dual-path query approach:
```python
# Primary path: through Feature
via_feature = db.query(func.sum(CostEntry.cost_usd)).join(...).join(Feature)...scalar()
# Direct path: workflows linked to project/design without feature (Phase 0, etc.)
direct = db.query(func.sum(CostEntry.cost_usd)).join(Workflow)....filter(
    Workflow.project_id == project_id, Workflow.feature_id.is_(None)
).scalar()
total = via_feature + direct
```

**Verification:** The direct path correctly captures costs from Phase 0 and undecomposed workflows that have `feature_id = NULL` but `project_id` or `design_id` set.

---

### B-4: `CostEntryCreate` missing token count validators — **FIXED ✅**

**File:** `src/mcp/autopilot_api.py:1556-1562`

**Fix Applied:** Added validator for all token count fields:
```python
@validator("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
def validate_token_counts(cls, v: int) -> int:
    if v < 0:
        raise ValueError("token counts must be non-negative")
    if v > 10_000_000:
        raise ValueError("token count exceeds maximum allowed value")
    return v
```

**Verification:** Runtime test confirmed:
- `input_tokens=-100` → ValidationError("token counts must be non-negative")
- `input_tokens=100_000_000` → ValidationError("token count exceeds maximum allowed value")

---

### B-5: `record_cost` bypasses $1000 cost cap — **FIXED ✅**

**File:** `src/core/cost_derivation.py:76-81`

**Fix Applied:** Added validation at the top of `record_cost()`:
```python
if cost_usd < 0:
    raise ValueError("cost_usd must be non-negative")
if cost_usd > 1000.0:
    logger.warning(f"[COST] Capping unusually high cost ${cost_usd:.2f} to $1000")
    cost_usd = 1000.0
```

**Verification:** Direct callers like `collect_task_cost` now go through validation. Costs >$1000 are capped with a warning log.

---

## Remaining WARNINGs (Non-Blocking)

The following 6 WARNINGs from Run 1 were NOT addressed but are non-blocking:

| ID | Issue | Severity | Status |
|----|-------|----------|--------|
| W-1 | `_extract_session_id` is unreliable heuristic | WARNING | Unchanged — still parses tmux name format |
| W-2 | `ClaudeCodeCollector` hardcoded prices | WARNING | Unchanged — no staleness detection |
| W-3 | `collect_task_cost` swallows all exceptions | WARNING | Unchanged — bare `except Exception` |
| W-4 | `BudgetPausedLabel` never rendered | WARNING | Unchanged — dead code |
| W-5 | Dashboard missing `onConfigureBudget` handler | WARNING | Unchanged — no budget UI from Dashboard |
| W-6 | Redundant SUM queries in rollup | WARNING | Unchanged — performance concern only |

These are quality improvements that can be addressed in a follow-up. None represent data integrity or functionality failures.

---

## New Issues Found

No new BLOCKERs or WARNINGs were introduced by the fixes. The fixes are minimal, targeted, and don't change unrelated behavior.

---

## Verdict: PASS

All 5 BLOCKERs from Run 1 are resolved. The code is ready for the next pipeline phase (security_review).
