# Adversarial Review — Backend OpenRouter Direct Cost Capture

**Scope reviewed:** `src/interfaces/langchain_llm_client.py::_invoke_and_record` (the
only production code touched this feature — a one-line log-level change), plus its
new tests in `tests/test_cost_tracking.py`, and the shared write path it calls into
(`src/core/cost_derivation.py::record_cost`, `src/core/database.py::get_db`) since a
correctness review of the extraction logic requires tracing what it feeds into.

All 47 tests in `tests/test_cost_tracking.py` pass, including the 4 new ones.

---

## BLOCKER

### B1 — `prompt_tokens_details: null` silently drops a legitimate, cost-bearing CostEntry

**File:** `src/interfaces/langchain_llm_client.py:369-390`

**Failure sequence:**
1. OpenRouter returns a response where `usage.cost.total` is a real, positive
   dollar amount (e.g. `0.01`) but `usage.prompt_tokens_details` is JSON `null`
   rather than omitted — the normal encoding a JSON API uses for "not applicable"
   (e.g. any model/provider combination that doesn't report cache statistics).
2. `cost_data.get("total", 0)` → `0.01`. The `if cost_usd > 0:` branch is entered —
   this is a real OpenRouter call that should produce a `CostEntry`.
3. `usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)` — `dict.get`
   only substitutes the default when the *key is missing*; here the key is present
   with value `None`, so this evaluates to `None.get("cached_tokens", 0)` and raises
   `AttributeError`.
4. The exception fires *after* `cost_usd > 0` was already established but *before*
   `record_cost()` is called. It's caught by the outer `except Exception` and now
   logged at `warning` — but the entire `CostEntry` for this call is dropped. Actual
   spend that should have been captured (this feature's entire purpose) silently
   disappears from the ledger, downgraded from "silent" (bad) to merely "logged and
   still lost" (still bad).

Verified interactively:
```python
usage = {"prompt_tokens": 100, "completion_tokens": 20,
         "prompt_tokens_details": None, "cost": {"total": 0.01}}
cost_data = usage.get("cost", {})
cost_usd = cost_data.get("total", 0)          # 0.01 -- a real cost
usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
# AttributeError: 'NoneType' object has no attribute 'get'
```

**Fix:** guard each chained `.get()` against an explicit `None`, not just a missing
key, before the `if cost_usd > 0` block does anything cost-bearing:

```python
token_details = usage.get("prompt_tokens_details") or {}
cache_read = token_details.get("cached_tokens", 0)
```

Apply the same `or {}` pattern to `cost_data = usage.get("cost") or {}` and
`usage = metadata.get("token_usage") or {}` — any of the three can legally be `null`
in a JSON payload, and only the first one currently fails safe (`usage.get("cost",
{})` on a dict is fine; it's the second-level lookup on a `None` that breaks).
The existing test `test_missing_response_metadata_does_not_raise` covers a wholesale
*missing* key — it does not cover a *present-but-null* nested field, which is the
actual shape that triggers this. Add a regression test with
`"prompt_tokens_details": None` and assert a `CostEntry` **is** still written.

---

## WARNING

### W1 — the "expected silent" and "unexpected error" paths are not actually distinguishable by exception type

**File:** `src/interfaces/langchain_llm_client.py:361-394`

The architecture doc (Task 2) intends a clean split: no-cost-data is silent
(`debug`), broken-extraction is loud (`warning`). But the split is implemented purely
by "did an exception happen to get raised," not by inspecting the data shape. Per B1,
a `None` anywhere in the `usage`/`cost`/`prompt_tokens_details` chain converts what
the design calls "expected, silent, happens on every non-OpenRouter call" into the
`except` branch instead of the intended `else` branch. Concretely: if OpenRouter (or
any provider routed through this same code path) ever serializes its "no cost data
yet" state as `"cost": null` instead of omitting the key, every one of those calls
will now log a `WARNING` in normal operation — the opposite of what Task 2 was trying
to achieve (visibility for real bugs, silence for the routine case). This doesn't
crash anything (NFR-1 still holds — the response is still returned), but it
undermines the log-level change's actual goal: warnings stop being a meaningful
signal once a normal, expected condition can trigger them.

**Recommendation:** fix B1 first (guard the `None` cases so cost-bearing responses
never hit the `except`), then re-derive whether the "no cost" `else`-branch condition
(`cost_usd > 0` check) can also be reached safely when `cost_data` itself is `None` —
it can, once `cost_data = usage.get("cost") or {}` is in place, since `{}.get("total",
0)` correctly returns `0` and falls through to the silent `else`.

### W2 — pre-existing silent cost-cap (`cost_derivation.record_cost`) is on this feature's only write path

**File:** `src/core/cost_derivation.py:80-82`

Not part of this feature's diff (unchanged, out of scope per architecture.md §3), but
`_invoke_and_record` is the only production call site exercising it for the
`openrouter_direct` source, so it's worth flagging: any cost above $1000 for a single
call is silently rewritten to exactly $1000 before being persisted (a `logger.warning`
fires, but the ledger row itself now records a number that never actually happened).
For a single LLM call this is very unlikely to trigger, but if it ever does (e.g. a
runaway agentic loop with a huge context window on a premium model), the resulting
`CostEntry.cost_usd` is wrong data being fed into the exact rollups (`Task` /
`Feature` / `AutopilotDesign` / `AutopilotProject` `cost_total_usd`) this feature
exists to keep trustworthy. Flagging for awareness, not requesting a change in this
PR since it predates it and is explicitly out of scope.

---

## NIT

### N1 — `component` fallback for `model` is misleading, not a model name

**File:** `src/interfaces/langchain_llm_client.py:385`

`model=metadata.get("model_name", component)` — if `model_name` is absent from the
response, the `CostEntry.model` column gets the *component* string (e.g.
`"task_enrichment"`) instead of a model identifier. This can't happen for a genuine
OpenRouter `usage.include=true` response (which always includes `model_name`), so
it's dead-in-practice, but if it ever does trigger it silently mislabels the ledger
row's `model` column with an unrelated value rather than `None`/`"unknown"`. Low
priority given the precondition is already gated by `cost_usd > 0`, which in practice
only OpenRouter satisfies.

### N2 — provider dispatch in `_create_model_for_provider` is a conditional chain, not addressed by this feature

Not touched by this PR (architecture.md explicitly scopes this feature to
`_invoke_and_record` only), but noted for the composition-review criterion: the
`if/elif` chain across `openai`/`groq`/`openrouter`/`azure_openai`/`google_ai` in the
model-factory method mixes provider-specific low-level details (header construction,
`extra_body` shaping, deployment-name semantics) into one large method rather than a
per-provider strategy object. No action requested here — out of scope for this
feature.

---

## Summary

The one-line change actually shipped in this PR (log level `debug` -> `warning`) does
exactly what it says and is correctly tested. The adversarial finding that matters is
upstream of that line: the extraction chain it guards has a real, reachable failure
mode (B1) where a legitimate, cost-bearing OpenRouter response can lose its
`CostEntry` entirely because of a `None` value at a level `dict.get(key, default)`
doesn't protect against. This should be fixed before this feature is considered done,
since it directly undermines FR-4 ("every OpenRouter call must have a corresponding
`CostEntry`" per requirements_analysis.md) rather than merely reducing log verbosity.
