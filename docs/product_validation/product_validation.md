# Product Validation Report: Backend OpenRouter Direct Cost Capture

**Feature ID:** des-91c8-openrouter-direct
**Validation Date:** 2026-07-24
**Design Document:** `.hephaestus/design.md` — §"Backend's own direct OpenRouter calls" (lines 234-253), §"Backend's own OpenRouter calls" (lines 579-620), §Data Model (254-311), Implementation Phase 5 (692-694)
**Requirements Document:** `docs/requirements_analysis.md`
**Architecture Document:** `docs/architecture.md`
**QA Report:** `docs/qa_validation/qa_report.md` (PASS, 102/102 tests)
**Security Report:** `security_report.md` (repo root — see §5, misplacement noted) (PASS, no critical/high)
**Verdict:** PASS

---

## 1. Executive Summary

This feature's own requirements_analysis.md (Phase 1) established an unusual but well-supported finding: the mechanism the design document describes — intercepting the orchestrator's own OpenRouter LLM calls, extracting token/cost usage, and writing it to the `CostEntry` ledger — was **already implemented** on this branch before this feature's pipeline began, having landed via earlier Cost Tracking Schema and Budget Enforcement development phases. That finding was independently re-verified at each subsequent gate (scope_review, architectural_review, adversarial_review, security_review, qa_validation) and is confirmed again here by direct inspection.

Given that, this feature's real scope narrowed to two things, both delivered:

1. **Closing a genuine test-coverage gap.** `_invoke_and_record()` (the extraction/write choke point at `src/interfaces/langchain_llm_client.py:323-395`) had zero test coverage of its own parsing logic before this feature. `tests/test_cost_tracking.py` now has a `TestInvokeAndRecord` class with 5 tests covering: a well-formed OpenRouter response, explicit-`null` metadata fields (a real bug this surfaced — see §2 FR-4), a non-OpenRouter response (no cost data), missing `response_metadata` entirely, and malformed metadata triggering the warning path.
2. **A defensive null-safety fix + log-level correction**, found via the coverage work in (1): `.get(key, {})` silently mishandles an explicit JSON `null` (as opposed to a missing key), raising `AttributeError` inside the `try` block and dropping a legitimate `CostEntry`. Changed to `.get(key) or {}` at all three call sites (`token_usage`, `cost`, `prompt_tokens_details`). The exception log level was also bumped from `debug` to `warning` so a future regression here is visible in normal operation, not just under debug logging.

