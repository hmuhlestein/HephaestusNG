# Backend OpenRouter Direct Cost Capture — Technical Architecture

**Feature ID:** des-91c8-openrouter-direct
**Version:** 1.0
**Date:** 2026-07-24
**Author:** Architecture Design Agent (Phase 3)
**Status:** Implementation-Ready
**Branch:** `feature/des-91c8/openrouter-direct`

---

## 1. Architecture Overview

### 1.1 Finding carried forward from Phase 1 (requirements_analysis.md §0)

The mechanism this feature describes — intercepting the orchestrator's own OpenRouter
calls, extracting token/cost usage, and writing it to the `CostEntry` ledger — is
**already implemented** on this branch, in `src/interfaces/langchain_llm_client.py`. It
landed via earlier Cost Tracking Schema / Budget Enforcement features, not a dedicated
build here. Verified present: `usage.include=true` opt-in (line 243), the `_invoke_and_record`
choke point (lines 323-395), all 7 orchestrator call sites routed through it, `task_id`
threading, the `CostEntry` write with `source="openrouter_direct"`, and rollup to
`Task`/`Feature`/`AutopilotDesign`/`AutopilotProject`.

This architecture therefore does **not** design new components, tables, or data flow —
there is nothing net-new to build. It scopes the two genuinely open items from
requirements_analysis.md FR-4 and the NFR-1 follow-up: closing a test-coverage gap and
narrowing an over-broad exception handler. No schema changes, no new API endpoints, no
new files beyond one test file.

### 1.2 Why no new component design is needed

- Data model: `CostEntry` (src/core/database.py:1230) is unchanged — out of scope per
  requirements_analysis.md §1 ("Explicitly out of scope: budget enforcement guards").
- Integration points: `_invoke_and_record` is the single existing choke point; every
  orchestrator LLM call already routes through it. No new call sites to wire up.
- Data flow: `ChatOpenAI.ainvoke()` → `response.response_metadata["token_usage"]` →
  `_invoke_and_record` extraction → `record_cost()` → `CostEntry` row → rollup. Unchanged.
- Infrastructure: no new services, queues, or config. Uses the existing DB session
  (`get_db()`) and existing `record_cost()` path shared with `pi`/`claude_code`/
  `opencode`/`codex` sources.

---

## 2. Scope of Work for This Feature

Two tasks, both confined to `src/interfaces/langchain_llm_client.py` and one test file.
Both trace directly to requirements_analysis.md FR-4 and its NFR-1 follow-up — nothing
beyond that is in scope.

### Task 1 — Add test coverage for `_invoke_and_record`'s extraction logic

**File:** `tests/test_cost_tracking.py` (add to the existing file — it already covers
`CostEntry`/`record_cost`/rollup with `source="openrouter_direct"`; keep coverage of
this component together rather than starting a new file).

**What to test**, using a stub/mock model whose `.ainvoke()` returns an object with a
`response_metadata` dict shaped like a real OpenRouter response:

```python
{
    "token_usage": {
        "prompt_tokens": 120,
        "completion_tokens": 45,
        "prompt_tokens_details": {"cached_tokens": 30},
        "cost": {"total": 0.0034},
    },
    "model_name": "anthropic/claude-sonnet-4",
}
```

Acceptance criteria (traces to requirements_analysis.md FR-4):
- Calling `_invoke_and_record(model, messages, component="task_enrichment", task_id="t1")`
  with the shape above writes exactly one `CostEntry` with `source="openrouter_direct"`,
  `cost_usd=0.0034`, `input_tokens=120`, `output_tokens=45`, `cache_read_tokens=30`,
  `model="anthropic/claude-sonnet-4"`, `task_id="t1"`.
- A response with `token_usage` present but `cost.total` absent or `0` (the shape for
  every non-OpenRouter provider, per NFR-3) writes **no** `CostEntry` — assert
  `record_cost` is not called / no new row exists.
- A response with `response_metadata` missing entirely (defensive case) does not raise —
  `_invoke_and_record` still returns the response.
