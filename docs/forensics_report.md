# Forensics Report: Cost Tracking Database Schema

**Date:** 2026-07-21
**Workflow ID:** af451d18-d3c7-4a3e-9c58-9c1ed72fc0ad
**Feature:** Cost Tracking Database Schema (feature/des-91c8/cost-schema)
**Pipeline Status:** Completed — all phases passed
**Total Agent Invocations:** 21 across 10 phases
**Total Wall Time:** ~11.5 hours (00:09 to 11:40 CDT, includes 7.5h overnight gap)
**Active Pipeline Time:** ~4 hours

---

## 1. Pipeline Metrics

| Phase | Runs | Agents | Duration | Verdict |
|-------|------|--------|----------|---------|
| product_requirements | 2 | 382dae99, 8814f569 | ~7 min | PASS |
| scope_review | 1 | c07573ff | ~7 min | PASS |
| architecture_design | 1 | f1ff3723 | ~13 min | PASS |
| development | 6 | 1099b9f6, 0fbf885e, 693b21a7, bf7be014, 1838b5f7, f897a332 | ~15 min initial + fix cycles | NEEDS_WORK → PASS |
| architectural_review | 4 | 4addea62, 9c8fc089, ee910346, 707dd71e | ~20 min total | NOT_READY → PASS (Run 3) |
| adversarial_review | 3 | 8230b3e0, 9ac44855, aa109fb1 | ~19 min total | 5 BLOCKERs → PASS (Run 3) |
| security_review | 1 | 9fc7bc2a | ~35 min | COMPLETE (3 vulns fixed) |
| qa_validation | 1 | 57c5a7af | ~44 min | PASS (39/39) |
| product_validation | 1 | 1ea6ca05 | ~21 min | NEEDS_WORK (3 unmet FRs) |
| doc_review | 1 | a8c6a566 | ~30 min | COMPLETE |

**Total invocations:** 21 (product_requirements ×2, scope_review ×1, architecture_design ×1, development ×6, architectural_review ×4, adversarial_review ×3, security_review ×1, qa_validation ×1, product_validation ×1, doc_review ×1)

---

## 2. Review-Fix-Verify Cycle Analysis

The dominant pipeline pattern was the **review-fix-verify cycle**, consuming 13 of 21 invocations (62%):

```
development v1 → architectural_review v1 (5 BLOCKERs, 4 FIXes)
  → development v2 (fix 5B + 4F) → architectural_review v2 (1B remaining)
    → development v3 (fix remaining) → architectural_review v3 (CLEAN)
      → adversarial_review v1 (5 NEW BLOCKERs)
        → development v4 (fix 5B) → adversarial_review v2 (1B remaining)
          → development v5 (fix residual) → adversarial_review v3 (CLEAN)
```

**Key insight:** Both review phases found distinct, non-overlapping bugs. The architectural review caught integration gaps (missing wiring, missing endpoints, missing guards) while the adversarial review caught correctness bugs (transaction boundaries, N+1 queries, falsy zero logic). This validates the two-pass review design.

---

## 3. Agent Performance Assessment

### 3.1 Excellent Performance

| Phase | Agent | Notes |
|-------|-------|-------|
| scope_review | c07573ff | Single-pass clean. Faithful comparison against design doc. |
| architecture_design | f1ff3723 | Comprehensive 12-task breakdown with dependency graph. |
| qa_validation | 57c5a7af | 39/39 tests pass. Structured qa_result.json on first attempt. |
| security_review | 9fc7bc2a | Found and fixed 3 real vulnerabilities. Despite MCP issues. |

### 3.2 Good Performance (needed review cycles, which is expected)

| Phase | Agent | Notes |
|-------|-------|-------|
| development (all) | Various | Each fix cycle was targeted and correct. Tests stayed green. |
| architectural_review | Various | Reviewers were thorough. Finding severity was appropriate. |
| adversarial_review | Various | Found real bugs (transaction boundaries, N+1, falsy logic). |

### 3.3 Issues Encountered

| Phase | Agent | Issue | Impact |
|-------|-------|-------|--------|
| product_requirements | 382dae99 | First agent; 8814f569 was the retry | ~3 min delay |
| security_review | 9fc7bc2a | MCP connection failures — fell back to curl for task update | No data loss, but fragile |
| doc_review | a8c6a566 | Same MCP connection issue — fell back to curl | No data loss, but fragile |

---

## 4. Stuck/Crashed Agents

**No stuck or crashed agents detected.** All 21 invocations completed and updated task status. The overnight gap (00:46 to 08:17) was a scheduled pipeline pause, not a stuck agent.

**MCP Connection Degradation:** Security review and doc review agents experienced MCP server disconnection mid-run. Both recovered by falling back to direct HTTP calls. Root cause: long-running sessions may lose MCP websocket connection. The agents' self-healing fallback (curl to localhost:8300) worked but is brittle.

---

## 5. Common Issue Patterns Cataloged

### 5.1 Transaction Boundary Violations (Found by: adversarial_review, 3 occurrences)

