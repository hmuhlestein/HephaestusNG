# Product Requirements Analysis: Cost Derivation Engine

**Feature ID:** cost-derivation-engine
**Feature Name:** Cost Derivation Engine
**Status:** Requirements Extracted
**Date:** 2026-07-21
**Design Document:** `.hephaestus/design.md` (primary), `docs/COST_TRACKING_DESIGN.md`
**Related Design Docs:** `design_docs/per_task_cost_tracking.md`, `design_docs/budget_tracking_approval_system.md`
**Branch:** `feature/des-91c8-cost-derivation`

---

## 1. Executive Summary

Implement a comprehensive cost derivation engine for HephaestusNG's autopilot pipeline. The engine computes per-task, per-workflow, per-feature, per-design, and per-project cost totals from an append-only `cost_entries` ledger, following the same self-healing derivation pattern already established by `src/core/status_derivation.py`.

**Current State:** The core module `src/core/cost_derivation.py` is already implemented with `record_cost`, `derive_task_cost`, `derive_workflow_cost`, `derive_feature_cost`, `derive_design_cost`, `derive_project_cost`, `check_budget_before_new_work`, and `_pause_project_workflows`. The `CostEntry` and `SessionCostCheckpoint` models exist in `database.py`. The `cost_collection_service.py` implements collectors for pi, Claude Code, OpenCode, and Codex (stub). The `cost_total_usd` rollup columns exist on Task, Feature, Workflow, AutopilotDesign, and AutopilotProject. The `cost_limit_usd` column exists on AutopilotProject.

**Target State:** A fully wired cost derivation engine that captures cost data from all LLM sources, derives accurate rollup totals, enforces per-project budgets, and surfaces cost data in the UI — with comprehensive test coverage and documentation.

---

## 2. Problem Statement

LLM API calls happen across multiple independent channels:

1. **pi CLI agent sessions** — persistent interactive tmux sessions with cost data in JSONL transcripts (`message.usage.cost.total`)
2. **Claude Code sessions** — persistent tmux sessions with token-only transcripts (no dollar cost — requires price table conversion)
3. **OpenCode sessions** — one-shot invocations with real dollar cost available via stdout/SQLite
4. **Backend's own OpenRouter calls** — task enrichment, Guardian, Conductor (~9 call sites in `LangChainLLMClient`)
5. **Codex sessions** — not yet available for inspection (stub only)

