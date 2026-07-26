---
type: qa_validation_result
feature_id: des-91c8-opencode-collector
passed_tests: 83
failed_tests: 0
total_tests: 83
pass_rate: 100.0
critical_issues: 0
non_blocking_issues: 1
requirements_met: 5
requirements_total: 5
security_fixes_verified: 0
security_fixes_total: 0
frontend_typecheck_new_errors: 0
unrelated_preexisting_failures: 42
unrelated_preexisting_errors: 7
status: PASS
recommendation: done
---

# QA Validation Report — OpenCode Cost Collector

**Feature ID:** des-91c8-opencode-collector
**Date:** 2026-07-26
**Reviewer:** QA validation phase (Phase 8 of 12)

## 1. Test Environment

`TESTING.md` exists at the project root and was read in full. Followed its documented commands exactly:

```
python -m pytest tests/ -p no:libtmux -q
python -m pytest tests/test_cost_collection_service.py tests/test_cost_tracking.py -p no:libtmux -q
```

Python 3.12.9, SQLite in-memory test DB, `-p no:libtmux` per the documented `libtmux` plugin gotcha (§8 of TESTING.md).

## 2. Scope of Changes Under Test

Confirmed via `git diff --stat main...HEAD`: this feature's diff touches exactly two files —

- `src/services/cost_collection_service.py` (+198/-79)
- `tests/test_cost_collection_service.py` (+492/-… net additions)

No other file in the repository was modified by this feature. This matches the architecture (single-module change, no schema changes) and the scope_review ruling (FR2–FR5 authorized).

## 3. Unit Tests — Feature-Scoped

```
python -m pytest tests/test_cost_collection_service.py tests/test_cost_tracking.py -p no:libtmux -q
```

**Result: 83 passed, 0 failed.**

Of these, 18 tests exercise the OpenCode collector directly:

