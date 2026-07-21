# Product Requirements Analysis: Cost Tracking Database Schema

**Feature ID:** cost-tracking-database-schema  
**Feature Name:** Cost Tracking Database Schema  
**Status:** Requirements Extracted  
**Date:** 2026-07-21  
**Design Document:** `.hephaestus/design.md`  
**Related Design Docs:** `design_docs/per_task_cost_tracking.md`, `design_docs/budget_tracking_approval_system.md`

---

## 1. Executive Summary

Implement a comprehensive cost tracking system for HephaestusNG's autopilot pipeline. Currently, cost is tracked at the design/feature level only via dead/unpopulated fields (`pipeline_metrics.json`, `cost_total: float = 0.0`). There is no per-task visibility into LLM token usage and dollar costs, making it impossible to identify expensive phases or optimize spend.

**Current State:** No real cost tracking. OpenRouter calls happen constantly but cost data is not captured. Dead code exists in `src/interfaces/cost_tracker.py` and `src/interfaces/openrouter_client.py` but is not imported anywhere.

**Target State:** Append-only `cost_entries` ledger table (source of truth), denormalized `cost_total_usd` rollup columns on Task/Feature/AutopilotDesign/AutopilotProject (self-healing derivation), per-project budget enforcement with automatic pipeline pause, and collection from multiple CLI agent sources (pi, Claude Code, OpenCode, backend's own OpenRouter calls).

---

## 2. Problem Statement

LLM API calls happen across multiple independent channels:

1. **pi CLI agent sessions** — persistent interactive tmux sessions with cost data in JSONL transcripts
2. **Claude Code sessions** — persistent tmux sessions, tokens-only in transcripts (no dollar cost)
3. **OpenCode sessions** — one-shot invocations with real dollar cost available via stdout/SQLite
4. **Backend's own OpenRouter calls** — task enrichment, Guardian, Conductor (~9 call sites in `LangChainLLMClient`)

None of these channels currently record cost data. The existing `cost_total: float = 0.0` field on reports is never populated. There is no way to answer: "Which tasks consume the most tokens?", "What is the total spend per design?", or "Has this project exceeded its budget?"

---

## 3. Functional Requirements

### FR-1: CostEntry Table (Append-Only Ledger)

**Requirement:** New `CostEntry` SQLAlchemy table — one row per LLM turn/call, not per task.

**Schema:**
```python
class CostEntry(Base):
    __tablename__ = "cost_entries"

    id = Column(String, primary_key=True)  # cost-<uuid8>
    task_id = Column(String, ForeignKey("tasks.id"), nullable=True)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=True)
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True)

    source = Column(String, nullable=False)  # 'pi' | 'claude_code' | 'opencode' | 'codex' | 'openrouter_direct'
    model = Column(String, nullable=True)  # e.g. "anthropic/claude-sonnet-4"

    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cache_read_tokens = Column(Integer, default=0)
    cache_write_tokens = Column(Integer, default=0)
    cost_usd = Column(Float, nullable=False)

    recorded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    raw_usage = Column(JSON, nullable=True)  # Raw source line/turn for debugging
```

**Indexes:** `ix_cost_entries_task_id`, `ix_cost_entries_workflow_id`

**Acceptance Criteria:**
- Table created on startup via `Base.metadata.create_all`
- `task_id` is nullable for non-task-scoped calls (guardian, conductor overhead)
- `source` values are constrained to known sources
- `raw_usage` preserves original transcript data for debugging

**Rationale for append-only ledger:** Aggregates are derived from this table, not hand-maintained. This mirrors the codebase's existing self-healing derivation pattern (`src/core/status_derivation.py`) rather than trusting a single mutable running-total column that can drift under concurrent writes.

---

### FR-2: SessionCostCheckpoint Table

**Requirement:** New table to track progress through CLI session transcript files, keyed by `session_id` (not `Agent.id`).

**Schema:**
```python
class SessionCostCheckpoint(Base):
    __tablename__ = "session_cost_checkpoints"

    session_id = Column(String, primary_key=True)
    lines_processed = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

**Critical Design Decision:** Checkpoint is keyed by `session_id`, NOT by `Agent.id`. Reason: `get_session_id(project_id, design_slug, phase_name)` is a pure function — it has no dependency on which `Agent` row is currently driving it. When an agent dies mid-phase and a retry creates a new `Agent` row, that new agent gets the exact same session ID and resumes the exact same session file. A checkpoint stored on the `Agent` row would start at 0 and double-count every turn the dead agent already ran.

**Acceptance Criteria:**
- Table created on startup
- Checkpoint advances correctly across agent retries
- No double-counting when agent rows change but session_id stays the same

---

### FR-3: Denormalized Rollup Columns

**Requirement:** Add `cost_total_usd = Column(Float, default=0.0, nullable=False)` to:
- `Task` model
- `Feature` model
- `AutopilotDesign` model
- `AutopilotProject` model

**Acceptance Criteria:**
- All four models have the column
- Column populated by `cost_derivation.py` on every new `CostEntry` write
- Rollup chain: `SUM(cost_entries.cost_usd)` grouped by `task_id` → `Feature.workflow_id == Task.workflow_id` → `Feature.design_id` → `AutopilotDesign.project_id`
- Recomputed on write (not independently maintained) so missed updates never permanently desync

---

### FR-4: Cost Derivation Module

**Requirement:** New module `src/core/cost_derivation.py` following the pattern of `src/core/status_derivation.py`.

**Functions:**
- `derive_task_cost(task_id)` — SUM cost_entries for task
- `derive_feature_cost(feature_id)` — SUM costs for all tasks in feature's workflow
- `derive_design_cost(design_id)` — SUM costs for all features in design
- `derive_project_cost(project_id)` — SUM costs for all designs in project
- `derive_cost_totals(cost_entry)` — Full rollup triggered on every new CostEntry write

**Acceptance Criteria:**
- Self-healing: missed updates never permanently desync displayed totals
- Called on every new CostEntry insertion
- Thread-safe for concurrent writes (up to MAX_PARALLEL_FEATURES = 4)

---

### FR-5: Budget Enforcement Schema

**Requirement:** Add `cost_limit_usd = Column(Float, nullable=True)` to `AutopilotProject` model.

- `None` = no limit
- `cost_total_usd` (from FR-3) is what gets compared against it

**Acceptance Criteria:**
- Column exists on AutopilotProject
- Nullable (no limit when None)
- Computed cost_total_usd used for comparison (no redundant "current spend" field)

---

### FR-6: Budget Enforcement Logic

**Requirement:** When `project.cost_total_usd >= project.cost_limit_usd`:

1. **Pause active workflows** — terminate active agents (with `terminated_at` set) and mark active workflows `paused` with `paused_by = "budget"`
2. **Block new work** — guard at top of `pick_next_design` and in `_run_one_feature` before calling `run_single_workflow`
3. **Idempotent pause** — `_pause_project_workflows` is naturally idempotent (only matches `status.in_(["active", "running"])` — second call finds nothing left to pause)

**Critical Gap Fix:** Don't reuse `/autopilot/stop` endpoint query as-is — it misses Phase 0. That endpoint filters `Workflow.definition_id == "autopilot"` but Phase 0 launches under `definition_id == "autopilot-phase0"`. Extract pause logic into shared `_pause_project_workflows(project_id, paused_by)` function that filters `Workflow.definition_id.in_(["autopilot", "autopilot-phase0"])`.

**Acceptance Criteria:**
- Pipeline pauses when budget exceeded
- Phase 0 workflows included in pause
- No new work starts for over-budget project
- Concurrent CostEntry writes don't cause redundant pauses
- Spend always lands at-or-slightly-over limit (cost only knowable after the fact)

---

### FR-7: Generalize `paused_by` Guards

**Requirement:** Change all self-heal/auto-resume guards from `== "user"` to `is not None`:
- `_try_auto_resume_paused_workflow`
- `_create_corrective_task`
- stuck-workflow restart in `attempt_recovery`
- `AutopilotService.start()`'s resume-on-play logic (EXCEPTION: keep `== "user"` here — clicking play should resume user-paused but NOT budget-paused)

**When limit raised or cleared:** If new limit is null or higher than `cost_total_usd`, clear `paused_by` on that project's `"budget"`-paused workflows.

**Acceptance Criteria:**
- Budget-paused workflows don't auto-resume through self-heal paths
- User-paused workflows still don't auto-resume
- Play button resumes user-paused but NOT budget-paused
- Raising limit clears budget pause

---

### FR-8: Pi Extension Collector

**Requirement:** Create pi extension (`extensions/hephaestus-cost-tracker.ts`) that hooks `turn_end` events to capture `message.usage.cost.total` in real-time.

**Data source verified:** Pi session files at `~/.pi/agent/sessions/` contain JSONL with:
```json
{
  "type": "message",
  "message": {
    "role": "assistant",
    "model": "xiaomi/mimo-v2.5",
    "usage": {
      "input": 9430, "output": 222, "cacheRead": 512, "cacheWrite": 0,
      "reasoning": 99, "totalTokens": 10164,
      "cost": {
        "input": 0.00099015, "output": 0.00006216,
        "cacheRead": 0, "cacheWrite": 0, "total": 0.0010523099999999999
      }
    }
  }
}
```

**Session file discovery:**
- Directory key: sanitized `cwd` (slashes → dashes, wrapped in `--`)
- Filename: `<ISO-creation-timestamp>_<session-id>.jsonl`
- Verify by reading first line's `{"type": "session", "id": "<session-id>", "cwd": "..."}`

**Acceptance Criteria:**
- Extension installed globally at `~/.pi/agent/extensions/hephaestus-cost-tracker/`
- POSTs each turn's cost to Hephaestus API immediately (no checkpoint table needed for pi)
- Reads `session_id` from pi session context
- Shows running cost in pi TUI via `ctx.ui.setStatus()`
- Fallback: JSONL tailing still works when extension not loaded

---

### FR-9: Pi JSONL Tailing Collector (Fallback)

**Requirement:** New module `src/services/cost_collection_service.py` with `CostCollector` ABC.

**Checkpoint mechanism:** Read `lines_processed` for session ID, sum `message.usage.cost.total` from `type: "message"` lines after that count where `message.role == "assistant"`, write new `CostEntry` rows, advance `lines_processed`.

**Acceptance Criteria:**
- Collector discovers session file via glob `*_<session_id>.jsonl` in cwd-keyed directory
- Correctly handles shared sessions (SESSION_ROLES maps multiple tasks to one session)
- No double-counting across agent retries (checkpoint keyed by session_id, not agent_id)
- Collection triggered on task completion (`update_task_status` handler), not on timer

---

### FR-10: Claude Code Collector

**Requirement:** Token-to-dollar conversion collector for Claude Code sessions.

**Data source verified:** Claude Code transcripts have `message.usage` with:
```json
{
  "input_tokens": 4736,
  "cache_creation_input_tokens": 2976,
  "cache_read_input_tokens": 8118,
  "output_tokens": 560
}
```
No dollar cost in transcript — only raw tokens. Requires maintained per-model price table.

**Session ID fix required:** `ClaudeCodeAgent.get_launch_command` currently passes no session flag. Must:
1. Derive valid UUID from deterministic inputs: `uuid.uuid5(NAMESPACE, f"{project_id}:{design_slug}:{role}")`
2. Add `--session-id {uuid}` to launch command

**Acceptance Criteria:**
- Price table maintained for all Claude models (input/output/cache rates)
- Two cache-write tiers handled (`ephemeral_1h` vs `ephemeral_5m`)
- Session ID correlation works via UUID5
- Collector falls back to heuristic if session ID unavailable

---

### FR-11: OpenCode Collector

**Requirement:** Capture cost from one-shot `opencode run` invocations.

**Data source verified:** OpenCode runs one-shot (not persistent tmux). Real dollar cost available via `opencode export <sessionID>` with `cost` field, `tokens` breakdown, `modelID`, `providerID`. Storage is SQLite at `~/.local/share/opencode/opencode.db`.

**Two mechanisms (in order of preference):**
1. **Stdout capture:** Add `--format json` to `OpenCodeAgent.get_launch_command`, parse JSON from tmux pane output
2. **Fallback: read opencode.db** after process exits

**Gate on actual usage:** Before building, check `config/workflows/autopilot/workflow.yaml` and `phase_cli_tool` overrides for whether `cli_type: opencode` is set on any live phase.

**Acceptance Criteria:**
- Smoke test `opencode run --format json "..."` to verify payload shape
- Cost captured from stdout or DB
- Collection happens once after process exits (no timer)
- If not in active use, stub as "unsupported"

---

### FR-12: OpenRouter Direct Collector

**Requirement:** Capture cost from backend's own direct OpenRouter calls (~9 call sites in `LangChainLLMClient`).

**Mechanism:** Add `usage: {include: true}` to `extra_body` in `ChatOpenAI` construction. OpenRouter returns non-standard `usage.cost` field.

**Refactor:** Add `_invoke_and_record(model, messages, component, task_id)` helper to avoid duplicating extraction logic across 9 call sites.

**Call sites to wire:**
- `enrich_task`
- `resolve_ticket_clarification`
- `analyze_agent_state`
- `analyze_agent_trajectory`
- `analyze_system_coherence`
- `review_qa_report`
- `generate_agent_prompt` (if exists)
- `generate_embedding` (if applicable)
- Others found via grep

**task_id threading required:** Most methods don't currently have `task_id` parameter — callers know the ID but don't pass it down.

**Acceptance Criteria:**
- `usage.include=true` confirmed working via smoke test
- `_invoke_and_record` helper wraps all call sites
- `task_id` threaded into all methods that are task-scoped
- Non-task-scoped calls (conductor) roll up to workflow or "overhead" bucket

---

### FR-13: Codex Collector Stub

**Requirement:** Stub collector that logs "unsupported" rather than silently reporting zero cost.

**Status:** `codex` not installed on this machine. Need to check if actually used in practice.

**Acceptance Criteria:**
- Stub implemented
- Logs "unsupported" message
- Does not report zero (which would be misleading)

---

### FR-14: UI — Budget Configuration

**Requirement:** Add `cost_limit_usd` number input to `ProjectSettingsModal.tsx`.

**Wiring:** Extend existing `PUT /projects/{project_id}` mutation.

**Acceptance Criteria:**
- Number input per project (optional — blank = no limit)
- Wired to existing mutation pattern
- Backend `ProjectUpdate` model extended

---

### FR-15: UI — Cost Display

**Requirement:** Display cost data in multiple UI locations.

**Autopilot design screen:** "$current / $limit" indicator (or just "$current spent" when no limit) with link to ProjectSettingsModal.

**Paused status distinction:** When workflow shows `paused_by == "budget"`, surface "Paused: budget limit reached" instead of generic "Paused".

**Acceptance Criteria:**
- Design screen shows current spend
- Link to settings for limit configuration
- Budget-paused workflows clearly labeled

---

## 4. Non-Functional Requirements

### NFR-1: Backward Compatibility
- Existing autopilot pipeline continues without cost tracking enabled
- No breaking changes to existing database schema
- Budget enforcement is opt-in (disabled by default)

### NFR-2: Performance
- `CostEntry` writes are < 1ms (SQLite insert)
- Cost derivation rollup on write path must not block pipeline
- Up to MAX_PARALLEL_FEATURES (4) concurrent CostEntry writers

### NFR-3: Reliability
- Append-only ledger is the source of truth (no mutable running totals)
- Self-healing derivation ensures consistency after missed updates
- Budget pause is idempotent (concurrent calls don't cause issues)

### NFR-4: Data Accuracy
- pi collector: exact cost from `message.usage.cost.total`
- Claude Code collector: estimated from token counts × price table
- OpenCode collector: exact cost from stdout or DB
- OpenRouter direct: exact cost from `usage.include=true` response

### NFR-5: Maintenance
- Claude Code price table needs updating when Anthropic reprices
- Codex collector stub logs "unsupported" (not zero)
- Historical backfill NOT supported (rollups start from zero at deploy time)

---

## 5. Technology Constraints

| Constraint | Detail |
|-----------|--------|
| Language | Python 3.12 (existing stack) |
| ORM | SQLAlchemy with StaticPool, expire_on_commit=False |
| Database | SQLite with WAL mode (existing) |
| Migrations | Follow `_migrate_*_column` pattern in `database.py` |
| Frontend | React 18, TypeScript, Tailwind CSS (existing) |
| No new dependencies | Pure extensions of existing patterns |

---

## 6. Integration Points

### 6.1 Existing Code (Modify)

| File | Change |
|------|--------|
| `src/core/database.py` | Add CostEntry, SessionCostCheckpoint tables; add cost_total_usd columns to Task/Feature/AutopilotDesign/AutopilotProject; add cost_limit_usd to AutopilotProject; add migration functions |
| `src/core/status_derivation.py` | Reference pattern for cost_derivation.py |
| `src/autopilot/orchestrator.py` | Extract `_pause_project_workflows` from `/autopilot/stop` handler; add budget checks in `pick_next_design` and `_run_one_feature` |
| `src/mcp/autopilot_api.py` | Extend `PUT /projects/{project_id}` for cost_limit_usd |
| `src/interfaces/langchain_llm_client.py` | Add `_invoke_and_record` helper; wire all 9 call sites; add `usage.include=true` |
| `src/agents/manager.py` | Propagate cost data from CLI agent sessions to CostEntry |
| `src/services/task_completion_service.py` | Trigger cost collection on task done |
| `src/interfaces/cli_interface.py` | Add `--session-id` to ClaudeCodeAgent; fix UUID derivation |
| `frontend/src/components/ProjectSettingsModal.tsx` | Add cost_limit_usd input |
| `frontend/src/components/autopilot/DesignQueuePanel.tsx` | Add cost display |

### 6.2 Existing Code (Reference Only)

| File | Why |
|------|-----|
| `src/core/status_derivation.py` | Pattern for self-healing derivation |
| `src/interfaces/cost_tracker.py` | Dead code — shows what was previously attempted |
| `src/monitoring/guardian.py` | LLM call site for cost tracking |
| `src/monitoring/conductor.py` | LLM call site for cost tracking |

### 6.3 New Files

| File | Purpose |
|------|---------|
| `src/core/cost_derivation.py` | Self-healing cost rollup module |
| `src/services/cost_collection_service.py` | Per-CLI transcript tailing collectors |
| `extensions/hephaestus-cost-tracker.ts` | Pi extension for real-time cost capture |

---

## 7. Implementation Phases

### Phase 1: Schema
- `cost_entries` and `session_cost_checkpoints` tables
- `cost_total_usd` columns on Task/Feature/AutopilotDesign/AutopilotProject
- `cost_limit_usd` on AutopilotProject
- Migration following existing `_migrate_*_column` pattern

### Phase 2: Pi Collector
- JSONL tailing collector + checkpoint mechanism
- `cost_derivation.py` rollup
- Wire into task completion handler
- Verify against real running pipeline

### Phase 3: Budget Enforcement
- `_pause_project_workflows` extraction (fixing `/autopilot/stop` gap)
- Enforcement check in `cost_derivation.py` rollup path
- `is not None` generalization of `paused_by` guards
- Budget checks in `pick_next_design` and `_run_one_feature`
- Land after pi collector (earliest real cost data)

### Phase 4: Claude Code Collector
- UUID5 session-ID fix
- Price-table-based collector
- Verify against real Claude Code sessions

### Phase 5: OpenRouter Direct
- Confirm `usage.include=true` works
- Wire `_invoke_and_record` across all 9 call sites
- Thread `task_id` into methods

### Phase 6: OpenCode Collector
- Gate on actual usage in workflow.yaml
- Smoke test `opencode run --format json`
- Implement stdout capture or DB read

### Phase 7: UI
- Budget config input
- Cost display on design screen
- Budget-paused status label

### Phase 8: Codex Collector
- Stub implementation (logs "unsupported")
- Full implementation when CLI available to inspect

---

## 8. Critical Design Decisions

### D-1: Append-Only Ledger vs Mutable Totals
**Decision:** Append-only `cost_entries` table as source of truth; denormalized `cost_total_usd` columns are derived, not maintained independently.
**Rationale:** Matches existing self-healing pattern in `status_derivation.py`. A missed update never permanently desyncs the displayed total from the ledger.

### D-2: Checkpoint by Session ID vs Agent ID
**Decision:** `SessionCostCheckpoint` keyed by `session_id`, NOT `Agent.id`.
**Rationale:** When an agent dies and retries, the new agent gets the same session ID and resumes the same file. A checkpoint on the agent row would double-count.

### D-3: Collection on Task Completion vs Timer
**Decision:** Collect cost on task completion (`update_task_status` handler), not on a timer.
**Rationale:** Session activity is fully written to disk by the time done lands. No torn-read risk. Avoids separate polling loop.

### D-4: Pi Extension vs Raw JSONL Tailing
**Decision:** Pi extension preferred over raw JSONL tailing for pi sessions.
**Rationale:** No file-system access needed. Real-time TUI display. No checkpoint table needed for pi. JSONL tailing remains as fallback.

### D-5: Single Shared Pause Function
**Decision:** Extract `_pause_project_workflows(project_id, paused_by)` from `/autopilot/stop` route handler.
**Rationale:** Current endpoint misses Phase 0 (only matches `"autopilot"`, not `"autopilot-phase0"`). Shared function fixes both.

### D-6: `paused_by` Generalization
**Decision:** Change guards from `== "user"` to `is not None`, EXCEPT in `AutopilotService.start()`.
**Rationale:** Any non-null paused_by means something deliberately paused this. Start() keeps `== "user"` because clicking play should resume user-paused but NOT budget-paused.

---

## 9. Open Questions

| # | Question | Status |
|---|----------|--------|
| Q1 | Should we track cost for non-LLM operations (tool calls, etc.)? | Deferred to future |
| Q2 | Should cost be rounded or stored with full precision? | Store full precision |
| Q3 | Do we need a cost budget/alert system per design (not just per project)? | Per-project only for now |
| Q4 | Is OpenCode actually used in any live phase? | Check workflow.yaml before building |
| Q5 | Does OpenCode's `-s` flag accept caller-chosen new session IDs? | Needs live test |
| Q6 | Does `usage.include=true` survive LangChain's response parsing? | Needs smoke test |
| Q7 | Should standalone tasks (no session_id) be forced to always pass a session ID? | Flagged, not resolved |

---

## 10. Acceptance Criteria Summary

| ID | Criterion | Verification |
|----|-----------|--------------|
| AC-1 | CostEntry table created | `from src.core.database import CostEntry` succeeds |
| AC-2 | SessionCostCheckpoint table created | Table exists in DB |
| AC-3 | cost_total_usd on Task/Feature/Design/Project | All four models have column |
| AC-4 | cost_limit_usd on AutopilotProject | Column exists, nullable |
| AC-5 | Pi collector captures real cost | CostEntry rows populated after pi agent task |
| AC-6 | Cost derivation self-heals | Missing updates recovered on next write |
| AC-7 | Budget pauses pipeline | Workflows paused when limit exceeded |
| AC-8 | Phase 0 included in budget pause | `_pause_project_workflows` matches both definition_ids |
| AC-9 | Budget-paused doesn't auto-resume | Self-heal guards use `is not None` |
| AC-10 | Play button doesn't clear budget pause | `start()` keeps `== "user"` filter |
| AC-11 | Raising limit clears budget pause | `PUT /projects/{id}` clears `"budget"`-paused |
| AC-12 | UI shows cost data | Design screen displays spend |
| AC-13 | Budget config works | ProjectSettingsModal has limit input |
| AC-14 | Existing tests pass | All tests green |
| AC-15 | No new dependencies | Pure SQLAlchemy/stdlib |

---

## 11. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Claude Code price table goes stale | Medium | Medium | Document update process; fallback to zero |
| Concurrent CostEntry writes cause contention | Low | Medium | WAL mode + QueuePool handle this |
| Pi extension not loaded | Medium | Low | JSONL tailing fallback still works |
| Budget enforcement misses edge case | Medium | High | Comprehensive testing of pause/resume paths |
| Historical data unavailable | N/A | Low | Noted in Non-Goals; rollups start from deploy |

---

## 12. Non-Goals (Explicitly Deferred)

- **Real-time streaming cost display mid-task for non-pi CLIs.** Pi extension provides real-time cost. Claude Code and OpenCode collection at task completion.
- **Codex collector implementation.** Stubbed only; needs CLI installed to inspect transcript format.
- **Historical backfill.** No cost data exists for tasks that already ran before this lands; rollups start from zero at deploy time.
- **Per-design budget limits.** Per-project only for now.

---

**Requirements extracted. Ready for Scope Review and Architecture Design.**
