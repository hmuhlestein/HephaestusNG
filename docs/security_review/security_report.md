---
type: security_review_result
feature_id: des-91c8-opencode-collector
verdict: PASS
critical_count: 0
high_count: 0
medium_count: 0
low_count: 0
---

# Security Review — OpenCode Cost Collector

## Scope

Reviewed the diff introducing the OpenCode cost collector: `src/services/cost_collection_service.py`
(`OpenCodeCollector`, `_discover_opencode_session`, and the `collect_task_cost` dispatch branch for
`cli_type == "opencode"`) and `tests/test_cost_collection_service.py`. This is an addition to a
pre-existing cost-collection module (`PiJsonlCollector`, `ClaudeCodeCollector`, `CodexStubCollector`)
that had already undergone architectural, adversarial, and prior security review in earlier phases
of this pipeline.

## Findings

### Critical / High
None.

### Medium
None.

### Low
None.

## Areas reviewed

- **Path traversal**: `_discover_opencode_session` resolves a fixed path
  (`~/.local/share/opencode/opencode.db`) and verifies via `.resolve()` + `startswith()` that it
  stays under the expected base directory before opening it. The path is not derived from any
  external input, so this check is defense-in-depth consistent with the existing pattern in
  `_discover_session_file`, not a fix for an actual new traversal vector.
- **SQL injection**: Both `OpenCodeCollector.collect` and `_discover_opencode_session` use
  parameterized `sqlite3` queries (`?` placeholders) for `session_row_id` and
  `(cwd, start_ms, end_ms)`. No string interpolation into SQL anywhere in the diff. `record_cost()`
  (downstream, unchanged) writes via SQLAlchemy ORM attribute assignment, not raw SQL.
- **Input provenance for `cwd`**: `_get_agent_cwd` (unchanged) sources `cwd` from
  `Workflow.working_directory` or `AgentWorktree.worktree_path` — both are values written by
  Hephaestus's own orchestration code from internal DB rows, not attacker-supplied request
  parameters. The `directory = ?` filter in `_discover_opencode_session`'s query is an equality
  match (not LIKE/glob), so even an adversarial `cwd` value can only select rows with that literal
  directory string — no wildcard or traversal semantics apply.
- **Untrusted SQLite content**: `OpenCodeCollector.collect` reads `cost`, token counts, and `model`
  from `opencode.db`'s `session` table row and writes them into `CostEntry` fields. `model` goes
  through `json.loads()` guarded by `except json.JSONDecodeError` with a fallback to the raw string
  — no unsafe deserialization (no `eval`, `pickle`, or unsafe YAML) is involved; malformed data is
  handled, not executed.
- **Read-only DB access**: Both new/touched `sqlite3.connect()` calls use `mode=ro` in the
  connection URI, so the collector cannot write to `opencode.db`.
- **Auth/authz**: `collect_task_cost` is an internal function invoked from
  `task_completion_service` on task completion, not an HTTP-exposed endpoint; no new auth surface
  is introduced by this diff.
- **Secret management**: No API keys, tokens, or credentials are introduced, logged, or persisted
  by this change. Log statements include only file paths, session ID prefixes, and CLI type — no
  secrets.
- **Data handling**: `raw_usage` stored for OpenCode entries is `dict(row)` from the `session`
  table — cost/token counters and model name only, no prompt/completion text or PII.
- **Dependency vulnerabilities**: No new third-party dependencies introduced; `sqlite3` is stdlib.

## Fixes applied in this phase

None required — no critical or high findings.

## Tickets filed

None — no medium/low findings met the reporting bar.