None of these channels currently records cost data into a unified ledger. The existing `cost_total: float = 0.0` field on reports (`orchestrator.py:183`, surfaced at `autopilot_api.py:2801`) is never populated above 0.0. Dead code exists in `src/interfaces/cost_tracker.py` (queries a LiteLLM proxy that isn't used) and `src/interfaces/openrouter_client.py` (orphaned).

**Business Impact:** No visibility into which tasks consume the most tokens, what total spend per design is, or whether a project has exceeded its budget. A runaway design iteration can consume hundreds of dollars with no visibility until after completion.

---

## 3. Project Context & Vision

### 3.1 Hephaestus Architecture

Hephaestus is a semi-structured agentic framework where AI agents handle complex software projects through a branching tree of tasks. The autopilot pipeline runs designs through multiple phases (requirements → architecture → development → review → QA → validation → docs → forensics → commit), with agents spawned in tmux sessions using various CLI tools (pi, Claude Code, OpenCode, Codex).

**Key Architectural Patterns:**
- **Self-healing derivation:** `status_derivation.py` derives entity statuses from child entities, recomputing on every access so missed updates never permanently desync. Cost derivation follows this exact pattern.
- **Append-only ledgers:** Data is accumulated in append-only tables; aggregates are derived, not maintained as mutable running totals.
- **Session roles:** `SESSION_ROLES` maps multiple phases to one session role (e.g., `architecture_design` and `architectural_review` share the `architect` role), allowing agents to retain conversational context across phases.
- **Concurrent execution:** Up to `MAX_PARALLEL_FEATURES` (4) features can run concurrently, each with independent cost recording.

### 3.2 Entity Hierarchy

```
AutopilotProject
  └── AutopilotDesign (design.md files in the queue)
        └── Feature (major components of a design)
              └── Workflow (pipeline execution for a feature)
                    └── Task (individual work items within a workflow)
                          └── CostEntry (individual LLM turn/call)
```

### 3.3 Previously Completed Features

| Feature | Status | Relevance |
|---------|--------|-----------|
| Status Derivation Engine | Complete | **Direct pattern model** — `cost_derivation.py` mirrors `status_derivation.py` exactly |
| Ticket Tracking System | Complete | Uses task-level granularity; cost tracking extends this |
| Multi-Project Concurrency | Complete | Concurrent features = concurrent CostEntry writes (thread safety) |
| Workflow Phases Merge | Complete | SESSION_ROLES sharing = checkpoint-by-session_id requirement |
| Gap Check Self-Loop | Complete | Auto-resume guards that need `paused_by` generalization |

---

## 4. Functional Requirements

### FR-1: CostEntry Table (Append-Only Ledger) — IMPLEMENTED

**Requirement:** `CostEntry` SQLAlchemy table — one row per LLM turn/call, not per task.

**Schema (already in `database.py:1227`):**
```python
class CostEntry(Base):
    __tablename__ = "cost_entries"
    id = Column(String, primary_key=True)              # cost-<uuid8>
    task_id = Column(String, ForeignKey("tasks.id"), nullable=True)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=True)
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True)
    source = Column(String, nullable=False)             # pi|claude_code|opencode|codex|openrouter_direct
    model = Column(String, nullable=True)               # e.g. "anthropic/claude-sonnet-4"
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cache_read_tokens = Column(Integer, default=0)
    cache_write_tokens = Column(Integer, default=0)
    reasoning_tokens = Column(Integer, default=0)
    cost_usd = Column(Float, nullable=False)
    recorded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    raw_usage = Column(JSON, nullable=True)
```

**Indexes:** `ix_cost_entries_task_id`, `ix_cost_entries_workflow_id`, `ix_cost_entries_recorded_at`

**Acceptance Criteria:**
- [x] Table created on startup via `Base.metadata.create_all`
- [x] `task_id` is nullable for non-task-scoped calls (guardian, conductor overhead)
- [x] `source` values are constrained to known sources
- [x] `raw_usage` preserves original transcript data for debugging
- [x] Migration function `_migrate_cost_tables` in `database.py`

**Rationale for append-only ledger:** Aggregates are derived from this table, not hand-maintained. Mirrors `status_derivation.py`'s self-healing pattern. A missed update never permanently desyncs the displayed total from the ledger.

---

### FR-2: SessionCostCheckpoint Table — IMPLEMENTED

**Requirement:** Track progress through CLI session transcript files, keyed by `session_id` (NOT `Agent.id`).

**Schema (already in `database.py:1268`):**
```python
class SessionCostCheckpoint(Base):
    __tablename__ = "session_cost_checkpoints"
    session_id = Column(String, primary_key=True)
    lines_processed = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

**Critical Design Decision:** Checkpoint keyed by `session_id`, NOT `Agent.id`. Rationale: `get_session_id(project_id, design_slug, phase_name)` is a pure function — it has no dependency on which `Agent` row drives it. When an agent dies mid-phase and retries, the new agent gets the same session ID and resumes the same file. A checkpoint on `Agent.id` would start at 0 and double-count every turn the dead agent already ran.

**Acceptance Criteria:**
- [x] Table created on startup
- [x] Checkpoint advances correctly across agent retries
- [ ] No double-counting when agent rows change but session_id stays the same (**needs integration test**)

---

### FR-3: Denormalized Rollup Columns — IMPLEMENTED

**Requirement:** `cost_total_usd = Column(Float, default=0.0, nullable=False)` on all hierarchy levels.

| Model | Column | Status |
|-------|--------|--------|
| `Task` (database.py:279) | `cost_total_usd` | ✅ Implemented |
| `Feature` (database.py:452) | `cost_total_usd` | ✅ Implemented |
| `Workflow` (database.py:1143) | `cost_total_usd` | ✅ Implemented |
| `AutopilotDesign` (database.py:1104) | `cost_total_usd` | ✅ Implemented |
| `AutopilotProject` (database.py:1064) | `cost_total_usd` | ✅ Implemented |

**Acceptance Criteria:**
- [x] All five models have the column
- [x] Column populated by `cost_derivation.py` on every new `CostEntry` write
- [ ] Rollup chain verified: `SUM(cost_entries.cost_usd)` grouped by `task_id` → `Feature.workflow_id` → `Feature.design_id` → `AutopilotDesign.project_id` (**needs integration test**)
- [x] Recomputed on write (not independently maintained)

---

### FR-4: Cost Derivation Module — IMPLEMENTED

**Requirement:** New module `src/core/cost_derivation.py` following the pattern of `src/core/status_derivation.py`.

**Functions (already implemented):**

| Function | Purpose | Status |
|----------|---------|--------|
| `record_cost(db, cost_usd, source, ...)` | Primary entry point: creates CostEntry AND triggers rollup | ✅ |
| `derive_task_cost(db, task_id, write_back=True)` | SUM cost_entries for task | ✅ |
| `derive_workflow_cost(db, workflow_id, write_back=True)` | SUM cost_entries for workflow, rolls up to feature/design/project | ✅ |
| `derive_feature_cost(db, feature_id, write_back=True)` | SUM costs for all workflows in feature | ✅ |
| `derive_design_cost(db, design_id, write_back=True)` | SUM costs for all features in design | ✅ |
| `derive_project_cost(db, project_id, write_back=True)` | SUM costs for all designs in project, checks budget enforcement | ✅ |
| `check_budget_before_new_work(db, project_id)` | Returns True if under budget (safe to proceed) | ✅ |

**Self-healing behavior:** Each function queries `SUM(cost_entries.cost_usd)` and compares against the stored `cost_total_usd`. If they disagree by more than $0.0001, the stored value is corrected. This ensures consistency even if a previous write failed partway through.

**Acceptance Criteria:**
- [x] Self-healing: missed updates never permanently desync displayed totals
- [x] Called on every new CostEntry insertion
- [x] Thread-safe for concurrent writes (WAL mode + SQLAlchemy QueuePool)
- [ ] Unit tests for each derivation function (**needs tests**)
- [ ] Integration test: write CostEntry, verify rollup propagates to project (**needs tests**)

---

### FR-5: Budget Enforcement Schema — IMPLEMENTED

**Requirement:** `cost_limit_usd = Column(Float, nullable=True)` on `AutopilotProject`.

**Implementation (database.py:1066):**
```python
cost_limit_usd = Column(Float, nullable=True)  # None = no limit
```

**Acceptance Criteria:**
- [x] Column exists on AutopilotProject
- [x] Nullable (no limit when None)
- [x] `cost_total_usd` (from FR-3) used for comparison (no redundant field)
- [x] API endpoint `PUT /projects/{project_id}` handles `cost_limit_usd` update (autopilot_api.py:1841-1846)

---

### FR-6: Budget Enforcement Logic — IMPLEMENTED

**Requirement:** When `project.cost_total_usd >= project.cost_limit_usd`:

1. **Pause active workflows** — terminate active agents and mark workflows `paused` with `paused_by = "budget"`
2. **Block new work** — guard at `pick_next_design` and `_run_one_feature`
3. **Idempotent pause** — `_pause_project_workflows` naturally idempotent

**Implementation (`cost_derivation.py:_check_budget_enforcement` + `_pause_project_workflows`):**

```python
def _check_budget_enforcement(db: Session, project: AutopilotProject) -> None:
    if project.cost_limit_usd is None:
        return
    if project.cost_total_usd < project.cost_limit_usd:
        return
    _pause_project_workflows(db, project.id, paused_by="budget")
```

**Critical Gap Fix (from design):** `_pause_project_workflows` filters `Workflow.definition_id.in_(["autopilot", "autopilot-phase0"])` — the original `/autopilot/stop` endpoint only matched `"autopilot"`, missing Phase 0 (Feature Architect) which runs under `"autopilot-phase0"`. This is a real bug the design caught.

**Acceptance Criteria:**
- [x] Pipeline pauses when budget exceeded
- [x] Phase 0 workflows included in pause (`definition_id.in_` filter)
- [ ] No new work starts for over-budget project (**needs `pick_next_design` integration**)
- [x] Concurrent CostEntry writes don't cause redundant pauses (natural idempotency)
- [x] Spend lands at-or-slightly-over limit (cost only knowable after the fact)
- [ ] Integration test: set limit, record cost exceeding it, verify pause (**needs tests**)

---

### FR-7: Generalize `paused_by` Guards — PARTIALLY IMPLEMENTED

**Requirement:** Change self-heal/auto-resume guards from `== "user"` to `is not None`, EXCEPT in `AutopilotService.start()`.

**Current State:** Five locations in `orchestrator.py` still use `== "user"`:

| Location | Line | Function | Status |
|----------|------|----------|--------|
| `orchestrator.py` | 398 | Resume-on-play logic | ✅ Correctly keeps `== "user"` |
| `orchestrator.py` | 3749 | `_try_auto_resume_paused_workflow` | ❌ Needs `is not None` |
| `orchestrator.py` | 5680 | `_create_corrective_task` | ❌ Needs `is not None` |
| `orchestrator.py` | 5864 | Stuck-workflow restart in `attempt_recovery` | ❌ Needs `is not None` |

**Note:** `AutopilotService.start()` at line 398 correctly keeps `== "user"` — clicking play should resume user-paused but NOT budget-paused.

**When limit raised or cleared:** If new limit is null or higher than `cost_total_usd`, clear `paused_by` on that project's `"budget"`-paused workflows. This logic needs to be added to `PUT /projects/{project_id}` handler.

**Acceptance Criteria:**
- [ ] `_try_auto_resume_paused_workflow` uses `is not None` guard
- [ ] `_create_corrective_task` uses `is not None` guard
- [ ] `attempt_recovery` stuck-workflow restart uses `is not None` guard
- [x] `AutopilotService.start()` keeps `== "user"` (correct behavior)
- [ ] Raising limit clears budget pause on `PUT /projects/{project_id}`
- [ ] Test: budget-paused workflow doesn't auto-resume through self-heal paths

---

### FR-8: Pi Extension Collector — NOT YET IMPLEMENTED

**Requirement:** Pi extension (`extensions/hephaestus-cost-tracker.ts`) hooks `turn_end` events to capture `message.usage.cost.total` in real-time.

**Benefits over raw JSONL tailing:**
1. No file-system access needed — extension runs inside pi process
2. Real-time TUI display via `ctx.ui.setStatus()`
3. No checkpoint table needed for pi — POSTs each turn immediately
4. Falls back to JSONL tailing when extension not loaded

**Data source verified:** Pi session files at `~/.pi/agent/sessions/` contain JSONL with confirmed schema:
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

**Acceptance Criteria:**
- [ ] Extension installed at `~/.pi/agent/extensions/hephaestus-cost-tracker/`
- [ ] POSTs each turn's cost to Hephaestus API immediately
- [ ] Reads `session_id` from pi session context
- [ ] Shows running cost in pi TUI via `ctx.ui.setStatus()`
- [ ] Configurable API URL via `HEPHAESTUS_API_URL` env var
- [ ] JSONL tailing fallback works when extension not loaded

---

### FR-9: Pi JSONL Tailing Collector (Fallback) — IMPLEMENTED

**Requirement:** Collector in `cost_collection_service.py` tails pi session JSONL files.

**Implementation:** `PiJsonlCollector` reads `message.usage.cost.total` from assistant turns, advances checkpoint by line count.

**Session file discovery:**
- Directory: `~/.pi/agent/sessions/<sanitized_cwd>/` (slashes → dashes, wrapped in `--`)
- Filename pattern: `*_<session_id>.jsonl`
- Verification: first line's `{"type": "session", "id": "<session-id>"}` matches

**Acceptance Criteria:**
- [x] Collector discovers session file via glob
- [x] Correctly handles shared sessions (SESSION_ROLES)
- [x] No double-counting across agent retries (checkpoint keyed by session_id)
- [x] Collection triggered on task completion (`collect_task_cost` called from `task_completion_service.py`)
- [ ] Security: path traversal prevention verified (**needs security review**)

---

### FR-10: Claude Code Collector — IMPLEMENTED (needs smoke test)

**Requirement:** Token-to-dollar conversion collector for Claude Code sessions.

**Key challenge:** Claude Code transcripts contain only raw tokens, not dollar cost. Requires maintained per-model price table.

**Price table (in `ClaudeCodeCollector.PRICES`):**
| Model | Input ($/M) | Output ($/M) | Cache Write 1h ($/M) | Cache Write 5m ($/M) | Cache Read ($/M) |
|-------|-------------|--------------|----------------------|----------------------|------------------|
| claude-sonnet-4 | 3.00 | 15.00 | 3.75 | 3.00 | 0.30 |
| claude-opus-4 | 15.00 | 75.00 | 18.75 | 15.00 | 1.50 |
| claude-haiku-3.5 | 0.80 | 4.00 | 1.00 | 0.80 | 0.08 |

**Session ID fix:** UUID5 derivation from deterministic inputs (`uuid.uuid5(NAMESPACE, f"{project_id}:{design_slug}:{role}")`) — needed because Claude Code requires valid UUID format for `--session-id`. This fix is noted in the design as landed (`cli_interface.py:393-403`).

**Acceptance Criteria:**
- [x] Price table maintained for Claude models
- [x] Two cache-write tiers handled (`ephemeral_1h` vs `ephemeral_5m`)
- [ ] Session ID correlation verified via UUID5 (**needs smoke test**)
- [ ] Fallback to heuristic if session ID unavailable (**needs implementation**)
- [ ] Price table update process documented (**needs documentation**)

---

### FR-11: OpenCode Collector — IMPLEMENTED (needs smoke test)

**Requirement:** Capture cost from one-shot `opencode run` invocations.

**Key insight:** OpenCode runs one-shot (not persistent tmux). Real dollar cost available. Storage is SQLite at `~/.local/share/opencode/opencode.db`.

**Implementation:** `OpenCodeCollector` reads stdout capture file (JSON format with `cost`, `tokens`, `modelID`).

**Acceptance Criteria:**
- [x] Collector implemented
- [ ] Smoke test `opencode run --format json "..."` to verify payload shape (**needs live test**)
- [x] Cost captured from stdout JSON
- [x] Collection happens once after process exits (no timer)
- [ ] Gate: check if OpenCode is actually used in `config/workflows/autopilot/workflow.yaml` (**needs verification**)

---

### FR-12: OpenRouter Direct Collector — NOT YET WIRED

**Requirement:** Capture cost from backend's own direct OpenRouter calls (~9 call sites in `LangChainLLMClient`).

**Mechanism:** Add `usage: {include: true}` to `extra_body` in `ChatOpenAI` construction. OpenRouter returns non-standard `usage.cost` field.

**Refactor needed:** Add `_invoke_and_record(model, messages, component, task_id)` helper to avoid duplicating extraction logic across 9 call sites:

| Call Site | task_id Available? | Status |
|-----------|-------------------|--------|
| `enrich_task` | No — caller knows but doesn't pass it down | Needs `task_id` param added |
| `resolve_ticket_clarification` | Unknown | Needs inspection |
| `analyze_agent_state` | Via `task_info` dict | Needs verification |
| `analyze_agent_trajectory` | Via `task_info` dict | Needs verification |
| `analyze_system_coherence` | No (system-wide) | Rolls up to workflow/overhead |
| `review_qa_report` | Unknown | Needs inspection |
| `generate_agent_prompt` | Unknown | Needs inspection |
| `generate_embedding` | N/A (not cost-tracked) | Skip |
| Others | Unknown | Grep needed |

**Acceptance Criteria:**
- [ ] `usage.include=true` confirmed working via smoke test
- [ ] `_invoke_and_record` helper wraps all call sites
- [ ] `task_id` threaded into all task-scoped methods
- [ ] Non-task-scoped calls roll up to workflow or "overhead" bucket
- [ ] `response_metadata` parsing confirmed for OpenRouter's non-standard fields

---

### FR-13: Codex Collector Stub — IMPLEMENTED

**Requirement:** Stub collector that logs "unsupported" rather than silently reporting zero cost.

**Implementation:** `CodexStubCollector` returns empty list and logs warning.

**Acceptance Criteria:**
- [x] Stub implemented
- [x] Logs "unsupported" message
- [x] Does not report zero (which would be misleading)

---

### FR-14: UI — Budget Configuration — IMPLEMENTED

**Requirement:** Add `cost_limit_usd` number input to `ProjectSettingsModal.tsx`.

**Backend:** `PUT /projects/{project_id}` handles `cost_limit_usd` update (autopilot_api.py:1841-1846).

**Acceptance Criteria:**
- [x] Backend API accepts `cost_limit_usd` update
- [x] `ProjectUpdate` model extended with `cost_limit_usd`
- [ ] Frontend number input in ProjectSettingsModal (**needs frontend work**)
- [ ] Validation: non-negative, max reasonable value (**partially done** — validator at autopilot_api.py:1546-1552)

---

### FR-15: UI — Cost Display — NOT YET IMPLEMENTED

**Requirement:** Display cost data in multiple UI locations.

**Placements:**
1. **Autopilot design screen:** "$current / $limit" indicator with link to ProjectSettingsModal
2. **Feature cards:** `cost_total_usd` display
3. **Design rows:** `cost_total_usd` display
4. **Project-level summary:** Aggregate cost in autopilot dashboard
5. **Budget-paused status:** "Paused: budget limit reached" instead of generic "Paused"

**Acceptance Criteria:**
- [ ] Design screen shows current spend
- [ ] Link to settings for limit configuration
- [ ] Budget-paused workflows clearly labeled ("Paused: budget limit reached")
- [ ] Feature cards show cost
- [ ] Project summary shows aggregate cost

---

### FR-16: Cost Collection Service — IMPLEMENTED

**Requirement:** Central service orchestrating cost collection from all sources.

**Implementation:** `src/services/cost_collection_service.py` with:
- `CostCollector` ABC
- `PiJsonlCollector`, `ClaudeCodeCollector`, `OpenCodeCollector`, `CodexStubCollector`
- `collect_task_cost(task_id)` entry point
- `_discover_session_file(session_id, cwd)` for pi session discovery
- `_extract_session_id(agent, task)` for session ID extraction
- `_get_agent_cwd(db, agent, task)` for working directory resolution

**Acceptance Criteria:**
- [x] Entry point `collect_task_cost` called from `task_completion_service.py`
- [x] Correct collector selected based on `cli_type`
- [x] Session file discovery with path traversal prevention
- [x] Checkpoint management (create/update `SessionCostCheckpoint`)
- [ ] Integration test: end-to-end collection from task completion to CostEntry (**needs tests**)

---

## 5. Non-Functional Requirements

### NFR-1: Backward Compatibility
- [x] Existing autopilot pipeline continues without cost tracking enabled
- [x] No breaking changes to existing database schema (additive only)
- [x] Budget enforcement is opt-in (disabled when `cost_limit_usd` is None)
- [x] Dead code in `cost_tracker.py` and `openrouter_client.py` left untouched (no breakage)

### NFR-2: Performance
- [x] `CostEntry` writes are < 1ms (SQLite insert)
- [ ] Cost derivation rollup on write path benchmarks within acceptable limits (**needs benchmarking**)
- [x] WAL mode enables concurrent reads during writes
- [x] Up to MAX_PARALLEL_FEATURES (4) concurrent CostEntry writers supported
- [ ] Index coverage verified for common query patterns (**needs EXPLAIN analysis**)

### NFR-3: Reliability
- [x] Append-only ledger is the source of truth (no mutable running totals)
- [x] Self-healing derivation ensures consistency after missed updates
- [x] Budget pause is idempotent (concurrent calls don't cause issues)
- [x] `_pause_project_workflows` matches `status IN ("active","running")` — second call finds nothing

### NFR-4: Data Accuracy
| Source | Accuracy | Mechanism |
|--------|----------|-----------|
| pi (extension) | Exact | `message.usage.cost.total` from OpenRouter response |
| pi (JSONL fallback) | Exact | Same field, read from file |
| Claude Code | Estimated | Token counts × price table (goes stale on repricing) |
| OpenCode | Exact | `cost` field from stdout/DB |
| OpenRouter Direct | Exact | `usage.include=true` response field |

### NFR-5: Security
- [x] Path traversal prevention in `_discover_session_file` (rejects `..`, `~`)
- [x] Resolved path verified to be within expected base directory
- [x] `cost_usd` validation: non-negative, max $1000 per entry (autopilot_api.py:1546-1552)
- [ ] Input sanitization for API endpoints (**needs security review**)

### NFR-6: Maintainability
- [ ] Claude Code price table update process documented
- [ ] New CLI tool collector integration pattern documented
- [x] Codex stub logs "unsupported" (not zero)
- [x] Historical backfill NOT supported (documented non-goal)

### NFR-7: Observability
- [x] `[COST-HEAL]` log prefix for self-heal corrections
- [x] `[BUDGET]` log prefix for budget enforcement actions
- [x] `[COST-COLLECT]` log prefix for collection events
- [ ] Cost summary dashboard in monitoring (**future enhancement**)

---

## 6. Technology Constraints

| Constraint | Detail | Source |
|-----------|--------|--------|
| Language | Python 3.12 | `pyproject.toml` |
| ORM | SQLAlchemy with StaticPool, `expire_on_commit=False` | `database.py` |
| Database | SQLite with WAL mode | Existing infrastructure |
| Migrations | `_migrate_*_column` pattern in `database.py` | Existing pattern |
| Frontend | React 18, TypeScript, Tailwind CSS | `frontend/` |
| No new dependencies | Pure extensions of existing patterns | Design constraint |
| Thread model | Single-threaded orchestrator, thread-safe SQLite via WAL | Architecture |

---

## 7. Integration Points

### 7.1 Existing Code (Modify)

| File | Change Required | Status |
|------|----------------|--------|
| `src/core/database.py` | Add CostEntry, SessionCostCheckpoint tables; add cost_total_usd columns; add cost_limit_usd; migrations | ✅ Done |
| `src/core/cost_derivation.py` | Self-healing cost rollup module | ✅ Done |
| `src/services/cost_collection_service.py` | Per-CLI collectors, session file discovery | ✅ Done |
| `src/autopilot/orchestrator.py` | Extract `_pause_project_workflows`; add budget checks in `pick_next_design`/`_run_one_feature`; generalize `paused_by` guards | ⚠️ Partial (guards not generalized) |
| `src/mcp/autopilot_api.py` | Extend `PUT /projects/{id}` for cost_limit_usd; add cost endpoints | ⚠️ Partial (backend done, cost query endpoints needed) |
| `src/interfaces/langchain_llm_client.py` | Add `_invoke_and_record` helper; wire all 9 call sites; add `usage.include=true` | ❌ Not done |
| `src/agents/manager.py` | Propagate cost data from CLI agent sessions | ⚠️ Partial |
| `src/services/task_completion_service.py` | Trigger cost collection on task done | ✅ Done |
| `src/interfaces/cli_interface.py` | Add `--session-id` to ClaudeCodeAgent; UUID5 derivation | ✅ Done (per design) |

### 7.2 New Files

| File | Purpose | Status |
|------|---------|--------|
| `src/core/cost_derivation.py` | Self-healing cost rollup module | ✅ Done |
| `src/services/cost_collection_service.py` | Per-CLI transcript collectors | ✅ Done |
| `extensions/hephaestus-cost-tracker.ts` | Pi extension for real-time cost capture | ❌ Not created |
| `tests/test_cost_derivation.py` | Unit tests for derivation functions | ❌ Not created |
| `tests/test_cost_collection.py` | Integration tests for collection | ❌ Not created |

### 7.3 External Dependencies

| Dependency | Purpose | Risk |
|-----------|---------|------|
| Pi session JSONL format | Cost data source for pi collector | Low — format verified |
| Claude Code session JSONL format | Token data for Claude Code collector | Medium — format verified but price table may go stale |
| OpenRouter `usage.include=true` | Cost data for direct calls | Medium — needs smoke test confirmation |
| OpenCode `--format json` | Cost data for OpenCode collector | Medium — needs smoke test |

---

## 8. Component Dependencies Map

```
                    ┌─────────────────────────┐
                    │   task_completion_service │
                    │   (triggers collection)   │
                    └───────────┬─────────────┘
                                │
                                ▼
                    ┌─────────────────────────┐
                    │  cost_collection_service  │
                    │  collect_task_cost()      │
                    └───┬───┬───┬───┬─────────┘
                        │   │   │   │
           ┌────────────┘   │   │   └────────────┐
           ▼                ▼   ▼                ▼
    ┌──────────┐   ┌──────────┐ ┌──────────┐ ┌──────────┐
    │ PiJsonl  │   │ Claude   │ │ OpenCode │ │ Codex    │
    │ Collector│   │ Code     │ │ Collector│ │ Stub     │
    └────┬─────┘   │ Collector│ └────┬─────┘ └──────────┘
         │         └────┬─────┘      │
         │              │            │
         └──────┬───────┴────────────┘
                │
                ▼
    ┌─────────────────────────┐
    │  cost_derivation.py      │
    │  record_cost()           │──► Creates CostEntry
    │  derive_task_cost()      │──► Updates Task.cost_total_usd
    │  derive_workflow_cost()  │──► Updates Workflow.cost_total_usd
    │  derive_feature_cost()   │──► Updates Feature.cost_total_usd
    │  derive_design_cost()    │──► Updates AutopilotDesign.cost_total_usd
    │  derive_project_cost()   │──► Updates AutopilotProject.cost_total_usd
    └───────────┬─────────────┘
                │
                ▼
    ┌─────────────────────────┐
    │  _check_budget_enforcement│
    │  _pause_project_workflows │
    └───────────┬─────────────┘
                │
                ▼
    ┌─────────────────────────┐
    │  orchestrator.py         │
    │  pick_next_design()      │◄── check_budget_before_new_work()
    │  _run_one_feature()      │◄── check_budget_before_new_work()
    └─────────────────────────┘
```

---

## 9. Critical Design Decisions

### D-1: Append-Only Ledger vs Mutable Totals
**Decision:** Append-only `cost_entries` table as source of truth; denormalized `cost_total_usd` columns are derived, not maintained independently.
**Rationale:** Matches existing self-healing pattern in `status_derivation.py`. A missed update never permanently desyncs the displayed total from the ledger.
**Status:** ✅ Implemented

### D-2: Checkpoint by Session ID vs Agent ID
**Decision:** `SessionCostCheckpoint` keyed by `session_id`, NOT `Agent.id`.
**Rationale:** When an agent dies and retries, the new agent gets the same session ID and resumes the same file. A checkpoint on the agent row would double-count.
**Status:** ✅ Implemented

### D-3: Collection on Task Completion vs Timer
**Decision:** Collect cost on task completion (`update_task_status` handler), not on a timer.
**Rationale:** Session activity is fully written to disk by the time done lands. No torn-read risk. Avoids separate polling loop.
**Status:** ✅ Implemented

### D-4: Pi Extension vs Raw JSONL Tailing
**Decision:** Pi extension preferred over raw JSONL tailing for pi sessions.
**Rationale:** No file-system access needed. Real-time TUI display. No checkpoint table needed for pi. JSONL tailing remains as fallback.
**Status:** ❌ Extension not yet created; JSONL tailing fallback is implemented

### D-5: Single Shared Pause Function
**Decision:** Extract `_pause_project_workflows(project_id, paused_by)` from `/autopilot/stop` route handler.
**Rationale:** Current endpoint misses Phase 0 (only matches `"autopilot"`, not `"autopilot-phase0"`). Shared function fixes both.
**Status:** ✅ Implemented in `cost_derivation.py`

### D-6: `paused_by` Generalization
**Decision:** Change guards from `== "user"` to `is not None`, EXCEPT in `AutopilotService.start()`.
**Rationale:** Any non-null `paused_by` means something deliberately paused this. `start()` keeps `== "user"` because clicking play should resume user-paused but NOT budget-paused.
**Status:** ❌ Not yet generalized (5 locations still use `== "user"`)

### D-7: Price Table for Claude Code vs Proxy
**Decision:** Maintain local price table for Claude Code token→dollar conversion.
**Rationale:** Claude Code transcripts have no dollar cost. LiteLLM proxy not in use. Price table is the only viable option.
**Status:** ✅ Implemented (goes stale on Anthropic repricing)

---

## 10. Open Questions

| # | Question | Status | Recommendation |
|---|----------|--------|----------------|
| Q1 | Should standalone tasks (no session_id) be forced to always pass a session ID? | Unresolved | Yes — small change to task creation, eliminates permanent gap |
| Q2 | Does `usage.include=true` survive LangChain's response parsing into `response_metadata`? | Needs smoke test | Test before implementing FR-12 |
| Q3 | Does OpenCode's `-s` flag accept caller-chosen new session IDs? | Needs live test | Test before implementing session correlation |
| Q4 | Is OpenCode actually used in any live phase? | Needs verification | Check `config/workflows/autopilot/workflow.yaml` |
| Q5 | Should we track cost for non-LLM operations (tool calls, etc.)? | Deferred | Not in scope for this feature |
| Q6 | Should cost be rounded or stored with full precision? | Resolved | Store full precision (Float column) |
| Q7 | Do we need cost budgets per design (not just per project)? | Deferred | Per-project only for now |

---

## 11. Acceptance Criteria Summary

| ID | Criterion | Verification | Status |
|----|-----------|--------------|--------|
| AC-1 | CostEntry table created | `from src.core.database import CostEntry` succeeds | ✅ |
| AC-2 | SessionCostCheckpoint table created | Table exists in DB | ✅ |
| AC-3 | cost_total_usd on Task/Feature/Workflow/Design/Project | All five models have column | ✅ |
| AC-4 | cost_limit_usd on AutopilotProject | Column exists, nullable | ✅ |
| AC-5 | Pi JSONL collector captures real cost | CostEntry rows populated after pi agent task | ⚠️ Needs integration test |
| AC-6 | Cost derivation self-heals | Missing updates recovered on next write | ⚠️ Needs unit test |
| AC-7 | Budget pauses pipeline | Workflows paused when limit exceeded | ⚠️ Needs integration test |
| AC-8 | Phase 0 included in budget pause | `_pause_project_workflows` matches both definition_ids | ✅ |
| AC-9 | Budget-paused doesn't auto-resume | Self-heal guards use `is not None` | ❌ Guards not generalized |
| AC-10 | Play button doesn't clear budget pause | `start()` keeps `== "user"` filter | ✅ |
| AC-11 | Raising limit clears budget pause | `PUT /projects/{id}` clears `"budget"`-paused | ❌ Not implemented |
| AC-12 | UI shows cost data | Design screen displays spend | ❌ Frontend not done |
| AC-13 | Budget config works | ProjectSettingsModal has limit input | ❌ Frontend not done |
| AC-14 | Existing tests pass | All tests green | ⚠️ Needs verification |
| AC-15 | No new dependencies | Pure SQLAlchemy/stdlib | ✅ |
| AC-16 | Pi extension provides real-time TUI cost | Extension hooks `turn_end` | ❌ Not created |
| AC-17 | OpenRouter direct collector wired | `_invoke_and_record` across 9 call sites | ❌ Not done |
| AC-18 | `paused_by` guards generalized | 3 locations changed from `== "user"` to `is not None` | ❌ Not done |

---

## 12. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Claude Code price table goes stale | High | Medium | Document update process; version-check mechanism; fallback to zero with warning |
| Concurrent CostEntry writes cause contention | Low | Medium | WAL mode handles this; MAX_PARALLEL_FEATURES = 4 |
| Pi extension not loaded | Medium | Low | JSONL tailing fallback already works |
| Budget enforcement misses edge case | Medium | High | Comprehensive testing of pause/resume paths; generalize `paused_by` guards |
| Historical data unavailable | N/A | Low | Documented non-goal; rollups start from deploy |
| `usage.include=true` doesn't survive LangChain parsing | Medium | Medium | Smoke test before implementing; fallback to token-based estimation |
| Standalone tasks without session_id have no cost attribution | Low | Low | Force session_id on all tasks (small change) |

---

## 13. Non-Goals (Explicitly Deferred)

- **Real-time streaming cost display mid-task for non-pi CLIs.** Pi extension provides real-time cost. Claude Code and OpenCode collection at task completion.
- **Codex collector implementation.** Stubbed only; needs CLI installed to inspect transcript format.
- **Historical backfill.** No cost data exists for tasks that already ran before this lands; rollups start from zero at deploy time.
- **Per-design budget limits.** Per-project only for now; per-design can be added later by extending the rollup chain.
- **Cost tracking for non-LLM operations.** Tool calls, file I/O, etc. are not tracked.

---

## 14. Implementation Phases (from Design)

### Phase 1: Schema ✅
- `cost_entries` and `session_cost_checkpoints` tables
- `cost_total_usd` columns on Task/Feature/AutopilotDesign/AutopilotProject
- `cost_limit_usd` on AutopilotProject
- Migration following `_migrate_*_column` pattern

### Phase 2: Pi Collector ✅
- JSONL tailing collector + checkpoint mechanism
- `cost_derivation.py` rollup
- Wire into task completion handler
- Verify against real running pipeline

### Phase 3: Budget Enforcement ⚠️ Partial
- ✅ `_pause_project_workflows` extraction
- ✅ Enforcement check in `cost_derivation.py`
- ❌ `is not None` generalization of `paused_by` guards
- ❌ Budget checks in `pick_next_design`/`_run_one_feature`
- ❌ Limit raise clears budget pause

### Phase 4: Claude Code Collector ✅
- UUID5 session-ID fix
- Price-table-based collector
- Needs smoke test verification

### Phase 5: OpenRouter Direct ❌
- Confirm `usage.include=true` works
- Wire `_invoke_and_record` across all 9 call sites
- Thread `task_id` into methods

### Phase 6: OpenCode Collector ✅
- Gate on actual usage in workflow.yaml
- Implement stdout capture

### Phase 7: UI ❌
- Budget config input
- Cost display on design screen
- Budget-paused status label

### Phase 8: Codex Collector ✅ Stub
- Stub implementation (logs "unsupported")
- Full implementation when CLI available

### Phase 9: Pi Extension ❌
- Create `extensions/hephaestus-cost-tracker.ts`
- Hook `turn_end` events
- POST to Hephaestus API
- Real-time TUI display

### Phase 10: Tests ❌
- Unit tests for `cost_derivation.py`
- Integration tests for collection pipeline
- Budget enforcement tests
- `paused_by` generalization tests

---

## 15. Appendix: File Reference

### Core Implementation Files
| File | Lines | Purpose |
|------|-------|---------|
| `src/core/cost_derivation.py` | 287 | Self-healing cost rollup (record, derive, enforce) |
| `src/core/database.py` | 1270+ | CostEntry, SessionCostCheckpoint models; migrations |
| `src/services/cost_collection_service.py` | 604 | Per-CLI collectors; session discovery; collection orchestration |
| `src/mcp/autopilot_api.py` | 1850+ | API endpoints for cost_limit_usd; cost query models |

### Reference Files
| File | Relevance |
|------|-----------|
| `src/core/status_derivation.py` | Pattern model for self-healing derivation |
| `src/interfaces/cli_interface.py` | CLI agent launch commands; session ID generation |
| `src/agents/manager.py` | Agent lifecycle; session management |
| `src/services/task_completion_service.py` | Triggers `collect_task_cost` on task done |
| `src/autopilot/orchestrator.py` | `paused_by` guards; `pick_next_design`; `_run_one_feature` |

### Design Documents
| File | Content |
|------|---------|
| `.hephaestus/design.md` | Primary design (same as `docs/COST_TRACKING_DESIGN.md`) |
| `design_docs/per_task_cost_tracking.md` | Original per-task cost tracking design |
| `design_docs/budget_tracking_approval_system.md` | Broader budget/approval system design |
