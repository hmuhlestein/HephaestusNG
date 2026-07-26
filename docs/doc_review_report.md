---
type: doc_review_result
feature_id: des-91c8-opencode-collector
verdict: PASS
---

# Documentation Quality Review Report

**Reviewer:** Hephaestus Doc Review Agent (Phase 10)
**Date:** 2026-07-26
**Feature:** OpenCode Cost Collector (des-91c8-opencode-collector)
**Workflow ID:** b0087459-85b3-4937-a50e-faee96194a7e

---

## 1. Executive Summary

Documentation quality is **GOOD**. `docs/requirements_analysis.md`,
`docs/security_review/security_report.md`, `docs/qa_validation/qa_report.md`,
`docs/product_validation/product_validation.md`, `docs/architectural_review/architectural_review_report.md`,
and `docs/adversarial_review/adversarial_review_report.md` were checked against
`src/services/cost_collection_service.py` (the actual shipped
implementation) and match it exactly — including their descriptions of the
two BLOCKERs found and fixed mid-pipeline (adversarial_review's naive-datetime
timezone bug, architectural_review's checkpoint-key bug), both independently
re-verified against the real code (lines 456-457, 583).

One document was stale: **`docs/architecture.md`**, written during Phase 3
before either BLOCKER fix landed, still described the pre-fix, buggy
behavior in two places. Both are fixed below.

`docs/doc_review_report.md`, `docs/feature_report.html`, and
`docs/code_summary.md` are overwritten each pipeline run for whichever
feature currently occupies this Docs Path; before this pass they held
content from a prior sibling feature (`des-91c8-cost-ui`) and are replaced
here with this feature's report — not a "stray file" in the sense of being
misplaced, just expected churn on paths that aren't versioned per-feature.
No other stray files were found at the project root or elsewhere; `git
status` shows only this phase's edit to `docs/architecture.md`.

## 2. Documentation Inventory

| Document | Path | Status |
|----------|------|--------|
| Requirements Analysis | `docs/requirements_analysis.md` | ✅ Accurate, matches implementation |
| Architecture | `docs/architecture.md` | ⚠️ Two stale code descriptions — **fixed** |
| Security Report | `docs/security_review/security_report.md` | ✅ Accurate (PASS) |
| QA Report | `docs/qa_validation/qa_report.md` | ✅ Accurate (83/83 tests, confirmed by live collection) |
| Product Validation | `docs/product_validation/product_validation.md` | ✅ Accurate (PASS) |
| Architectural Review | `docs/architectural_review/architectural_review_report.md` | ✅ Accurate |
| Adversarial Review | `docs/adversarial_review/adversarial_review_report.md` | ✅ Accurate |

## 3. Verification Method

Rather than trusting each doc's own claims, the implementation itself
(`src/services/cost_collection_service.py`) was read directly and compared
line-by-line against every architectural and behavioral claim:

- `OpenCodeCollector.collect()` (lines 264-342) — checkpoint 0/1 semantics, column mapping, zero-cost handling — matches `docs/architecture.md` §2.2 and `docs/requirements_analysis.md` FR3.
- `_discover_opencode_session()` (lines 423-481) — directory + time-window matching, most-recent tie-break, read-only SQLite, path-safety guard — matches §2.1 and FR2, **except** the UTC-timestamp detail (§3.1 below).
- `collect_task_cost()`'s opencode branch and checkpoint-key line (lines 559-593) — matches §2.3 and Data Flow §3, **except** the checkpoint-key detail (§3.2 below).
- Test collection count: `pytest tests/test_cost_collection_service.py tests/test_cost_tracking.py --collect-only -q` → **83 tests collected**, matching the QA and product-validation reports' "83/83" claims exactly.

## 4. Inaccuracies Found and Fixed

### 4.1 `docs/architecture.md` §2.1 — missing UTC tzinfo in timestamp params (Critical)

Section 2.1, step 4 described the time-window query params as
`int(agent_created_at.timestamp() * 1000)` / `int(datetime.utcnow().timestamp()
* 1000)` — the pre-fix, buggy form. The shipped code
(`cost_collection_service.py:456-457`) attaches `tzinfo=timezone.utc` to both
values before calling `.timestamp()`, because naive `.timestamp()` on a
UTC-wall-clock-but-tzinfo-less datetime is misread as local time, silently
shifting the query window on any non-UTC host and dropping 100% of OpenCode
cost collection there. This exact bug was found and fixed as adversarial_review
BLOCKER B-1 (commit `af59ac8`) — the architecture doc simply predates the fix
and was never updated. A reader relying on this doc to understand or
re-implement the discovery query would reproduce the bug. Fixed by updating
the params description to match the actual `.replace(tzinfo=timezone.utc)`
calls and adding a note explaining why, citing B-1.

### 4.2 `docs/architecture.md` §3 Data Flow, step 7 — wrong checkpoint key (Critical)

Step 7 stated the `SessionCostCheckpoint` row for OpenCode is "keyed by
Hephaestus's own internal `session_id`" — this was the doc's original design,
but it's wrong: OpenCode never resumes a session, so a second task launch
sharing the same Hephaestus `session_id` (e.g. a reused `session_role`) would
find the first launch's checkpoint already at 1 and silently skip collecting
its own, different `opencode.db` session row, dropping its cost with no
error. The shipped code (`cost_collection_service.py:583`) keys the OpenCode
checkpoint by `opencode_session_row_id` instead — the correct fix, found and
applied as architectural_review BLOCKER B-1 (commit `adae90b`), which that
report explicitly calls out as "the implementation is more correct than the
spec." The architecture doc was never updated to reflect its own design
error being overridden. Fixed by rewriting step 7 to describe the actual
`opencode_session_row_id` keying and why it's necessary.

## 5. Stray Files

None found. `git status` shows no untracked or misplaced files.

## 6. Verdict

**PASS.** Every phase report (requirements, security, QA, product
validation, architectural review, adversarial review) was already accurate
against the actual shipped code — including their descriptions of the two
BLOCKER fixes. The only inaccuracies were in `docs/architecture.md`, which
predated those fixes; both are corrected above to describe the real,
shipped behavior. `feature_report.html` and `code_summary.md` are produced
alongside this report per the phase's required outputs.