**Pattern:** derive_* functions each calling `db.commit()` independently instead of letting the caller control the transaction boundary.

**Root cause:** Development agent followed existing patterns in the codebase (status_derivation.py uses commits) without considering that cost derivation needs atomic multi-table updates.

**Frequency:** This is a recurring pattern in SQLAlchemy codebases. The adversarial reviewer found it on Run 1, and a residual instance in `_pause_project_workflows` on Run 2.

### 5.2 Missing Integration Wiring (Found by: architectural_review, 4 occurrences)

**Pattern:** Core modules implemented in isolation but not connected to the rest of the system:
- task_completion_service.py not calling collect_task_cost
- langchain_llm_client.py not routing through _invoke_and_record
- No POST /cost-entries endpoint
- Missing budget guards on pick_next_design/_run_one_feature

**Root cause:** Development focused on new files (cost_derivation.py, cost_collection_service.py) but under-invested in modifying existing integration points.

### 5.3 Falsy Zero Bugs (Found by: adversarial_review, 1 occurrence)

**Pattern:** `if proj.cost_total_usd and proj.cost_total_usd < proj.cost_limit_usd` — the first condition is falsy when cost is 0.0, permanently locking zero-spend projects.

**Root cause:** Python truthiness gotcha. Common mistake when guarding against None vs 0.

### 5.4 N+1 Query Patterns (Found by: adversarial_review, 1 occurrence)

**Pattern:** `_pause_project_workflows` querying ALL agents globally then filtering in Python instead of using a JOIN.

**Root cause:** Developer wrote the simplest working code first without considering scale.

### 5.5 Missing Authentication on New Endpoints (Found by: security_review, 1 occurrence)

**Pattern:** New `/cost-entries` endpoint created without `verify_agent_authentication()` check.

**Root cause:** Existing endpoints all had auth, but the development prompt didn't explicitly call out "every new endpoint must have auth."

### 5.6 Nested Session Leaks (Found by: adversarial_review, 1 occurrence)

**Pattern:** `_get_agent_cwd` opening its own `get_db()` session inside a caller that already has one, leaking connections and reading inconsistent snapshots.

**Root cause:** Utility function written in isolation without considering its call context.

---

## 6. Prompt Improvement Proposals

### 6.1 Development Prompt — Transaction Boundary Guidance

**Before (current prompt excerpt):**
```
Implement all components according to the architecture.
Reads architecture.md from Phase 2, implements each component following
the task breakdown, writes tests, and creates working software.
```

**After (proposed):**
```
Implement all components according to the architecture.

CRITICAL PATTERN — TRANSACTION BOUNDARIES:
When modifying multiple tables in a single operation (e.g., cost derivation
that writes to cost_entries AND updates task/feature/project rollups), the
CALLER must control the transaction. Do NOT put db.commit() inside individual
derive/helper functions. The pattern is:
  1. Caller opens session via get_db()
  2. Caller passes session to all helper functions
  3. Caller calls db.commit() once at the end
  4. On any exception, the entire operation rolls back atomically

This follows the existing pattern in status_derivation.py — study it before implementing.
```

**Rationale:** Would have prevented 3 of 5 adversarial BLOCKERs (cascading commits, nested sessions, residual commit in _pause_project_workflows).

### 6.2 Development Prompt — Integration Wiring Checklist

**Before:** No explicit instruction about wiring new modules.

**After (proposed addition):**
```
INTEGRATION WIRING CHECKLIST (verify before marking done):
- [ ] Every new endpoint has authentication (verify_agent_authentication or equivalent)
- [ ] Every new module is called from the appropriate lifecycle hook (task completion, workflow start, etc.)
- [ ] Every new Pydantic model has input validation (ranges, enums, non-negative checks)
- [ ] Budget/cost guard functions are called BEFORE dispatching new work, not just after
```

**Rationale:** Would have prevented missing auth on /cost-entries, missing task_completion wiring, and missing budget guards.

### 6.3 Security Review Prompt — New Endpoint Checklist

**Before (current prompt):**
```
Perform focused security review and fix vulnerabilities found.
Analyzes the codebase for security vulnerabilities, authentication issues,
authorization bypasses, data handling problems, and FIXES critical security
issues before they ship.
```

**After (proposed):**
```
Perform focused security review and fix vulnerabilities found.

MANDATORY FIRST STEP — NEW ENDPOINT AUDIT:
Before running any automated scans, grep for new route definitions added in
this feature. For EACH new endpoint, verify:
  1. Authentication check exists (verify_agent_authentication or equivalent)
  2. Input validation via Pydantic model with appropriate constraints
  3. No raw SQL or string interpolation in queries
  4. Rate limiting considered (at minimum, document if omitted)
If any endpoint lacks auth, FIX IT — this is a CRITICAL finding.
```

**Rationale:** The security agent found the missing auth, but a more structured checklist would make this faster and more reliable.

### 6.4 Adversarial Review Prompt — Already Excellent