- The response object itself is always returned unchanged to the caller, regardless of
  whether cost extraction succeeded, failed, or found nothing to record (NFR-1).

Do not test `record_cost()`'s rollup behavior here — that's already covered elsewhere in
`test_cost_tracking.py` and is out of scope (requirements_analysis.md §1).

### Task 2 — Narrow the exception handler in `_invoke_and_record`

**File:** `src/interfaces/langchain_llm_client.py:392-393`

Current code:
```python
except Exception as e:
    logger.debug(f"Cost recording failed for {component}: {e}")
```

This swallows two different situations at the same (nearly invisible) log level:
"no cost data present" (expected, happens on every non-OpenRouter call — should stay
silent) and "cost data was present but extraction/write raised" (a real bug — e.g. a
LangChain metadata shape change breaking the `usage.get("cost", {})` chain, or a DB
write failure in `record_cost`). Per requirements_analysis.md §3 NFR-1, a bug here must
never break the underlying LLM call, so the response must still always be returned — but
it also shouldn't be silent.

**Design:** keep the outer `try/except Exception` as the safety net around the whole
extraction+write block (NFR-1 requires the LLM call site never breaks), but distinguish
the two cases the code already branches on:

- The `else` branch at line 391 (`cost_usd` is `0`/absent) is the expected, silent case
  for non-OpenRouter providers — leave it at `debug`, no change.
- The `except Exception` at line 392 means something raised while `token_usage`/`cost`
  data was being parsed or written — this is unexpected and should log at `warning`, not
  `debug`, so it's visible in normal logs rather than only with debug logging enabled.
  No new exception type, no re-raise — the goal is visibility, not new control flow.

```python
except Exception as e:
    logger.warning(f"Cost recording failed for {component}: {e}")
```

Acceptance criteria:
- Non-OpenRouter responses (no `cost` field) still log at `debug` via the existing
  `else` branch — unchanged, still silent by default.
- An exception raised during extraction/write now logs at `warning`.
- The `model.ainvoke()` result is still returned in both cases — behavior of the
  surrounding function is otherwise unchanged.
- Covered by the "missing `response_metadata`" test case in Task 1 (drive it through a
  path that raises inside the `try` to confirm the `warning` log fires and the response
  still returns).

---

## 3. Explicitly Out of Scope (unchanged from requirements_analysis.md §1)

- Budget enforcement guards (separate, already-delivered feature)
- Claude Code / OpenCode / Codex collector implementations
- Pi extension changes
- UI budget configuration
- Any change to `CostEntry` schema, `record_cost()`, or rollup derivation
- Live confirmatory call against the real OpenRouter API (requirements_analysis.md §0
  item 1) — the mocked-response test in Task 1 covers the extraction logic; an actual
  live smoke test against OpenRouter is not automatable in CI and is left as a manual
  verification note, not a build task

---

## 4. Task Breakdown for Development Phase

| # | Task | Depends on | Files touched |
|---|---|---|---|
| 1 | Add extraction-logic tests for `_invoke_and_record` (happy path, no-cost path, missing-metadata path) | none | `tests/test_cost_tracking.py` |
| 2 | Narrow `except Exception` log level from `debug` to `warning` in `_invoke_and_record` | Task 1 (test should exercise this path so the change is verified, not just asserted by inspection) | `src/interfaces/langchain_llm_client.py` |

Both tasks are small enough for a single development-phase pass; there is no blocking
relationship requiring separate agents or sequencing beyond "write the test that proves
the log-level change, then make the change."

---

## 5. Risks / Notes for Later Phases

- The extraction path's fragility (LangChain's `response_metadata` shape is an implicit,
  version-dependent contract — requirements_analysis.md §5) is not something this
  feature can eliminate; the test in Task 1 pins current behavior so a future LangChain
  upgrade that breaks the shape will fail a test instead of silently degrading to
  `cost_usd=0`.
- No live OpenRouter smoke test exists or is being added — if `qa_validation` or
  `product_validation` wants confirmation that real API responses actually carry
  `usage.cost`, that requires a manual check against a live API key, not something this
  architecture builds as an automated test.
