# QA Validation Report: Backend OpenRouter Direct Cost Capture

**Feature ID:** des-91c8-openrouter-direct
**Date:** 2026-07-24
**Phase:** qa_validation (Phase 8 of 12)
**Status:** PASS

> Note: `docs/qa_validation/qa_report.md` and `qa_result.json` previously contained a stale report for an unrelated earlier feature ("Budget Enforcement and Pipeline Throttling", dated 2026-07-21). Both are overwritten by this report.

## 1. TESTING.md

Exists at repo root. Read in full. Its guidance (`-p no:libtmux` required to avoid the libtmux plugin's `Marks cannot be applied to fixtures` error) was followed exactly for all commands below.

## 2. Scope of this feature (per requirements_analysis.md §0)

Requirements analysis found the core mechanism (OpenRouter `usage.include=true`, the `_invoke_and_record` choke point, `CostEntry` writes) already existed on this branch before this feature's development phase. The actual delta delivered by `development` is small and defensive:

- `src/interfaces/langchain_llm_client.py` — `_invoke_and_record()`: `.get(key, {})` → `.get(key) or {}` for `token_usage`, `cost`, and `prompt_tokens_details`, so an explicit JSON `null` (not just a missing key) doesn't raise `AttributeError`. Also bumped the parse-failure log level from `debug` to `warning`.
- `tests/test_cost_tracking.py` — 149 new lines: a `TestInvokeAndRecord` class (5 tests) covering the extraction path directly, closing the FR-4 test-coverage gap the requirements analysis flagged.

This QA pass validates that delta and regression-checks the cost-tracking/budget-enforcement subsystem it plugs into.

## 3. Test commands executed and results

```
python -m pytest tests/test_cost_tracking.py tests/test_budget_enforcement.py \
  tests/test_budget_enforcement_integration.py tests/test_cost_collection_service.py \
  -p no:libtmux -q
```
**Result: 102 passed, 0 failed** (510 warnings, all pre-existing deprecations — FastAPI `on_event`, Pydantic v1-style `@validator`, `datetime.utcnow()` — none introduced by this feature).

Targeted subset directly exercising this feature's diff:
```
python -m pytest "tests/test_cost_tracking.py::TestInvokeAndRecord" -p no:libtmux -v
```
```
test_openrouter_response_writes_cost_entry                        PASSED
test_null_prompt_tokens_details_still_writes_cost_entry            PASSED
test_non_openrouter_response_writes_no_cost_entry                  PASSED
test_missing_response_metadata_does_not_raise                      PASSED
test_malformed_metadata_logs_warning_and_still_returns_response    PASSED
```

These 5 tests close requirements_analysis.md's FR-4 gap ("no test coverage of `_invoke_and_record`'s extraction logic... a regression would silently degrade to `cost_usd=0` and pass every existing test"). With a realistically-shaped mocked `response_metadata`, they assert:
- a well-formed OpenRouter response writes a `CostEntry` with `source="openrouter_direct"`, non-zero `cost_usd`, populated token counts;
- an explicit `null` in `prompt_tokens_details` doesn't crash (the exact bug the `.get() or {}` fix addresses) and still writes a valid entry;
- a non-OpenRouter response (no `cost` field) writes nothing, silently and correctly (NFR-3);
- a response with no `response_metadata` at all is a no-op, not an exception (NFR-1);
- malformed metadata (`response_metadata` not a dict) logs at `warning` (not swallowed at `debug`) and still returns the LLM response.

## 4. Log locations

Test run output captured inline above (stdout). TESTING.md doesn't specify a separate log directory for pytest runs, and none of the executed suites write logs to disk.

## 5. Type checking

`mypy src/interfaces/langchain_llm_client.py`: 60 pre-existing errors (all in unrelated code — model-assignment lookup helpers around lines 708-864 with `ModelAssignment | None` unchecked). Verified by checking out `main`'s version of the same file and re-running mypy: identical 60 errors, same file, same lines. **This feature's diff introduces zero new mypy errors.**

`flake8` is not installed in this environment — skipped. The diff is 10 lines; hand-reviewed for style consistency with surrounding code, and mypy + the full pytest run cover correctness.

## 6. Integration / end-to-end validation

`test_cost_collection_service.py` and `test_budget_enforcement_integration.py` exercise the downstream consumers of the `CostEntry` ledger this feature writes into (rollup to Task/Workflow/Feature/Design/Project, budget pause/resume, agent termination on budget breach) — all pass, confirming the null-safety fix doesn't regress the pipeline this feature feeds. No live OpenRouter API call was made (would require a real API key and network access, out of scope for this environment); the mocked-response tests in `TestInvokeAndRecord` are the closest available proxy and match the `response_metadata` shape design.md documents for OpenRouter's `usage.cost` field.

## 7. Requirements compliance

Cross-checked against `docs/requirements_analysis.md`:

| Requirement | Status |
|---|---|
| FR-1 (usage.include on OpenRouter requests) | Unchanged by this feature, already satisfied |
| FR-2 (single choke point) | Unchanged by this feature, already satisfied |
| FR-3 (task_id attribution) | Unchanged by this feature, already satisfied |
| FR-4 (extract tokens/cost, write CostEntry) | **Now test-covered** — gap closed by the 5 new `TestInvokeAndRecord` tests |
| FR-5 (raw usage retained) | Unchanged, already satisfied |
| NFR-1 (extraction failure never breaks LLM call) | Verified by `test_missing_response_metadata_does_not_raise`, `test_malformed_metadata_logs_warning_and_still_returns_response` |
| NFR-2 (no double-instrumentation) | Unchanged, single `record_cost()` call per invocation |
| NFR-3 (non-OpenRouter providers unaffected) | Verified by `test_non_openrouter_response_writes_no_cost_entry` |

Requirements analysis's recommendation ("add unit test coverage for `_invoke_and_record`'s extraction logic using a realistic mocked `response_metadata`") was fully implemented by the development phase.

## 8. Security fixes validated

Per the security_review phase output (`security_report.md` at worktree root, commit `910c244`): 0 critical/high/medium findings, 1 low finding deferred to a ticket (`ticket-c07312d3-3243-4650-bf52-e5773c7ce738`), out of this feature's scope, not currently exploitable since `raw_usage` originates from OpenRouter itself, not attacker-controlled input. No fixes were required in security_review, so there is nothing to re-validate beyond confirming that rationale still holds — re-read `_invoke_and_record` (`langchain_llm_client.py:360-390`) and confirmed `raw_usage=usage` is sourced directly from OpenRouter's response, unchanged.

**File-location note:** `docs/security_review/security_report.md` and `docs/security_report.md` in this worktree are stale leftovers from earlier, unrelated features (Budget Enforcement / Cost Derivation Engine security reviews, dated 2026-07-22 and 2025-07-21). This feature's actual security_review output is `security_report.md` at the worktree root (added by commit `910c244`, misplaced outside `docs/`). Flagging so a later phase (e.g. doc_review) doesn't treat the stale `docs/` copies as current.

## 9. Branch state check

The root `security_report.md` noted this branch was "one commit behind main" when security_review ran. Re-verified at QA time: `git log --oneline HEAD..main` returns nothing — the branch already contains `cdb7d0d` (the commit security_review was concerned about missing) in its ancestry. No rebase needed; that concern is resolved.

## 10. Aggregate results

| Metric | Value |
|---|---|
| Tests run (cost-tracking + budget-enforcement + cost-collection suites) | 102 |
| Passed | 102 |
| Failed | 0 |
| New tests for this feature's diff | 5/5 passed |
| New mypy errors introduced | 0 |
| Requirements met | 8/8 (FR-1–5, NFR-1–3) |
| Security findings requiring fix | 0 |

## 11. Iteration recommendation

**done** — no blocking issues. This phase's own findings (stale doc-location duplicates, no live-API smoke test) are informational, not blockers: the stale `docs/security_report.md` / `docs/security_review/security_report.md` predate this feature and were never touched by it; a live OpenRouter smoke test was flagged by requirements_analysis.md as a nice-to-have, and the mocked-response test suite added this phase is a standard, adequate substitute — network-dependent tests don't belong in an automated pytest suite regardless.

## 12. Deliverables

- `docs/qa_validation/qa_report.md` — this report
- `docs/qa_validation/qa_result.json` — structured pass/fail counts for the pipeline gate