The adversarial review prompt is already well-calibrated. No changes proposed. It found real, non-trivial bugs that the developer and architectural reviewer both missed. The "assume the code is broken" framing is effective.

### 6.5 Product Requirements Prompt — Duplicate Agent Prevention

**Before:** No instruction about checking if work was already done.

**After (proposed addition):**
```
FIRST STEP: Check if requirements_analysis.md already exists in docs/.
If it does and appears complete (>500 lines, has FR-1 through FR-N),
read it and verify completeness rather than regenerating from scratch.
```

**Rationale:** Product requirements ran twice (agents 382dae99 and 8814f569). The second agent appears to be a retry. Checking for existing work first would save time.

---

## 7. Methodology Refinements

### 7.1 Review-Fix-Verify Cycle Efficiency

The architectural review took 3 runs and adversarial review took 3 runs. Each run found new issues because the fix cycle introduced new code. This is **expected and healthy** — it's the purpose of iterative review.

**Improvement:** Consider running adversarial review immediately after architectural review passes, before the developer marks done on the full feature. Currently, the developer fixes arch review findings, then adversarial review finds different bugs. Running both reviewers in parallel on the initial implementation would reduce total cycles from 6 to 3-4.

### 7.2 MCP Connection Resilience

Two agents (security_review, doc_review) lost MCP connection mid-task. Both recovered via curl fallback, but this is fragile. 

**Recommendation:** Add a "MCP health check" at the start of each phase, and if connection fails, automatically fall back to HTTP API calls. Or, implement automatic MCP reconnection in the agent harness.

### 7.3 Overnight Gap

The 7.5-hour gap between architectural review v1 (00:46) and development v2 (08:17) suggests the pipeline paused overnight. This is fine for cost optimization but means the total wall time is misleading. Active time was ~4 hours.

**Recommendation:** Track active vs. idle time separately in pipeline_metrics.json (which was not generated for this run).

### 7.4 pipeline_metrics.json Not Generated

The forensics phase expects `pipeline_metrics.json` but it was not created. This means timing data had to be reconstructed from git log timestamps and tmux file modification times.

**Recommendation:** The orchestrator should generate pipeline_metrics.json automatically when each phase completes, recording: phase name, agent ID, start time, end time, and verdict.

### 7.5 QA Result JSON Schema Mismatch

The QA agent initially wrote qa_result.json in a non-standard schema. The pipeline flagged this and the agent rewrote it in the documented shape. The prompt should include the expected JSON schema inline to prevent this.

---

## 8. Positive Patterns Worth Preserving

1. **Self-healing derivation pattern:** cost_derivation.py mirrors status_derivation.py exactly. The architecture agent correctly identified this pattern and the developer implemented it faithfully.

2. **Session-id keyed checkpoints:** SessionCostCheckpoint uses session_id (not Agent.id) to prevent double-counting on retries. This was identified in requirements and preserved through all phases.

3. **Paused-by generalization:** The nuanced rule (is_not_none everywhere EXCEPT start() keeps ==user) was correctly specified, implemented, and verified across all review cycles.

4. **Two-pass review design:** Architectural review catches integration gaps; adversarial review catches correctness bugs. They found non-overlapping issues. This is the right design.

5. **Security agent self-healing:** When MCP failed, the security agent fell back to curl and still completed its task. Good resilience pattern, though it should be formalized.

---

## 9. Actionable Findings Summary

| # | Finding | Severity | Proposed Fix | Phase |
|---|---------|----------|--------------|-------|
| 1 | Transaction boundary violations in derive functions | HIGH | Add explicit guidance to development prompt | development |
| 2 | Missing integration wiring (auth, task hooks, guards) | HIGH | Add integration checklist to development prompt | development |
| 3 | MCP connection loss in long-running agents | MEDIUM | Add MCP health check + auto-reconnect | infrastructure |
| 4 | pipeline_metrics.json not generated | MEDIUM | Orchestrator should auto-generate on phase completion | orchestrator |
| 5 | QA result JSON schema mismatch | LOW | Include expected schema in qa_validation.yaml prompt | qa_validation |
| 6 | Duplicate product_requirements agent | LOW | Check for existing work before regenerating | product_requirements |
| 7 | No rate limiting on new endpoints | LOW | Document in security review findings | security_review |

---

## 10. Pipeline Efficiency Summary

| Metric | Value |
|--------|-------|
| Total phases | 10 (of 12, excluding forensics and git_commit_push) |
| Total agent invocations | 21 |
| Average invocations per phase | 2.1 |
| Most iterated phase | development (6 runs) |
| Least iterated phase | scope_review, architecture_design, security_review, qa_validation, product_validation, doc_review (1 run each) |
| Review-fix-verify cycles | 6 (3 architectural + 3 adversarial) |
| Bugs found by review | 10 BLOCKERs + 4 FIXes (architectural) + 5 BLOCKERs (adversarial) + 3 vulnerabilities (security) |
| Active pipeline time | ~4 hours |
| Wall time (including overnight gap) | ~11.5 hours |
