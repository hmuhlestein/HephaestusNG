# Architectural Review: Backend OpenRouter Direct Cost Capture

**Feature ID:** des-91c8-openrouter-direct
**Reviewer:** Architecture agent (Phase 5), same session as Phase 3 design
**Date:** 2026-07-24
**Verdict:** PASS

---

## 1. What was reviewed

- `docs/architecture.md` — the two-task, verify-and-harden architecture written in
  Phase 3, against `docs/requirements_analysis.md` FR-4/NFR-1.
- The development-phase diff: `git diff HEAD~1` (commit `a07e6f5` on top of `ec54d47`),
  touching only `src/interfaces/langchain_llm_client.py` and `tests/test_cost_tracking.py`.
- Full test run: `python -m pytest tests/test_cost_tracking.py -q` → 47 passed, including
  the 4 new tests in `TestInvokeAndRecord`.

## 2. Architecture compliance

Both scoped tasks were implemented exactly as designed, with no additions beyond scope:

- **Task 1 (test coverage):** `TestInvokeAndRecord` in `tests/test_cost_tracking.py`
  covers all four cases specified in architecture.md §2 Task 1 — happy path (writes a
  `CostEntry` with correct `cost_usd`/`input_tokens`/`output_tokens`/`cache_read_tokens`/
  `model`/`task_id`), the non-OpenRouter no-cost path (`record_cost` not called), the
  missing-`response_metadata` defensive path, and the malformed-metadata path that drives
  the `except` branch. All assert the response object is still returned regardless of
  outcome (NFR-1), matching architecture.md's stated acceptance criteria verbatim.
- **Task 2 (log level):** `langchain_llm_client.py:393` changed `logger.debug` →
  `logger.warning` in the `except Exception` branch only. The `else` branch at line 391
  (expected no-cost case) is untouched and still logs at `debug`, exactly as designed —
  the two cases remain correctly distinguished.
- **Out-of-scope boundary held:** no changes to `CostEntry` schema, `record_cost()`,
  rollup logic, budget enforcement, or any other collector. `git diff --stat` confirms
  only the two files architecture.md named were touched.

## 3. Component boundaries / interface contracts

- `_invoke_and_record`'s signature and call sites are unchanged — no new parameters, no
  new callers added or removed. The 7 existing orchestrator call sites are untouched.
- Test fixture `llm_client` constructs a real `LangChainLLMClient` with `ChatOpenAI`
  patched out at `src.interfaces.langchain_llm_client.ChatOpenAI`, exercising the actual
  `_invoke_and_record` method rather than a reimplementation — correct approach, avoids
  testing a mock of the logic instead of the logic itself.
- Tests patch `record_cost` and `get_db` at their *call-site* module paths
  (`src.core.cost_derivation.record_cost`, `src.core.database.get_db`), which is correct
  given `_invoke_and_record` does local `from ... import ...` inside the function body —
  patching the source module (not a re-exported name) is the right target here.

## 4. Data flow

Verified unchanged and matches architecture.md §1.2: `ChatOpenAI.ainvoke()` →
`response.response_metadata["token_usage"]` → extraction → `record_cost()` → `CostEntry`
row → rollup. No new data flow introduced.

## 5. Design patterns / naming

Consistent with existing test file conventions (class-per-concern grouping, `Mock`/
`AsyncMock` for the model, `caplog` for log assertions). No naming or pattern
deviations found.

## 6. Findings

### BLOCKER
None.

### FIX
None.

### DEFER
1. **Stray whitespace change unrelated to the task.** `src/interfaces/langchain_llm_client.py:349-350` — a blank line was added after `from src.core.log_context import set_log_context` inside `_invoke_and_record`. This is not part of either scoped task (test coverage or log-level narrowing) and isn't called out in the commit message (`a07e6f5`: "Self-review complete, no changes needed"). Harmless formatting noise, not an architecture violation — flagging only because CLAUDE.md's "surgical changes" convention calls for changed lines to trace to the task; no fix required, informational only.

## 7. Test verification

```
python -m pytest tests/test_cost_tracking.py -q
47 passed, 200 warnings in 8.13s
```

All new and pre-existing tests pass. No regressions.

## 8. Conclusion

Implementation matches the Phase 3 architecture precisely: both scoped tasks completed,
acceptance criteria met, out-of-scope boundaries respected, tests pass. No blocking or
fixable architectural issues. One trivial, non-functional DEFER noted above.
