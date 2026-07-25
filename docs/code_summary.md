# Code Summary: Backend OpenRouter Direct Cost Capture

**Feature ID:** des-91c8-openrouter-direct
**Date:** 2026-07-24

## What changed

The direct OpenRouter cost-capture mechanism (intercepting orchestrator LLM
calls, extracting token/cost usage, writing `CostEntry` rows) already existed
on this branch from earlier features. This feature closed the two gaps
identified in `docs/requirements_analysis.md` §0: missing test coverage and
an over-broad exception handler.

### `src/interfaces/langchain_llm_client.py`

`_invoke_and_record()` — the single choke point wrapping every orchestrator
`model.ainvoke()` call:

- `metadata.get("token_usage", {})` → `metadata.get("token_usage") or {}`,
  and the same pattern for `usage.get("cost", {})` and
  `usage.get("prompt_tokens_details", {})`. Fixes a bug where an explicit
  JSON `null` (as opposed to a missing key) in OpenRouter's response would
  crash the `.get()` chain — `{}.get(...)` on `None` raises `AttributeError`,
  which the surrounding `try/except` swallows silently.
- `logger.debug(...)` → `logger.warning(...)` on extraction/write failure, so
  a real bug (e.g. a LangChain response-shape change) surfaces in normal
  logs instead of only under debug logging.

### `tests/test_cost_tracking.py`

New `TestInvokeAndRecord` class covering `_invoke_and_record`'s extraction
logic directly (previously only `CostEntry`/`record_cost`/rollup were
tested, never the extraction path itself):

- Happy path: realistic `response_metadata` shape → exactly one `CostEntry`
  with correct `cost_usd`, `input_tokens`, `output_tokens`,
  `cache_read_tokens`, `model`, `task_id`.
- No-cost path (non-OpenRouter provider shape): no `CostEntry` written.
- Missing `response_metadata`: does not raise, response still returned.
- Malformed/`null` metadata fields: logs a warning, still returns the
  response, doesn't crash the call site.

## Why

Requirements analysis found the cost-capture mechanism itself was already
built by prior features; this feature's actual job was verification and
hardening — proving the extraction logic works via tests, and making
extraction failures visible instead of silent. No schema, API, or new-file
changes beyond the one test file.

## Out of scope (unchanged)

Budget enforcement guards, Claude Code/OpenCode/Codex collectors, Pi
extension changes, UI budget configuration, `CostEntry` schema/rollup logic.
