# Feature: Cost Tracking Database Schema

## Overview
Create the foundational database schema for cost tracking: the `cost_entries` append-only ledger table (one row per LLM turn/call with task_id, source, model, token breakdowns, cost_usd, raw_usage), the `session_cost_checkpoints` table (keyed by session_id for incremental collection tracking), add `cost_total_usd` denormalized columns to Task/Feature/AutopilotDesign/AutopilotProject, and add `cost_limit_usd` to AutopilotProject. Follow the existing `_migrate_*_column` pattern in `database.py` for safe live-migration on existing databases. Also remove dead code files `src/interfaces/cost_tracker.py` and `src/interfaces/openrouter_client.py` which are orphaned remnants of a previous LiteLLM proxy-based approach and are no longer imported anywhere.

## Files Owned
- `src/core/database.py`
- `src/interfaces/cost_tracker.py` (DELETE — dead code removal)
- `src/interfaces/openrouter_client.py` (DELETE — dead code removal)

## Dependencies
None — this is the foundational schema that all other cost-related features depend on.

## Implementation Notes

### New table: `cost_entries`
One row per LLM turn/call. Key columns:
- `id` (String PK, `cost-<uuid8>`)
- `task_id` (FK → tasks.id, nullable — some calls aren't task-scoped)
- `agent_id` (FK → agents.id, nullable)
- `workflow_id` (FK → workflows.id, nullable)
- `source` (String, NOT NULL — `'pi'` | `'claude_code'` | `'opencode'` | `'codex'` | `'openrouter_direct'`)
- `model` (String, nullable)
- `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` (Integer, default 0)
- `cost_usd` (Float, NOT NULL)
- `recorded_at` (DateTime, default utcnow)
- `raw_usage` (JSON, nullable)
- Indexes: `ix_cost_entries_task_id`, `ix_cost_entries_workflow_id`

### New table: `session_cost_checkpoints`
Keyed by `session_id` (not Agent.id — this is critical to avoid double-counting on agent retries sharing the same session). Columns:
- `session_id` (String PK)
- `lines_processed` (Integer, default 0)
- `updated_at` (DateTime)

### New columns on existing tables
- `Task.cost_total_usd = Column(Float, default=0.0, nullable=False)`
- `Feature.cost_total_usd = Column(Float, default=0.0, nullable=False)`
- `AutopilotDesign.cost_total_usd = Column(Float, default=0.0, nullable=False)`
- `AutopilotProject.cost_total_usd = Column(Float, default=0.0, nullable=False)`
- `AutopilotProject.cost_limit_usd = Column(Float, nullable=True)` — None = no limit

### Migration pattern
Follow the existing `_migrate_workflow_paused_by_column` pattern: check for `OperationalError` on `insert`, run `ALTER TABLE ADD COLUMN` via `text()`, log success. Add a `_migrate_cost_columns` method to the `Database` class that handles all new columns, and call it from `__init__` alongside the existing migration calls.

## Acceptance Criteria
- [ ] `cost_entries` table exists with all specified columns and indexes
- [ ] `session_cost_checkpoints` table exists with `session_id` PK and `lines_processed`
- [ ] `cost_total_usd` column exists on Task, Feature, AutopilotDesign, and AutopilotProject (default 0.0)
- [ ] `cost_limit_usd` column exists on AutopilotProject (nullable, default None)
- [ ] Migrations run safely against existing databases without data loss (follow `_migrate_*_column` pattern)
- [ ] `src/interfaces/cost_tracker.py` deleted (dead code)
- [ ] `src/interfaces/openrouter_client.py` deleted (dead code)