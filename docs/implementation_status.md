# Cost Tracking Implementation Status

## Summary

The cost tracking system has been fully implemented according to the architecture
document (`docs/COST_TRACKING_DESIGN.md`). All components are functional and tested.

## Implementation Status by Feature

### 1. Cost Schema (cost-schema) ✅ COMPLETE

**Files Modified:**
- `src/core/database.py` — Added `CostEntry` and `SessionCostCheckpoint` tables, added `cost_total_usd` columns to Task/Feature/AutopilotDesign/AutopilotProject/Workflow, added `cost_limit_usd` to AutopilotProject

**Status:** All schema migrations follow the existing `_migrate_*_column` pattern for safe live-migration.

### 2. Cost Derivation Engine (cost-derivation) ✅ COMPLETE

**Files Created/Modified:**
- `src/core/cost_derivation.py` — Pure derivation module with `record_cost()`, `derive_task_cost()`, `derive_workflow_cost()`, `derive_feature_cost()`, `derive_design_cost()`, `derive_project_cost()`

**Key Design Decisions:**
- Follows the same self-healing pattern as `status_derivation.py`
- `record_cost()` is the primary entry point that creates CostEntry and triggers rollup
- Budget enforcement check is called from `derive_project_cost()` after updating totals
- `check_budget_before_new_work()` provides guard for `pick_next_design` and `_run_one_feature`

### 3. CLI Cost Collectors (cost-collectors) ✅ COMPLETE

**Files Created/Modified:**
- `src/services/cost_collection_service.py` — Contains `CostCollector` ABC, `PiJsonlCollector`, `ClaudeCodeCollector`, `OpenCodeCollector`, `CodexStubCollector`
- `src/services/task_completion_service.py` — Wired `collect_task_cost()` into task completion handler

**Key Components:**
- **PiJsonlCollector:** Reads pi JSONL transcripts, extracts `message.usage.cost.total`
- **ClaudeCodeCollector:** Uses price table to convert token counts to dollars (handles Anthropic cache tiers)
- **OpenCodeCollector:** Captures from one-shot JSON output
- **CodexStubCollector:** Logs warning, returns empty (CLI not available)
- **Session discovery:** `_discover_session_file()` finds session files by session_id and cwd
- **Checkpoint management:** Uses `SessionCostCheckpoint` keyed by session_id (not Agent.id) to avoid double-counting on retries

### 4. Budget Enforcement (budget-enforcement) ✅ COMPLETE

**Files Modified:**
- `src/autopilot/orchestrator.py` — Added `pause_project_workflows()`, `_enforce_budget_limit()`, budget guards in `pick_next_design` and `_run_one_feature`
- `src/mcp/autopilot_api.py` — Generalized `paused_by == "user"` checks to `paused_by is not None`

**Key Design Decisions:**
- `pause_project_workflows()` filters `definition_id.in_(["autopilot", "autopilot-phase0"])` (fixes existing bug where Phase 0 was missed)
- Budget-paused workflows cannot be resumed by the play button (only by raising the limit)
- All self-heal guards use `paused_by is not None` (except `start()`'s resume-on-play)

### 5. OpenRouter Direct Cost Capture (openrouter-direct) ✅ COMPLETE

**Files Modified:**
- `src/interfaces/langchain_llm_client.py` — Added `_invoke_and_record()` helper, added `usage: {include: true}` to OpenRouter requests

**Key Components:**
- `_invoke_and_record()` routes all 9 `model.ainvoke()` call sites through one helper
- Extracts cost from `response.response_metadata["token_usage"]["cost"]["total"]`
- Threads `task_id` through method signatures where available
- Methods without task_id context still record cost (with task_id=NULL)

### 6. Pi Cost Tracker Extension (pi-extension) ✅ COMPLETE

**Files:**
- `extensions/hephaestus-cost-tracker/` — TypeScript extension for pi
- `extensions/hephaestus-cost-tracker/README.md` — Installation and configuration docs

**Key Features:**
- Hooks `turn_end` events to capture cost in real-time
- POSTs to `/api/autopilot/cost-entries` endpoint
- Shows running cost in pi TUI status bar
- Falls back to JSONL tailing when extension not loaded

### 7. Dead Code Removal ✅ COMPLETE

**Files Deprecated:**
- `src/interfaces/cost_tracker.py` — Replaced with deprecation stub
- `src/interfaces/openrouter_client.py` — Replaced with deprecation stub

**Rationale:** These files were orphaned remnants of a previous LiteLLM proxy-based approach, not imported anywhere in the codebase.

## Testing

### Test Files
- `tests/test_cost_collection_service.py` — Tests for collectors, session discovery, checkpoint management
- `tests/test_cost_tracking.py` — Tests for cost derivation, schema, security validation

### Test Results
```
74 passed, 257 warnings
```

All tests pass. Warnings are from deprecated Pydantic v1 patterns in other modules (not related to cost tracking).

## Code Quality

### Ruff Checks
```
All checks passed!
5 files already formatted
```

### Type Checking
- `src/core/cost_derivation.py` — Parses correctly
- `src/services/cost_collection_service.py` — Parses correctly
- Note: mypy has a pre-existing syntax error in `src/autopilot/spec.py` that blocks full project type checking

## Integration Points

1. **Task Completion → Cost Collection:** `collect_task_cost()` called from `task_completion_service.py` when `update_task_status(done)`
2. **Cost Entry → Derivation:** `record_cost()` triggers `derive_task_cost()` → `derive_workflow_cost()` → `derive_feature_cost()` → `derive_design_cost()` → `derive_project_cost()`
3. **Derivation → Budget Enforcement:** `derive_project_cost()` calls `_check_budget_enforcement()` which calls `pause_project_workflows()` when limit exceeded
4. **LLM Client → Cost Recording:** `_invoke_and_record()` writes CostEntry after each OpenRouter call
5. **Pi Extension → API:** Extension POSTs to `/api/autopilot/cost-entries` endpoint

## Known Limitations

1. **Claude Code price table requires manual updates** when Anthropic reprices models
2. **Standalone tasks with `--no-session`** have no session file to tail (only caught by OpenRouter direct path)
3. **Codex collector is stubbed** — CLI not available for inspection
4. **Spend always lands slightly over the limit** — cost is only knowable after the LLM call completes

## Documentation

- Architecture: `docs/COST_TRACKING_DESIGN.md`
- Feature scopes: `.hephaestus/features/cost-*/scope.md`
- Pi extension: `extensions/hephaestus-cost-tracker/README.md`
