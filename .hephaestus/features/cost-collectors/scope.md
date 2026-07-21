# Feature: CLI Cost Collectors (Pi + Claude Code)

## Overview
Build the Python cost collection infrastructure in `src/services/cost_collection_service.py`: an abstract `CostCollector` base class with `collect_since(session_file, checkpoint)` returning `(list[CostEntry], new_checkpoint)`, plus `PiCollector` (reads pi JSONL transcripts, sums `message.usage.cost.total` from assistant turns since checkpoint) and `ClaudeCodeCollector` (uses a maintained per-model price table to convert raw token counts from Claude Code transcripts into dollar amounts, including the two Anthropic cache-write tiers). Also includes: (1) the UUID5 deterministic session-ID fix in `ClaudeCodeAgent.get_launch_command` so Claude Code sessions are correlateable to tasks (derive a valid UUID via `uuid.uuid5(NAMESPACE, ...)`), (2) a cost-ingestion API endpoint in `src/mcp/server.py` for the pi extension to POST per-turn cost data, and (3) wiring collection into `src/services/task_completion_service.py` so cost is gathered when `update_task_status(done)` lands. The `SessionCostCheckpoint` table (from cost-schema) is used for incremental reads keyed by session_id, not by Agent.id, to avoid double-counting on agent retries.

## Files Owned
- `src/services/cost_collection_service.py`
- `src/interfaces/cli_interface.py`
- `src/mcp/server.py`
- `src/services/task_completion_service.py`

## Dependencies
- `cost-schema` — reads/writes `cost_entries`, `session_cost_checkpoints` (including `reasoning_tokens` field)
- `cost-derivation` — calls `derive_cost_totals()` after new entries are written; `derive_cost_totals` is a pure derivation function with no budget enforcement side effects
- `budget-enforcement` — after calling `derive_cost_totals()`, this feature calls `_enforce_budget_limit(project_id)` to trigger enforcement if the cost limit is crossed; this is a direct import dependency on `src/autopilot/orchestrator.py`

## Implementation Notes

### PiCollector — JSONL transcript tailing
1. Given `session_id` and the agent's `cwd` (worktree path), construct the session directory key: replace `/` with `-`, wrap in leading/trailing `--`, look in `~/.pi/agent/sessions/<key>/`
2. Glob `*_<session_id>.jsonl` in that directory
3. Verify the glob match by reading the file's first line (`type: "session"`, `id == session_id`)
4. Read lines from `lines_processed` checkpoint onward
5. For each line where `type == "message"` and `message.role == "assistant"`: extract `message.usage.cost.total`, `message.usage.input`, `message.usage.output`, `message.usage.reasoning`, `message.usage.cacheRead`, `message.usage.cacheWrite`, `model`, `provider`
6. Create a `CostEntry` per turn with `source="pi"`, `task_id` from current context; map `message.usage.reasoning` to `reasoning_tokens`
7. Advance `session_cost_checkpoints.lines_processed`

### ClaudeCodeCollector — price-table conversion
1. Same session-file discovery pattern as PiCollector, adapted for Claude Code's directory structure (`~/.claude/projects/<cwd-keyed-dir>/`)
2. No `cost` field exists in transcripts — must convert tokens to dollars using a maintained price table
3. Price table format (Python dict constant at module level): `PRICING = {"anthropic/claude-sonnet-4": {"input_per_mtok": ..., "output_per_mtok": ..., "cache_write_1h_per_mtok": ..., "cache_write_5m_per_mtok": ..., "cache_read_per_mtok": ...}}`
4. For each assistant message: `(input_tokens / 1_000_000) * input_per_mtok + ...` etc., also extract `usage.reasoning_tokens` for the `reasoning_tokens` column
5. Important: Anthropic has two cache-write tiers (`ephemeral_1h_input_tokens` and `ephemeral_5m_input_tokens`) at different rates

### UUID5 session-ID fix for Claude Code
In `src/interfaces/cli_interface.py`, modify `ClaudeCodeAgent.get_launch_command`:
- Generate: `session_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hephaestus:{project_id}:{design_slug}:{role}"))`
- Add `--session-id {session_uuid}` to the launch command
- Ensure `get_session_args` returns this UUID so callers can store it