All 48 tests in `tests/test_cost_tracking.py` pass (102 across the full cost/budget test surface). No new mypy errors. No critical or high security findings. Architectural and adversarial review both closed clean (one resolved BLOCKER from adversarial review's first run — the exact null-handling bug (1) fixes — verified fixed in the re-run).

**This is a legitimate, narrowly-scoped feature**, not a no-op: it converted an untested, latently-buggy extraction path into a tested, correctly-null-safe one. It does not, however, deliver any new user-facing capability beyond what already existed on `main` — the cost-capture behavior itself (writing `CostEntry` rows for orchestrator OpenRouter calls) predates this feature.

---

## 2. Functional Requirements Verification

Traced against `docs/requirements_analysis.md` §2.

| ID | Requirement | Verification | Status |
|---|---|---|---|
| FR-1 | `extra_body={"usage": {"include": True}}` on all `provider == "openrouter"` `ChatOpenAI` construction | `langchain_llm_client.py:243` — confirmed present, merged correctly alongside `provider`/`reasoning` extra_body keys (lines 224-245) | ✅ PASS (pre-existing) |
| FR-2 | Single choke-point helper wrapping `model.ainvoke()`, all orchestrator call sites routed through it | `_invoke_and_record` at lines 323-395; `grep -n ainvoke` confirms all 7 call sites (`classify_complexity`, `enrich_task`, `resolve_ticket_clarification`, `analyze_agent_state`, `analyze_agent_trajectory`, `analyze_system_coherence`, `review_qa_report`) route through it, zero direct `model.ainvoke()` calls elsewhere | ✅ PASS (pre-existing) |
| FR-3 | `task_id` threaded where known, `None` (overhead bucket) where not | Confirmed at each call site; `CostEntry.task_id` nullable | ✅ PASS (pre-existing) |
| FR-4 | Extract tokens/cost from `response_metadata` and write `CostEntry`, verified (not just assumed) correct | **This feature's actual deliverable.** `TestInvokeAndRecord` (5 tests, `tests/test_cost_tracking.py:801-944`) exercises the real extraction path with realistic mocked `response_metadata`; all pass. The `.get(key) or {}` null-safety fix (`langchain_llm_client.py:363,366,375-376`) closes a real bug the test-writing surfaced (explicit `null` in `prompt_tokens_details` previously raised and silently dropped a valid, cost-bearing `CostEntry`) | ✅ PASS (gap closed) |
| FR-5 | `raw_usage` retained for debugging | `langchain_llm_client.py:388` (`raw_usage=usage`) | ✅ PASS (pre-existing) |

**Live smoke-test item (requirements_analysis.md §0/§6, architecture.md §3):** confirming against a *real* OpenRouter API response that `usage.cost` survives LangChain's parsing into `response_metadata["token_usage"]["cost"]` was explicitly scoped out of automated testing (not CI-automatable, requires a live API key) and left as a manual verification note. This remains open — see §7 Recommendations. The mocked-response tests in FR-4 pin the *code's* handling of that shape; they cannot prove OpenRouter+LangChain actually produce that shape today.

---

## 3. Non-Functional Requirements

| ID | Requirement | Verification | Status |
|---|---|---|---|
| NFR-1 | Cost-extraction failure must never break the underlying LLM call | `test_missing_response_metadata_does_not_raise` and `test_malformed_metadata_logs_warning_and_still_returns_response` both assert `response is model.ainvoke.return_value` even when extraction fails | ✅ PASS |
| NFR-2 | No duplicate `CostEntry` rows per LLM turn | Single `record_cost` call per `_invoke_and_record` invocation, by construction; unchanged by this feature | ✅ PASS |
| NFR-3 | Non-OpenRouter providers unaffected (no spurious `CostEntry`) | `test_non_openrouter_response_writes_no_cost_entry` asserts `record_cost` not called when `cost` field absent | ✅ PASS |
| — | No new mypy errors | QA report: 60 pre-existing errors in unrelated code, identical on `main`; feature diff introduces zero new ones | ✅ PASS |
| — | No new attack surface | Security report: in-process defensive fix to metadata parsing wrapped in existing broad error handling; cost-ingestion HTTP path (auth, rate limiting, size caps) pre-existing and unaffected | ✅ PASS |

---

## 4. Integration Validation

- **Cost ingestion pipeline:** `_invoke_and_record` → `record_cost()` (`src/core/cost_derivation.py`) → `CostEntry` row → rollup to `Task`/`Feature`/`AutopilotDesign`/`AutopilotProject.cost_total_usd`. Traced end-to-end by both security_review and this validation; unchanged by this feature, correctly fed by the (now-fixed) extraction path.
- **Shared with other cost sources:** `record_cost()` and the rollup chain are shared with `pi`/`claude_code`/`opencode`/`codex` sources — confirmed this feature made no changes there (out of scope per requirements_analysis.md §1, respected).
- **Budget enforcement:** explicitly out of scope and untouched (`cost_limit_usd` checks, pause/resume) — confirmed no changes to `src/autopilot/orchestrator.py`'s budget-guard logic beyond the pre-existing, unrelated `cdb7d0d` gap noted in §5.
- **Test suite integration:** the new `TestInvokeAndRecord` class lives in the existing `tests/test_cost_tracking.py`, consistent with architecture.md's directive to keep coverage of this component together rather than fragmenting into a new file.

---

## 5. Known Gaps and Housekeeping (non-blocking)

1. **Branch is one commit behind `main`.** Missing `cdb7d0d` ("fix: correct two unsound self-heal heuristics from prior autopilot session"), which touches `src/autopilot/orchestrator.py`, `src/core/database.py`, `tests/test_orchestrator_helpers.py`, `tests/test_self_review_migration.py`, and `frontend/src/context/WebSocketContext.tsx` — all unrelated to this feature's scope, but the branch should be rebased onto `main` before merge (flagged first by security_review, confirmed here) so that fix isn't inadvertently reverted by the merge.
2. **`security_report.md` was written to the repo root**, not `docs/security_review/security_report.md` (the location every other review phase used, and where `docs/security_review/security_review_capped_notice.md` already lives). Content is sound (PASS verdict, reviewed in §1/§3 above) but the misplacement means the standard `docs/security_review/` directory still holds a stale report from an unrelated prior feature (Budget Enforcement, dated 2026-07-22). Flagging for doc_review (Phase 10) to reconcile — not a product-validation blocker since the actual review content is correct and was located and verified for this report.
3. **Stale artifacts from prior features found and overwritten during this pipeline run:** `docs/requirements_analysis.md`, `docs/product_validation/product_validation.md` (this file), and (per its own QA report) `docs/qa_validation/qa_report.md` all previously contained leftover content from the unrelated "Budget Enforcement and Pipeline Throttling" feature that ran earlier in this worktree. This is a worktree-reuse artifact of the pipeline tooling, not a defect in this feature's implementation.
4. **Live OpenRouter smoke test remains unautomated** (see §2 FR-4 note) — a deliberate, documented scope decision (architecture.md §3), not an oversight, but worth a manual check before/soon after merge given it's the one part of this mechanism that has never been confirmed against a real API response.

None of these block this feature's validation; they are recommendations for the phases that follow (doc_review, git_commit_push).

---

## 6. Edge Cases Confirmed Handled

- Explicit `null` (not missing key) in `token_usage`, `cost`, or `prompt_tokens_details` — fixed and tested (§2 FR-4).
- `response_metadata` attribute entirely absent from the response object — tested, no-op, no exception.
- `response_metadata` present but not a dict (`"not-a-dict"`) — tested, hits the `except` branch, logs at `warning`, still returns the response.
- Non-OpenRouter provider response (`cost` field absent) — tested, correctly writes nothing.
- Cost present but `task_id` absent (overhead-bucket calls like `classify_complexity`, `analyze_system_coherence`) — pre-existing behavior, `CostEntry.task_id` nullable, unaffected by this feature's changes.

---

## 7. Recommendations for Human Reviewer

1. **Approve for merge**, contingent on a rebase onto `main` (§5 item 1) to pick up `cdb7d0d` before the git_commit_push phase runs — this is routine housekeeping, not a rework request.
2. **Schedule a manual live-API confirmation** of the FR-4 smoke-test item at convenience: one real OpenRouter call through the orchestrator's existing call sites with `usage.include=true`, confirming a `CostEntry` with `source="openrouter_direct"` and non-zero `cost_usd` actually lands. This is low-urgency (the mocked tests pin correct code behavior for the documented response shape) but is the one part of this mechanism never verified against a live response.
3. **No code changes requested.** The feature closes the exact gap its own requirements analysis identified, with real test coverage and a real bug fix discovered along the way (the `null`-handling `AttributeError`). Scope discipline was maintained throughout — budget enforcement, other collectors, and UI were correctly left untouched.
4. Direct doc_review (Phase 10) to reconcile the misplaced `security_report.md` location (§5 item 2) if the pipeline's documentation conventions matter for this repo going forward.
