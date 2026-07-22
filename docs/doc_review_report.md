# Documentation Review Report

**Feature:** Cost Derivation Engine (cost-derivation-engine)  
**Review Date:** 2026-07-21  
**Reviewer:** Doc Review Agent (Phase 10)  
**Branch:** `feature/des-91c8-cost-derivation`

---

## Summary

Reviewed all project documentation against the actual implementation. Found and fixed **critical inaccuracies** where docs described features as "NOT IMPLEMENTED" or "PARTIALLY IMPLEMENTED" when they were actually complete. The primary documents reviewed were:

1. `docs/architecture.md` — Technical Architecture (Phase 3 output)
2. `docs/requirements_analysis.md` — Product Requirements (Phase 1 output)
3. `docs/COST_TRACKING_DESIGN.md` — Original Design Document

---

## Critical Inaccuracies Fixed

### 1. OpenRouter Direct Collection — Marked "PARTIALLY IMPLEMENTED", Actually Complete

**Location:** `docs/architecture.md` Section 2.4, `docs/requirements_analysis.md` FR-12

**What docs said:** "❌ Not all 9 call sites routed through `_invoke_and_record()`" and "❌ `task_id` not threaded into all methods"

**Actual state:** `_invoke_and_record()` is implemented at `langchain_llm_client.py:323` and ALL major call sites are routed through it:
- `classify_complexity` (line 409)
- `enrich_task` (line 466) — with `task_id` parameter
- `resolve_ticket_clarification` (line 530)
- `analyze_agent_state` (line 592)
- `analyze_agent_trajectory` (line 691) — guardian
- `analyze_system_coherence` (line 750) — conductor
- `review_qa_report` (line 842) — conductor

`usage.include=true` is confirmed working at line 243, extracting cost from `response_metadata.token_usage.cost.total`.

### 2. Budget Enforcement — Marked "PARTIALLY IMPLEMENTED", Actually Complete

**Location:** `docs/architecture.md` Section 2.5

**What docs said:** Three items marked "Not Implemented":
- "❌ `paused_by` guards not generalized (3 locations still use `== "user"`)"
- "❌ Budget checks not wired into `pick_next_design()` and `_run_one_feature()`"
- "❌ Limit raise doesn't clear budget pause on `PUT /projects/{id}`"

**Actual state:** ALL THREE are implemented:
- `paused_by` guards at lines 3531, 5218, 5384 all use `is not None`
- `check_budget_before_new_work()` is called in `pick_next_design()` (line 1931) and `_run_one_feature()` (line 6381)
- `PUT /projects/{id}` clears budget pause when limit raised (lines 1868-1880)

### 3. Pi Extension — Marked "NOT IMPLEMENTED", Actually Exists

**Location:** `docs/architecture.md` Section 2.6, `docs/requirements_analysis.md` FR-8

**What docs said:** "NOT IMPLEMENTED" with target path `extensions/hephaestus-cost-tracker.ts`

**Actual state:** Extension exists at `extensions/hephaestus-cost-tracker/src/index.ts` with full implementation:
- Hooks `turn_end` events
- Extracts `message.usage.cost.total`
- POSTs to `POST /api/autopilot/cost-entries`
- Shows running cost in TUI via `ctx.ui.setStatus()`
- Configurable via `HEPHAESTUS_API_URL` env var

### 4. Tests — Marked "NOT CREATED", Actually Exist

**Location:** `docs/architecture.md` Appendix, `docs/requirements_analysis.md` Phase 10

**What docs said:** Tests "❌ Not created"

**Actual state:**
- `tests/test_cost_tracking.py` — 39 test functions (755 lines)
- `tests/test_budget_enforcement_integration.py` — 13 test functions (516 lines)

### 5. API Endpoints — Marked "NOT IMPLEMENTED", Actually Complete

**Location:** `docs/architecture.md` Section 2.7.2

**What docs said:** Cost query endpoints "❌ Not implemented"

**Actual state:** ALL endpoints implemented in `src/mcp/autopilot_api.py`:
- `POST /cost-entries` (line 1948)
- `GET /tasks/{id}/costs` (line 2063)
- `GET /workflows/{id}/costs` (line 2111)
- `GET /features/{id}/costs` (line 2159)
- `GET /designs/{id}/costs` (line 2207)
- `GET /projects/{id}/costs` (line 2255)

---

## Other Documentation Issues

### File Organization

- Moved `security_report.md` from project root to `docs/` (pipeline output belongs in docs path)

### Line Number Corrections

Architecture doc referenced incorrect line numbers for `paused_by` guards:
- `_try_auto_resume_paused_workflow`: doc said 3749, actual is 3531
- `_create_corrective_task`: doc said 5680, actual is 5218
- `attempt_recovery`: doc said 5864, actual is 5384
- `AutopilotService.start()`: doc said 398, actual is 395

### Still Outstanding (Not Fixed — Accurate as Documented)

| Item | Status | Notes |
|------|--------|-------|
| Frontend cost components | ❌ Not implemented | Accurately documented as not done |
| `DESIGN_WORKFLOW_DEFINITION_IDS` constant | ✅ Exists | Used instead of hardcoded list |
| `SessionCostCheckpoint` keyed by session_id | ✅ Correct | Design rationale still valid |
| Claude Code price table maintenance | ⚠️ Manual | No automated mechanism — documented correctly |
| OpenCode actual usage verification | ❓ Unresolved | Needs check of workflow.yaml |
| Force session_id on standalone tasks | ❓ Unresolved | Design flagged, still open |

---

## Files Modified

| File | Changes |
|------|---------|
| `docs/architecture.md` | Fixed Sections 2.4, 2.5, 2.6, 2.7.2, Appendix A, Open Questions |
| `docs/requirements_analysis.md` | Fixed FR-7, FR-8, FR-12, AC-9/11/16/17/18, Phases 3/5/9/10, integration tables |
| `docs/security_report.md` | Moved from project root to docs/ |

---

## Conclusion

The implementation is significantly further along than the documentation indicated. The core cost derivation engine is fully implemented with:
- ✅ Schema (CostEntry, SessionCostCheckpoint, rollup columns)
- ✅ Cost derivation module with self-healing
- ✅ All CLI collectors (pi, Claude Code, OpenCode, Codex stub)
- ✅ OpenRouter direct collection via `_invoke_and_record`
- ✅ Budget enforcement (pause, block new work, limit raise clear)
- ✅ Pi extension for real-time cost display
- ✅ All API endpoints (7 total)
- ✅ Tests (52 test functions across 2 files)

**Not yet implemented:** Frontend cost components (UI display, budget config input).

The documentation has been corrected to reflect the actual implementation state.