Also do the same UUID5 derivation in `PiAgent` — derive a valid UUID from the deterministic inputs, keeping backward compatibility with existing slug-format session IDs by using the same `--session-id` flag but with the UUID now valid for both pi and claude.

### Codex stub
In `cost_collection_service.py`, add a `CodexCollector` stub that:
- Logs `"Codex cost collection not implemented — CLI not available for inspection"` 
- Returns empty entries
- Does NOT silently report zero cost

### Cost-ingestion API endpoint
Add to `src/mcp/server.py`:
- `POST /api/cost` accepting `{session_id, source, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, raw_usage}`
- `task_id` is optional — if omitted, the endpoint resolves `task_id` by querying the Agent table for the most recent agent with a matching session_id (via `Agent.session_id`), extracting that agent's `task_id`. If no matching agent is found, the entry is written with `task_id=NULL` (it will roll up to the project-level overhead bucket)
- Creates a `CostEntry` with the resolved or provided `task_id`, calls `derive_cost_totals(db, task_id)`
- Used by the pi extension for real-time cost reporting (pi extension omits `task_id` since it doesn't know the task context)

### Budget enforcement hook (after derivation)
After calling `derive_cost_totals(db, task_id)`, call `_enforce_budget_limit(project_id)` (from `src/autopilot/orchestrator.py`, part of the `budget-enforcement` feature — this feature has a direct dependency on `budget-enforcement`). That function checks `cost_total_usd >= cost_limit_usd` and pauses the project's workflows if the limit is crossed. This is the single integration point between cost collection and budget enforcement — cost-derivation itself is pure computation.

### Wiring into task completion
In `task_completion_service.py`, when `update_task_status(status="done")`:
- Look up the task's `session_id` (from the Agent row or workflow metadata)
- Call `PiCollector.collect_since(...)` and `ClaudeCodeCollector.collect_since(...)`
- Write resulting `CostEntry` rows
- Call `derive_cost_totals(db, task_id)` → triggers total recalculation
- Call `_enforce_budget_limit(project_id)` → triggers enforcement if limit crossed

### Known Limitation: Standalone tasks with `--no-session` are uncosted
Tasks launched with `'no-session` (when `PiAgent.get_session_args` falls back with no `session_id` passed — e.g., standalone, non-phase tasks; see `src/interfaces/cli_interface.py:501`) produce no Transcript-file to tail, so the pi JSONL collector has nothing to read for these tasks. Only costs captured via the secondary paths (OpenRouter direct, pi extension POST) flow through for these tasks. This is an inherent limitation of the file-tailing design, not a bug, but users should not expect 100% CLI-attributed cost coverage for all task types.

### Claude Code price table maintenance
The Claude Code collector's `PRICING` constant at module level requires manual updates when Anthropic reprices models. Include a `# Last verified: YYYY-MM-DD` comment next to the constant, and document this requirement in a module docstring or README comment. The two Anthropic cache-write tiers (`ephemeral_1h` vs `ephemeral_5m`) at different rates must both be represented.

### Dead code removal
The orphaned files `src/interfaces/cost_tracker.py` and `src/interfaces/openrouter_client.py` are handled by the `cost-schema` feature (deleted as part of schema setup). This feature builds fresh cost collection logic in `src/services/cost_collection_service.py`.

## Acceptance Criteria
- [ ] `CostCollector` ABC exists in `src/services/cost_collection_service.py` with `collect_since()` interface
- [ ] `PiCollector` correctly reads pi JSONL transcripts using glob-and-verify pattern and extracts per-turn cost
- [ ] `ClaudeCodeCollector` correctly converts token counts to dollar amounts using the price table with cache tier support
- [ ] `ClaudeCodeAgent.get_launch_command` passes `--session-id` with a valid UUID5 derived from deterministic inputs
- [ ] Cost-ingestion API endpoint exists at `POST /api/cost` and creates CostEntry + triggers derivation
- [ ] Task completion handler gathers cost from active collectors
- [ ] Codex collector stubs gracefully with a log warning, not silent zero