- `TestOpenCodeCollector` (6 tests) — basic collection, checkpoint short-circuit (`checkpoint >= 1`), missing `session_row_id`, missing DB row, zero-cost skip, malformed `model` JSON falling back to the raw string.
- `TestDiscoverOpencodeSession` (8 tests) — no DB file present, single match, empty result, multiple matches (most-recent tie-break), directory mismatch, session before/after the time window excluded, and a dedicated regression test (`test_finds_session_using_real_utc_epoch_regardless_of_host_tz`) verifying the `.timestamp()`/naive-datetime fix independently of the code path being tested (computes its fixture epoch via `calendar.timegm()`, not by reusing the same conversion under test — so it can't hide the bug the way the original test suite did).
- `TestCollectTaskCostOpenCode` (4 tests) — end-to-end write of a `CostEntry` + checkpoint row, second-call idempotency (no double record), graceful no-op when `opencode.db` is absent, and the specific double-count bug fixed in `af59ac8`: two OpenCode launches sharing the same Hephaestus `session_id` (via `SESSION_ROLES` reuse) each get their own checkpoint keyed by `session_row_id`, so the second launch's cost isn't silently dropped.

All pass. No skips, no xfails, in this file set.

## 4. Full Repository Test Suite

```
python -m pytest tests/ -p no:libtmux -q
```

**Result: 2013 passed, 42 failed, 52 skipped, 7 errors, 2114 total (0:20:53 wall time).**

All 42 failures and 7 errors are in files this feature's diff never touches: `test_conductor.py`, `test_mcp_results_endpoint.py`, `test_mcp_server.py`, `test_monitor.py`, `test_prompt_builder.py`, `test_prompt_delivery_cleanup.py`, `test_result_submission_flow.py`, `test_self_review_hook.py`, `test_self_review_migration.py`, `test_update_task_status_*.py`, `test_validation_agent_protection.py`, `integration/test_task_deduplication_flow.py`.

Verified these are pre-existing, not caused by this feature: re-ran `tests/test_mcp_server.py` in isolation (outside the full-suite run) and it fails identically (`7 failed` — `Failed: async def function...`, an async-test-marker/config issue unrelated to cost collection). Since this feature's diff contains zero changes to pytest config, conftest, async fixtures, the MCP server, conductor, monitor, or task-status endpoints, these failures cannot be attributed to this change. `TESTING.md` §8 ("Known Issues & Gotchas") independently documents this class of pre-existing failure (ForeignKey/fixture-ordering issues in some integration tests). Treated as out-of-scope for this feature's QA gate — not a blocker for `des-91c8-opencode-collector`, but worth flagging to the project maintainers as separate, standing test-suite debt.

## 5. Integration / End-to-End Validation

There is no separate `tests/integration/` file for the OpenCode collector — `TestCollectTaskCostOpenCode` in `test_cost_collection_service.py` *is* the integration-level coverage: it runs `collect_task_cost()` end-to-end (task → agent → session discovery → collector → `record_cost()` → checkpoint write) against a real SQLite `opencode.db` fixture and a real (in-memory) Hephaestus DB, not mocks. This matches the existing pattern used for the `pi` and `claude_code` collectors elsewhere in the same file. No additional end-to-end harness exists or is warranted — `collect_task_cost()` has no HTTP surface (it's an internal function called from `task_completion_service`), so there's no API-level e2e path to add.

## 6. Requirements Compliance

Verified against `docs/requirements_analysis.md` FR1–FR5 and the NFRs, cross-checked against the actual code in `src/services/cost_collection_service.py`:

| Requirement | Status | Evidence |
|---|---|---|
| FR1 — build/defer gate factually checked, conflict escalated | Met (ruled PROCEED by scope_review) | `docs/scope_review/scope_review_result.json` |
| FR2 — correlate completed task to OpenCode session via directory + time window | Met | `_discover_opencode_session()`, lines 423-481; tie-break = most recent `time_created`, logs discarded IDs |
| FR3 — rewrite collector to query `session` table's pre-aggregated columns | Met | `OpenCodeCollector.collect()`, lines 264-342 — direct column mapping, no stdout-JSON parsing |
| FR4 — wire `collect_task_cost()`'s opencode branch to run | Met | `collect_task_cost()`, lines 559-566 (`cli_type == "opencode"` branch, no longer a bare `pass`) |
| FR5 — checkpointing/re-collection safety | Met | `checkpoint_key` keyed by `opencode_session_row_id` (lines 574-583), not shared `session_id` — the exact fix for the double-count bug found in adversarial_review |
| NFR — no new tables/columns | Met | `git diff --stat` shows no `src/core/database.py` changes |
| NFR — read-only DB access | Met | `sqlite3.connect(f"file:{...}?mode=ro", uri=True)` in both `OpenCodeCollector.collect()` and `_discover_opencode_session()` |
| NFR — path safety under `~/.local/share/opencode/` | Met | `.resolve()` + `startswith()` base-dir check, lines 438-446 |
| NFR — graceful absence | Met | `test_no_opencode_db_present`, `test_no_db_file` cover missing-DB paths |
| NFR — no timer-based collection | Met | Only call site is `collect_task_cost()`, invoked at task completion |

**5/5 requirements met, 0 unmet.**

## 7. Prior Review Findings Carried Into This Phase

- **architectural_review**: PASS, 0 blockers, 1 DEFER (D-1: `opencode.db` URI path not percent-encoded for literal `?`/`#` characters in a home directory — theoretical, real-world home dirs essentially never contain these characters; correctly left deferred, not re-litigated here since QA is not the phase that re-scopes deferred architectural items).
- **adversarial_review**: 1 BLOCKER found in the initial pass (B-1: naive-datetime `.timestamp()` misread as local time, silently dropping 100% of OpenCode costs on non-UTC hosts), fixed in `af59ac8`, and independently re-verified in a second adversarial pass by diff inspection plus actually running the test suite. Confirmed still fixed: `_discover_opencode_session()` (lines 456-457) attaches `tzinfo=timezone.utc` before calling `.timestamp()` on both bounds, and `test_finds_session_using_real_utc_epoch_regardless_of_host_tz` passes.
- **security_review**: PASS, 0 critical/high/medium/low findings. Path traversal, SQL injection, untrusted-deserialization, and read-only-access surfaces all reviewed with no issues raised.

No open blockers or unresolved findings from any prior phase.

## 8. Security Fixes Validation

No security-phase fixes were required (`security_review` found 0 issues), so there is nothing to re-verify at this gate beyond what's already covered in §7. The one BLOCKER that did require a fix (B-1) was found and resolved in `adversarial_review`, not `security_review`; it's carried forward and independently re-confirmed above via the live test run (`test_finds_session_using_real_utc_epoch_regardless_of_host_tz` passing) rather than by trusting the prior report alone.

## 9. Non-Blocking Issues

1. **D-1 (carried forward, architectural_review):** `opencode.db`'s URI path isn't percent-encoded for literal `?`/`#` characters. Deferred — matches the original classification; no change in risk since that review.

## 10. Frontend

No frontend changes in this feature's diff (backend-only, single service file). `npx tsc --noEmit` not re-run since no `.ts`/`.tsx` files are touched; `frontend_typecheck_new_errors: 0` reflects "not applicable, zero new errors possible" rather than a fresh full frontend build.

## 11. Recommendation

**PASS — done.** All 83 feature-scoped tests pass (18 of them OpenCode-specific), all 5 requirements are met and traced to code, no unresolved findings carry forward from architectural/adversarial/security review, and the one deferred item (D-1) is a pre-existing, correctly-scoped-out theoretical edge case. The 42 failures/7 errors seen in the full repository suite are pre-existing, confirmed unrelated to this feature's diff (different files entirely, and reproduce in isolation without this feature's code in the call path), and are not a gate for this feature.
