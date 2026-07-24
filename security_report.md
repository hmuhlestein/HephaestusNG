# Security Review — Backend OpenRouter Direct Cost Capture

## Scope

Diffed this branch against `main` and found the actual feature delta is small and
narrowly scoped:

- `src/interfaces/langchain_llm_client.py` — `_invoke_and_record()`: null-safety
  fixes when parsing OpenRouter's `response_metadata` (defensive `.get(...) or {}`
  instead of `.get(..., {})`, which mishandles explicit JSON `null`), plus a
  log-level bump (debug → warning) on parse failure.
- `tests/test_cost_tracking.py` — new unit tests for the above and for
  `src/core/cost_derivation.py`'s `record_cost`/`derive_*_cost`/budget-enforcement
  functions.

The other files in `git diff main --stat` (`src/autopilot/orchestrator.py`,
`src/core/database.py`, `frontend/src/context/WebSocketContext.tsx`,
`docs/architecture.md`, `docs/requirements_analysis.md`,
`docs/scope_review/scope_review_result.json`, `tests/test_orchestrator_helpers.py`,
`tests/test_self_review_migration.py`) are **not** part of this feature — this
branch is one commit behind `main` (missing `cdb7d0d`, a self-heal-heuristic fix
landed on main after this branch diverged) and diffs against those files just
reflect that gap. No security review action taken there; recommend rebasing before
merge so this branch doesn't reintroduce the reverted heuristics.

The cost-ingestion infrastructure this feature writes into (`CostEntry` model,
`POST /cost-entries` HTTP endpoint, `CostEntryCreate` Pydantic validation, agent
auth, rate limiting) is pre-existing and unchanged by this branch — already carries
its own validation (`source` allow-list, `cost_usd` bounds, token-count bounds,
`raw_usage` 10KB cap, model-name length cap, `X-Agent-ID` auth, 60 req/min rate
limit, mandatory `task_id`/`workflow_id` link for budget-enforcement rollup). Not
re-audited line-by-line here since it's untouched by this feature, but traced end
to end to confirm the new code path feeds into it safely.

## Findings

### Critical / High
None.

### Medium
None.

### Low
1. **`record_cost()` doesn't enforce a `raw_usage` size cap at the function
   level** — only the HTTP endpoint's Pydantic validator (`CostEntryCreate.
   validate_raw_usage`) caps `raw_usage` at 10KB and `model` at 200 chars.
   `_invoke_and_record()` calls `record_cost()` directly (in-process, bypassing
   HTTP), so callers other than the HTTP endpoint get no such bound. In this
   feature's case the `raw_usage` payload originates from OpenRouter's own
   `token_usage` object — a trusted third party already holding the API key, not
   attacker-controlled — so this isn't currently exploitable. Filed as
   `ticket-c07312d3-3243-4650-bf52-e5773c7ce738` (low priority) to move the size
   caps into `record_cost()` itself so the invariant holds for every caller, not
   just the HTTP layer.

## Areas reviewed

- **Auth/authz**: `POST /cost-entries` requires `X-Agent-ID` + `verify_agent_authentication`
  (pre-existing, unchanged). The new code path (`_invoke_and_record`) never crosses
  an HTTP boundary — it's an internal function call within the same process, so no
  additional auth surface was introduced.
- **Input validation**: cost/token values from OpenRouter flow through
  `record_cost()`, which rejects negative `cost_usd` and caps it at $1000
  (pre-existing, unchanged by this diff). The new `.get(...) or {}` pattern only
  changes how `None` values (vs. missing keys) in the response are defaulted —
  verified this can't be used to inject non-dict/non-numeric values into `cost_usd`
  (arithmetic on a non-numeric `cost_data.get("total", 0)` would raise inside the
  existing broad `try/except`, which already logs and safely no-ops).
  See `test_malformed_metadata_logs_warning_and_still_returns_response` and
  `test_null_prompt_tokens_details_still_writes_cost_entry`.
- **Data handling/storage**: `raw_usage` (token/cost metadata only — no prompt or
  completion text) is stored in the `cost_entries.raw_usage` JSON column. No PII or
  secrets observed in the fields written (`prompt_tokens`, `completion_tokens`,
  `cost`, `model_name`, cache token counts).
- **Secret management**: OpenRouter API key is loaded from `os.getenv(provider_config.
  api_key_env)` (pre-existing) and never appears in logs, `raw_usage`, or the new
  code path. `logger.warning(f"Cost recording failed for {component}: {e}")` logs
  only the component name and exception string — no request/response bodies.
- **Injection**: All DB writes go through SQLAlchemy ORM (`CostEntry(...)`,
  `db.query(...)`); no raw SQL string interpolation in the touched code.
- **Dependency vulnerabilities**: No new dependencies introduced by this diff.
- **Error handling / availability (OWASP A04/A09)**: `_invoke_and_record()` wraps
  cost-recording in a broad `try/except` so a malformed or missing
  `response_metadata` (any provider that isn't OpenRouter, or a transient parsing
  bug) never breaks the underlying LLM call — verified by
  `test_missing_response_metadata_does_not_raise` and
  `test_non_openrouter_response_writes_no_cost_entry`.

## Fixes applied in this phase

None required — no critical or high findings.

## Tickets filed

- `ticket-c07312d3-3243-4650-bf52-e5773c7ce738` (low, improvement): move
  `raw_usage`/`model` size caps into `record_cost()` so in-process callers get the
  same bound as the HTTP endpoint.
