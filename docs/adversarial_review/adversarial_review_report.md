# Adversarial Review — Cost Derivation Engine (Run 3)

**Reviewer:** Hephaestus Adversarial Agent (Phase 6)  
**Date:** 2025-07-21  
**Scope:** Verification of 5 BLOCKERs from Run 1, plus security review and QA fixes  
**Verdict:** PASS — All 5 BLOCKERs remain fixed. Security review and QA fixes verified. No new BLOCKERs.

---

## BLOCKER Verification Summary

All 5 BLOCKERs from Run 1 remain fixed. No regressions from security review (commit 4c00a39) or QA fixes (commit 7aeb0c9).

### B-1: `update_project` silently wipes `cost_limit_usd` — **STILL FIXED ✅**

**File:** `src/mcp/autopilot_api.py:1521, 1866`

The `clear_cost_limit: bool = False` sentinel field persists. Handler logic unchanged:
```python
if req.clear_cost_limit:        # Explicit clear signal
    proj.cost_limit_usd = None
elif req.cost_limit_usd is not None:  # Value provided
    proj.cost_limit_usd = req.cost_limit_usd
# else: leave unchanged
```

**Runtime verification:** `ProjectUpdate(name='Test')` correctly leaves `cost_limit_usd` unchanged.

---

### B-2: Pi extension posts to wrong URL and port — **STILL FIXED ✅**

**File:** `extensions/hephaestus-cost-tracker/src/index.ts:58, 123`

Default port remains `http://localhost:8300`. Path remains `/api/autopilot/cost-entries`.

---

### B-3: `derive_project_cost` and `derive_design_cost` miss costs without `feature_id` — **STILL FIXED ✅**

**File:** `src/core/cost_derivation.py:230-236, 266-277`

Dual-path queries persist (via_feature + direct for feature_id IS NULL). No changes to this file since Run 2.

---

### B-4: `CostEntryCreate` missing token count validators — **STILL FIXED ✅**

**File:** `src/mcp/autopilot_api.py:1558`

Validator for all token count fields persists. Runtime verification confirms:
- `input_tokens=-100` → ValidationError
- `input_tokens=100_000_000` → ValidationError

---

### B-5: `record_cost` bypasses $1000 cost cap — **STILL FIXED ✅**

**File:** `src/core/cost_derivation.py:77-79`

Validation at top of `record_cost()` persists. No changes to this file since Run 2.

---

## Security Review Fixes Verified

The security review (commit 4c00a39) added 5 critical/high fixes. All verified:

| # | Fix | Status | Verification |
|---|-----|--------|--------------|
| 1 | Authentication on cost query endpoints | ✅ | All 5 GET endpoints require X-Agent-ID |
| 2 | raw_usage size limit (10KB) | ✅ | Runtime: >10KB rejected |
| 3 | model string length limit (200 chars) | ✅ | Runtime: >200 chars rejected |
| 4 | Rate limiting on cost entry creation | ✅ | 60 requests/minute per agent |
| 5 | pi-extension in KNOWN_SYSTEM_AGENTS | ✅ | Listed in server.py:443-452 |

**Frontend updated:** All 5 cost API calls in `frontend/src/services/api.ts` now include `X-Agent-ID: ui-user` header.

---

## QA Validation Verified

The QA validation (commit 71ba2e1) confirmed:
- **52/52 feature-specific tests pass** (39 unit + 13 integration)
- **103/109 smoke tests pass** (6 pre-existing failures unrelated to cost tracking)
- All design requirements verified

---

## Remaining WARNINGs (Non-Blocking)

The following 6 WARNINGs from Run 1 remain non-blocking:

| ID | Issue | Severity | Status |
|----|-------|----------|--------|
| W-1 | `_extract_session_id` is unreliable heuristic | WARNING | Unchanged |
| W-2 | `ClaudeCodeCollector` hardcoded prices | WARNING | Unchanged |
| W-3 | `collect_task_cost` swallows all exceptions | WARNING | Unchanged |
| W-4 | `BudgetPausedLabel` never rendered | WARNING | Unchanged |
| W-5 | Dashboard missing `onConfigureBudget` handler | WARNING | Unchanged |
| W-6 | Redundant SUM queries in rollup | WARNING | Unchanged |

**New minor observations (non-blocking):**

| ID | Issue | Severity |
|----|-------|----------|
| N-1 | Rate limit store is in-memory only (resets on restart, no multi-process sync) | NIT |
| N-2 | Rate limit keys never removed from dict (minor memory leak over long uptime) | NIT |

---

## New Issues Found

No new BLOCKERs or WARNINGs were introduced by the security review or QA fixes.

---

## Verdict: PASS

All 5 BLOCKERs from Run 1 remain fixed. Security review and QA fixes verified. The code is ready for the next pipeline phase (product_validation).
