# Cost Derivation Engine — Technical Architecture

**Feature ID:** cost-derivation-engine  
**Version:** 1.0  
**Date:** 2026-07-21  
**Author:** Architecture Design Agent (Phase 3)  
**Status:** Implementation-Ready  
**Branch:** `feature/des-91c8-cost-derivation`

---

## 1. Architecture Overview

### 1.1 System Context

The Cost Derivation Engine is a cross-cutting concern that tracks LLM API spend across all agent execution channels in HephaestusNG. It follows the existing self-healing derivation pattern established by `status_derivation.py` — an append-only ledger (`cost_entries`) serves as the single source of truth, with denormalized rollup columns (`cost_total_usd`) derived on write and self-healed on read.

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              HephaestusNG System                                 │
│                                                                                  │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐   │
│  │ pi CLI       │    │ Claude Code  │    │ OpenCode     │    │ Codex        │   │
│  │ (persistent) │    │ (persistent) │    │ (one-shot)   │    │ (stub)       │   │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘    └──────┬───────┘   │
│         │                   │                   │                   │            │
│         ▼                   ▼                   ▼                   ▼            │
│  ┌─────────────────────────────────────────────────────────────────────────┐     │
│  │                    Cost Collection Service                               │     │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐ │     │
│  │  │PiJsonl   │ │ClaudeCode│ │OpenCode  │ │CodexStub │ │OpenRouter    │ │     │
│  │  │Collector │ │Collector │ │Collector │ │Collector │ │Direct       │ │     │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────────┘ │     │
│  └──────────────────────────────┬──────────────────────────────────────────┘     │
│                                 │                                                │
│                                 ▼                                                │
│  ┌─────────────────────────────────────────────────────────────────────────┐     │
│  │                    Cost Derivation Module                                │     │
│  │  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐ │     │
│  │  │record_cost() │  │derive_*()    │  │check_budget()│  │_pause_*()   │ │     │
│  │  │(entry point) │  │(self-healing)│  │(enforcement) │  │(idempotent) │ │     │
│  │  └─────────────┘  └──────────────┘  └──────────────┘  └─────────────┘ │     │
│  └──────────────────────────────┬──────────────────────────────────────────┘     │
│                                 │                                                │
│                                 ▼                                                │
│  ┌─────────────────────────────────────────────────────────────────────────┐     │
│  │                         SQLite Database                                   │     │
│  │  ┌──────────────┐  ┌────────────────────┐  ┌───────────────────────┐   │     │
│  │  │cost_entries   │  │session_cost_       │  │Task/Feature/Workflow/ │   │     │
│  │  │(append-only)  │  │checkpoints         │  │Design/Project         │   │     │
│  │  │              │  │(progress tracking)  │  │(cost_total_usd cols)  │   │     │
│  │  └──────────────┘  └────────────────────┘  └───────────────────────┘   │     │
│  └─────────────────────────────────────────────────────────────────────────┘     │
│                                                                                  │
│  ┌─────────────────────────────────────────────────────────────────────────┐     │
│  │                    API Layer (autopilot_api.py)                          │     │
│  │  GET /projects/{id}/costs  │  PUT /projects/{id} (cost_limit_usd)       │     │
│  └──────────────────────────────┬──────────────────────────────────────────┘     │
│                                 │                                                │
│                                 ▼                                                │
│  ┌─────────────────────────────────────────────────────────────────────────┐     │
│  │                    Frontend (React/TypeScript)                            │     │
│  │  ProjectSettingsModal │ DesignScreen │ FeatureCards │ CostDisplay        │     │
│  └─────────────────────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Core Design Principles

| Principle | Implementation |
|-----------|---------------|
| **Append-only ledger** | `cost_entries` is the single source of truth; aggregates are derived, never independently maintained |
| **Self-healing derivation** | Every `derive_*()` function recomputes from the ledger and corrects mismatches > $0.0001 |
| **Collection on completion** | Cost collected when task completes (not on timer) — eliminates torn-read risk |
| **Checkpoint by session_id** | `SessionCostCheckpoint` keyed by deterministic `session_id`, not `Agent.id` — survives agent retries |
| **Idempotent budget pause** | `_pause_project_workflows` naturally idempotent — second call finds nothing to pause |
| **No new dependencies** | Pure SQLAlchemy + stdlib; extends existing patterns only |

---

## 2. Component Architecture

### 2.1 Cost Entry Layer

#### 2.1.1 CostEntry Table (IMPLEMENTED)

**Location:** `src/core/database.py:1227`

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

#### 2.1.2 SessionCostCheckpoint Table (IMPLEMENTED)

**Location:** `src/core/database.py:1268`

```python
class SessionCostCheckpoint(Base):
    __tablename__ = "session_cost_checkpoints"
    session_id = Column(String, primary_key=True)
    lines_processed = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

**Key Design Decision:** Checkpoint keyed by `session_id`, NOT `Agent.id`. When an agent dies mid-phase and retries, the new agent gets the same session ID and resumes the same file. A checkpoint on `Agent.id` would start at 0 and double-count.

#### 2.1.3 Denormalized Rollup Columns (IMPLEMENTED)

| Model | Column | Location |
|-------|--------|----------|
| `Task` | `cost_total_usd = Column(Float, default=0.0)` | `database.py:279` |
| `Feature` | `cost_total_usd = Column(Float, default=0.0)` | `database.py:452` |
| `Workflow` | `cost_total_usd = Column(Float, default=0.0)` | `database.py:1143` |
| `AutopilotDesign` | `cost_total_usd = Column(Float, default=0.0)` | `database.py:1104` |
| `AutopilotProject` | `cost_total_usd = Column(Float, default=0.0)` | `database.py:1064` |
| `AutopilotProject` | `cost_limit_usd = Column(Float, nullable=True)` | `database.py:1066` |

---

### 2.2 Cost Derivation Module (IMPLEMENTED)

**Location:** `src/core/cost_derivation.py`

#### 2.2.1 Public API

| Function | Purpose | Self-Healing |
|----------|---------|--------------|
| `record_cost(db, cost_usd, source, ...)` | Primary entry point: creates CostEntry AND triggers rollup | N/A (creates entry) |
| `derive_task_cost(db, task_id, write_back=True)` | SUM cost_entries for task | ✅ Corrects mismatches > $0.0001 |
| `derive_workflow_cost(db, workflow_id, write_back=True)` | SUM cost_entries for workflow, rolls up | ✅ Propagates to feature/design/project |
| `derive_feature_cost(db, feature_id, write_back=True)` | SUM costs for all workflows in feature | ✅ |
| `derive_design_cost(db, design_id, write_back=True)` | SUM costs for all features in design | ✅ |
| `derive_project_cost(db, project_id, write_back=True)` | SUM costs for all designs in project + budget check | ✅ |
| `check_budget_before_new_work(db, project_id)` | Returns True if under budget | N/A (read-only) |

#### 2.2.2 Internal Functions

| Function | Purpose | Idempotent |
|----------|---------|------------|
| `_check_budget_enforcement(db, project)` | Check if project exceeds limit, pause if needed | ✅ |
| `_pause_project_workflows(db, project_id, paused_by)` | Pause active workflows + terminate agents | ✅ (finds nothing if already paused) |

#### 2.2.3 Self-Healing Algorithm

```
For each derive_*() function:
    1. Query SUM(cost_entries.cost_usd) WHERE <entity_filter>
    2. Compare with entity.cost_total_usd
    3. If |total - stored| > 0.0001:
        - Log [COST-HEAL] correction
        - Update entity.cost_total_usd = total
    4. Return total
```

This ensures consistency even if a previous write failed partway through the rollup chain.

#### 2.2.4 Rollup Chain

```
CostEntry (task_id=X)
    └─► derive_task_cost(X) ─► Task.cost_total_usd
        └─► derive_workflow_cost(W) ─► Workflow.cost_total_usd
            └─► derive_feature_cost(F) ─► Feature.cost_total_usd
                └─► derive_design_cost(D) ─► AutopilotDesign.cost_total_usd
                    └─► derive_project_cost(P) ─► AutopilotProject.cost_total_usd
                        └─► _check_budget_enforcement(P)
                            └─► _pause_project_workflows(P) [if over budget]
```

---

### 2.3 Cost Collection Service (IMPLEMENTED)

**Location:** `src/services/cost_collection_service.py`

#### 2.3.1 Collector Architecture

```python
class CostCollector(ABC):
    @abstractmethod
    def collect(self, session_id, task_id, workflow_id, agent_id, session_file, checkpoint)
        -> Tuple[List[dict], int]:
        """Return new cost entries since checkpoint, and new checkpoint."""
```

#### 2.3.2 Collector Implementations

| Collector | Source | Data Format | Cost Accuracy |
|-----------|--------|-------------|---------------|
| `PiJsonlCollector` | `~/.pi/agent/sessions/<cwd>/*_<sid>.jsonl` | `message.usage.cost.total` | Exact (OpenRouter response) |
| `ClaudeCodeCollector` | `~/.claude/projects/<cwd>/*_<sid>.jsonl` | Token counts × price table | Estimated (price table) |
| `OpenCodeCollector` | Stdout capture file (JSON) | `cost` field | Exact |
| `CodexStubCollector` | N/A | N/A | Unsupported (logs warning) |

#### 2.3.3 Session File Discovery

**Pi Sessions:**
- Directory: `~/.pi/agent/sessions/<sanitized_cwd>/` (slashes → dashes, wrapped in `--`)
- Filename: `*_<session_id>.jsonl`
- Verification: first line's `{"type": "session", "id": "<session-id>"}` matches

**Claude Code Sessions:**
- Directory: `~/.claude/projects/<sanitized_cwd>/`
- Filename: `*_<session_id>.jsonl`

**Security:** Path traversal prevention in `_discover_session_file()`:
- Rejects paths containing `..` or `~`
- Verifies resolved path is within expected base directory

#### 2.3.4 Claude Code Price Table

| Model | Input ($/M) | Output ($/M) | Cache Write 1h ($/M) | Cache Write 5m ($/M) | Cache Read ($/M) |
|-------|-------------|--------------|----------------------|----------------------|------------------|
| claude-sonnet-4 | 3.00 | 15.00 | 3.75 | 3.00 | 0.30 |
| claude-opus-4 | 15.00 | 75.00 | 18.75 | 15.00 | 1.50 |
| claude-haiku-3.5 | 0.80 | 4.00 | 1.00 | 0.80 | 0.08 |

**Maintenance:** Update `ClaudeCodeCollector.PRICES` dict when Anthropic reprices. No automated mechanism — manual update required.

#### 2.3.5 Entry Point

```python
def collect_task_cost(task_id: str) -> None:
    """Called from task_completion_service when a task completes."""
```

**Flow:**
1. Look up Task → Agent → session_id
2. Get or create `SessionCostCheckpoint`
3. Discover session file based on `cli_type`
4. Select appropriate collector
5. Call `collector.collect()` to get new entries since checkpoint
6. For each entry, call `record_cost()` (triggers rollup)
7. Update checkpoint
8. Commit transaction

---

### 2.4 OpenRouter Direct Collection (IMPLEMENTED)

**Location:** `src/interfaces/langchain_llm_client.py`

#### 2.4.1 Current State

- ✅ `_invoke_and_record()` method implemented at line 323
- ✅ `usage.include=true` added to `extra_body` at line 243
- ✅ Cost extraction from `response_metadata.token_usage.cost.total`
- ✅ All major call sites routed through `_invoke_and_record()`: `classify_complexity`, `enrich_task`, `resolve_ticket_clarification`, `analyze_agent_state`, `analyze_agent_trajectory` (guardian), conductor analysis, conductor review_qa
- ✅ `task_id` threaded into task-scoped methods

#### 2.4.2 Call Site Inventory

| Call Site | File:Line | task_id Available? | Status |
|-----------|-----------|-------------------|--------|
| `classify_complexity` | langchain_llm_client.py:409 | No (design-level) | ✅ Routed |
| `enrich_task` | langchain_llm_client.py:466 | Yes (passed as param) | ✅ Routed |
| `resolve_ticket_clarification` | langchain_llm_client.py:530 | No (ticket-scoped) | ✅ Routed |
| `analyze_agent_state` | langchain_llm_client.py:592 | Via `task_info` dict | ✅ Routed |
| `analyze_agent_trajectory` | langchain_llm_client.py:691 | Via `task_info` dict | ✅ Routed (guardian) |
| `analyze_system_coherence` | langchain_llm_client.py:750 | No (system-wide) | ✅ Routed (conductor) |
| `review_qa_report` | langchain_llm_client.py:842 | Yes (passed as param) | ✅ Routed (conductor) |
| `generate_embedding` | langchain_llm_client.py | N/A | ⏭️ Skip (not cost-tracked) |

---

### 2.5 Budget Enforcement (IMPLEMENTED)

#### 2.5.1 Implemented

- ✅ `cost_limit_usd` column on `AutopilotProject`
- ✅ `_check_budget_enforcement()` in `cost_derivation.py`
- ✅ `_pause_project_workflows()` includes Phase 0 (`definition_id.in_(DESIGN_WORKFLOW_DEFINITION_IDS)`)
- ✅ `check_budget_before_new_work()` guard function
- ✅ `PUT /projects/{id}` handles `cost_limit_usd` update AND clears budget pause when limit raised
- ✅ `paused_by` guards generalized — all self-heal paths use `is not None`
- ✅ Budget checks wired into `pick_next_design()` (line 1931) and `_run_one_feature()` (line 6381)

#### 2.5.2 `paused_by` Guard Locations

| Location | Line | Current Check | Status |
|----------|------|---------------|--------|
| `_try_auto_resume_paused_workflow` | 3531 | `is not None` | ✅ Generalized |
| `_create_corrective_task` | 5218 | `is not None` | ✅ Generalized |
| `attempt_recovery` (stuck restart) | 5384 | `is not None` | ✅ Generalized |
| `AutopilotService.start()` | 395 | `== "user"` | ✅ Correctly kept — play button resumes user-paused, not budget-paused |

---

### 2.6 Pi Extension (IMPLEMENTED)

**Location:** `extensions/hephaestus-cost-tracker/src/index.ts`

The pi extension is implemented and hooks `turn_end` events to capture `message.usage.cost.total` in real-time. It POSTs each turn's cost to the Hephaestus API and displays running cost in the pi TUI via `ctx.ui.setStatus()`.

#### 2.6.1 Current Implementation

- ✅ Hooks `turn_end` events in pi process
- ✅ Extracts `message.usage.cost.total` from turn data
- ✅ POSTs to Hephaestus API (`POST /api/autopilot/cost-entries`)
- ✅ Shows running cost in TUI via `ctx.ui.setStatus()`
- ✅ Configurable API URL via `HEPHAESTUS_API_URL` env var (default: `http://localhost:8300`)
- ✅ Fire-and-forget POST with graceful error handling (never blocks pi)
- ✅ Reads `agent_id`, `task_id`, `workflow_id` from environment variables

#### 2.6.2 Benefits over JSONL Tailing

| Aspect | Pi Extension (✅ Implemented) | JSONL Tailing (Fallback) |
|--------|------------------------------|--------------------------|
| File-system access | Not needed | Required |
| Real-time display | Yes (TUI) | No (on completion) |
| Checkpoint table | Not needed | Required |
| Latency | Immediate | On task completion |

#### 2.6.3 Fallback Behavior

When extension not loaded:
- JSONL tailing collector activates on task completion
- No real-time TUI display
- Same data accuracy, delayed timing
- Extension installed globally at `~/.pi/agent/extensions/hephaestus-cost-tracker/` by `scripts/install.sh` when pi is detected

---

### 2.7 Frontend (NOT IMPLEMENTED)

#### 2.7.1 Required Components

| Component | Location | Purpose |
|-----------|----------|---------|
| `CostDisplay` | Design screen | Show `$current / $limit` with link to settings |
| `FeatureCostBadge` | Feature cards | Display `cost_total_usd` |
| `DesignCostRow` | Design list | Display `cost_total_usd` per design |
| `ProjectCostSummary` | Dashboard | Aggregate project cost |
| `BudgetPausedLabel` | Workflow status | "Paused: budget limit reached" instead of generic "Paused" |
| `BudgetConfigInput` | ProjectSettingsModal | Number input for `cost_limit_usd` |

#### 2.7.2 API Endpoints (All Implemented)

| Endpoint | Method | Purpose | Status |
|----------|--------|---------|--------|
| `/projects/{id}` | PUT | Update `cost_limit_usd` + clear budget pause | ✅ Implemented |
| `/cost-entries` | POST | Create cost entry (pi extension endpoint) | ✅ Implemented |
| `/tasks/{id}/costs` | GET | Get task cost breakdown with entries | ✅ Implemented |
| `/workflows/{id}/costs` | GET | Get workflow cost breakdown | ✅ Implemented |
| `/features/{id}/costs` | GET | Get feature cost breakdown | ✅ Implemented |
| `/designs/{id}/costs` | GET | Get design cost breakdown | ✅ Implemented |
| `/projects/{id}/costs` | GET | Get project cost breakdown | ✅ Implemented |

---

## 3. Data Flow

### 3.1 Cost Recording Flow

```
Task Completion
    │
    ▼
task_completion_service.py::update_task_status("done")
    │
    ▼
collect_task_cost(task_id)
    │
    ├─► Look up Task → Agent → session_id
    ├─► Get/create SessionCostCheckpoint
    ├─► Discover session file (by cli_type)
    ├─► Select collector (pi/claude_code/opencode/codex)
    ├─► collector.collect(session_file, checkpoint)
    │       │
    │       ├─► Parse JSONL lines since checkpoint
    │       ├─► Extract cost/tokens from each turn
    │       └─► Return (entries[], new_checkpoint)
    │
    ├─► For each entry:
    │       └─► record_cost(db, cost_usd, source, task_id, ...)
    │               │
    │               ├─► INSERT INTO cost_entries
    │               ├─► derive_task_cost(task_id)
    │               └─► derive_workflow_cost(workflow_id)
    │                       │
    │                       ├─► derive_feature_cost(feature_id)
    │                       ├─► derive_design_cost(design_id)
    │                       └─► derive_project_cost(project_id)
    │                               │
    │                               └─► _check_budget_enforcement()
    │                                       │
    │                                       └─► _pause_project_workflows() [if over budget]
    │
    ├─► Update SessionCostCheckpoint
    └─► COMMIT
```

### 3.2 Budget Enforcement Flow

```
derive_project_cost() ─► _check_budget_enforcement()
    │
    ├─► project.cost_limit_usd is None? ─► Return (no limit)
    ├─► project.cost_total_usd < limit? ─► Return (under budget)
    └─► Over budget:
            │
            ▼
        _pause_project_workflows(project_id, "budget")
            │
            ├─► Query active workflows (definition_id in [autopilot, autopilot-phase0])
            ├─► Set status="paused", paused_by="budget", status_reason="Budget limit reached"
            ├─► Terminate active agents on paused workflows
            └─► Return (idempotent — second call finds nothing)
```

### 3.3 Budget Resume Flow

```
PUT /projects/{id} with new cost_limit_usd
    │
    ├─► Update project.cost_limit_usd
    ├─► If new limit is None OR new limit > project.cost_total_usd:
    │       │
    │       ▼
    │   Clear budget pause:
    │       UPDATE workflows SET status="active", paused_by=NULL
    │       WHERE project_id=X AND paused_by="budget"
    │
    └─► COMMIT
```

---

## 4. Task Breakdown

### Task Dependency Graph

```
T1: Generalize paused_by guards ──────────────────────────────────────┐
T2: Wire budget checks into orchestrator ─────────────────────────────┤
T3: Limit raise clears budget pause ──────────────────────────────────┤
                                                                       │
T4: Wire all LangChainLLMClient call sites ───────────────────────────┤
T5: Thread task_id into LLM methods ──────────────────────────────────┤
                                                                       │
T6: Create API cost query endpoints ──────────────────────────────────┤
T7: Create frontend cost components ──────────────────────────────────┤
T8: Create frontend budget config ────────────────────────────────────┤
                                                                       │
T9: Create Pi extension ──────────────────────────────────────────────┤
T10: Create unit tests for cost_derivation ───────────────────────────┤
T11: Create integration tests for collection ─────────────────────────┤
T12: Create integration tests for budget enforcement ─────────────────┘
```

### Detailed Task Specifications

---

#### **T1: Generalize `paused_by` Guards**

**Priority:** P0 (Critical)  
**Effort:** Small (1-2 hours)  
**Blocks:** Budget enforcement correctness  
**Blocked By:** None  

**Description:** Change three locations in `orchestrator.py` from `== "user"` to `is not None` to prevent budget-paused workflows from auto-resuming through self-heal paths.

**Files to Modify:**
- `src/autopilot/orchestrator.py`

**Changes:**

| Line | Current | Required |
|------|---------|----------|
| 3749 | `if wf.paused_by == "user":` | `if wf.paused_by is not None:` |
| 5680 | `if wf.paused_by == "user":` | `if wf.paused_by is not None:` |
| 5864 | `if wf.status == "paused" and wf.paused_by == "user":` | `if wf.status == "paused" and wf.paused_by is not None:` |

**DO NOT CHANGE:**
- Line 398: `Workflow.paused_by == "user"` in `AutopilotService.start()` — correctly keeps `== "user"` because play button should resume user-paused but NOT budget-paused.

**Acceptance Criteria:**
- [ ] `_try_auto_resume_paused_workflow` uses `is not None` guard
- [ ] `_create_corrective_task` uses `is not None` guard
- [ ] `attempt_recovery` stuck-workflow restart uses `is not None` guard
- [ ] `AutopilotService.start()` keeps `== "user"` (verified unchanged)
- [ ] Test: budget-paused workflow doesn't auto-resume through self-heal paths

**Test Plan:**
```python
def test_budget_paused_workflow_not_auto_resumed(db_session, sample_workflow):
    """Budget-paused workflows should not auto-resume."""
    sample_workflow.status = "paused"
    sample_workflow.paused_by = "budget"
    db_session.commit()
    
    # Simulate auto-resume attempt
    _try_auto_resume_paused_workflow(db_session, sample_workflow)
    
    assert sample_workflow.status == "paused"
    assert sample_workflow.paused_by == "budget"
```

---

#### **T2: Wire Budget Checks into Orchestrator**

**Priority:** P0 (Critical)  
**Effort:** Medium (2-4 hours)  
**Blocks:** Budget enforcement on new work  
**Blocked By:** None  

**Description:** Add `check_budget_before_new_work()` guards to `pick_next_design()` and `_run_one_feature()` in `orchestrator.py`.

**Files to Modify:**
- `src/autopilot/orchestrator.py`

**Changes:**

1. **In `pick_next_design()`:** Before selecting a design for processing, check if its project is over budget:
```python
from src.core.cost_derivation import check_budget_before_new_work

# In pick_next_design(), before processing a candidate design:
if not check_budget_before_new_work(db, design.project_id):
    logger.info(f"[BUDGET] Skipping design {design.id[:8]} — project over budget")
    continue  # Skip to next candidate
```

2. **In `_run_one_feature()`:** Before launching a feature's workflow:
```python
if not check_budget_before_new_work(db, project_id):
    logger.warning(f"[BUDGET] Cannot launch feature — project over budget")
    return  # Don't launch
```

**Acceptance Criteria:**
- [ ] `pick_next_design()` skips designs for over-budget projects
- [ ] `_run_one_feature()` refuses to launch features for over-budget projects
- [ ] Log messages use `[BUDGET]` prefix
- [ ] Existing behavior unchanged when `cost_limit_usd` is None

**Test Plan:**
```python
def test_pick_next_design_skips_over_budget_project(db_session, sample_project, sample_design):
    """pick_next_design should skip designs for over-budget projects."""
    sample_project.cost_limit_usd = 10.0
    sample_project.cost_total_usd = 15.0
    db_session.commit()
    
    result = pick_next_design(db_session)
    assert result is None or result.project_id != sample_project.id
```

---

#### **T3: Limit Raise Clears Budget Pause**

**Priority:** P0 (Critical)  
**Effort:** Small (1-2 hours)  
**Blocks:** Budget resume functionality  
**Blocked By:** None  

**Description:** When `PUT /projects/{id}` raises or clears the cost limit, clear `"budget"`-paused workflows for that project.

**Files to Modify:**
- `src/mcp/autopilot_api.py`

**Changes:**

In the `PUT /projects/{id}` handler (around line 1841), after updating `cost_limit_usd`:

```python
# After updating project fields:
if "cost_limit_usd" in update_data:
    new_limit = update_data["cost_limit_usd"]
    if new_limit is None or (project.cost_total_usd and new_limit > project.cost_total_usd):
        # Clear budget pause on workflows
        budget_paused = db.query(Workflow).filter(
            Workflow.project_id == project_id,
            Workflow.paused_by == "budget"
        ).all()
        for wf in budget_paused:
            wf.status = "active"
            wf.paused_by = None
            wf.paused_at = None
            wf.status_reason = None
        if budget_paused:
            logger.info(f"[BUDGET] Cleared budget pause on {len(budget_paused)} workflows — limit raised to ${new_limit}")
```

**Acceptance Criteria:**
- [ ] Raising limit clears `"budget"`-paused workflows
- [ ] Setting limit to None clears all budget pauses
- [ ] Lowering limit does NOT clear pauses
- [ ] Log messages use `[BUDGET]` prefix

**Test Plan:**
```python
def test_raising_limit_clears_budget_pause(db_session, sample_project, sample_workflow):
    """Raising the cost limit should clear budget-paused workflows."""
    sample_project.cost_limit_usd = 10.0
    sample_project.cost_total_usd = 15.0
    sample_workflow.status = "paused"
    sample_workflow.paused_by = "budget"
    db_session.commit()
    
    # Simulate PUT /projects/{id} with higher limit
    sample_project.cost_limit_usd = 20.0
    # Clear budget pause logic here
    
    assert sample_workflow.status == "active"
    assert sample_workflow.paused_by is None
```

---

#### **T4: Wire All LangChainLLMClient Call Sites**

**Priority:** P1 (High)  
**Effort:** Medium (3-5 hours)  
**Blocks:** OpenRouter direct cost capture  
**Blocked By:** T5  

**Description:** Route all LLM invocations through `_invoke_and_record()` to capture costs from OpenRouter direct calls.

**Files to Modify:**
- `src/interfaces/langchain_llm_client.py`

**Call Sites to Wire:**

| Method | Current Call | Required Change |
|--------|-------------|-----------------|
| `classify_complexity()` | `model.ainvoke(messages)` | Route through `_invoke_and_record(model, messages, "complexity_classification")` |
| `enrich_task()` | `model.ainvoke(messages)` | Route through `_invoke_and_record(model, messages, "task_enrichment", task_id=task_id)` |
| `analyze_agent_state()` | `model.ainvoke(messages)` | Route through `_invoke_and_record(model, messages, "agent_state_analysis", task_id=task_info.get("task_id"))` |
| `analyze_agent_trajectory()` | `model.ainvoke(messages)` | Route through `_invoke_and_record(model, messages, "trajectory_analysis", task_id=task_info.get("task_id"))` |
| `analyze_system_coherence()` | `model.ainvoke(messages)` | Route through `_invoke_and_record(model, messages, "system_coherence")` |
| `review_qa_report()` | `model.ainvoke(messages)` | Route through `_invoke_and_record(model, messages, "qa_review", task_id=task_id)` |
| `generate_agent_prompt()` | `model.ainvoke(messages)` | Route through `_invoke_and_record(model, messages, "prompt_generation", task_id=task_id)` |

**Acceptance Criteria:**
- [ ] All 7 call sites route through `_invoke_and_record()`
- [ ] `generate_embedding()` excluded (not cost-tracked)
- [ ] Component names match table above
- [ ] No behavioral changes to LLM calls

---

#### **T5: Thread `task_id` into LLM Methods**

**Priority:** P1 (High)  
**Effort:** Medium (2-4 hours)  
**Blocks:** T4 (task_id must be available for routing)  
**Blocked By:** None  

**Description:** Add `task_id` parameter to methods that don't currently accept it, and thread it from callers.

**Files to Modify:**
- `src/interfaces/langchain_llm_client.py`
- Callers of these methods (grep for each method name)

**Changes:**

1. **`enrich_task()`:** Add `task_id: str` parameter
2. **`classify_complexity()`:** No task_id (design-level operation)
3. **`analyze_agent_state()`:** Verify `task_info` dict contains `task_id`
4. **`analyze_agent_trajectory()`:** Verify `task_info` dict contains `task_id`
5. **`analyze_system_coherence()`:** No task_id (system-wide operation)
6. **`review_qa_report()`:** Add `task_id: str` parameter
7. **`generate_agent_prompt()`:** Add `task_id: str` parameter

**Acceptance Criteria:**
- [ ] All task-scoped methods accept `task_id` parameter
- [ ] Callers pass correct `task_id`
- [ ] Non-task-scoped methods work without `task_id`
- [ ] No breaking changes to existing callers

---

#### **T6: Create API Cost Query Endpoints**

**Priority:** P1 (High)  
**Effort:** Medium (3-5 hours)  
**Blocks:** T7 (frontend needs data)  
**Blocked By:** None  

**Description:** Create API endpoints for querying cost data at various hierarchy levels.

**Files to Create/Modify:**
- `src/mcp/autopilot_api.py`

**Endpoints:**

| Endpoint | Method | Response |
|----------|--------|----------|
| `/projects/{id}/costs` | GET | Project cost summary with design breakdown |
| `/designs/{id}/costs` | GET | Design cost summary with feature breakdown |
| `/features/{id}/costs` | GET | Feature cost summary with workflow breakdown |
| `/workflows/{id}/costs` | GET | Workflow cost summary with task breakdown |
| `/tasks/{id}/costs` | GET | Task cost summary with entry breakdown |

**Response Schema (example for project):**
```python
class ProjectCostSummary(BaseModel):
    project_id: str
    cost_total_usd: float
    cost_limit_usd: Optional[float]
    remaining_usd: Optional[float]
    is_over_budget: bool
    designs: List[DesignCostSummary]
    
class DesignCostSummary(BaseModel):
    design_id: str
    design_name: str
    cost_total_usd: float
    features: List[FeatureCostSummary]
```

**Acceptance Criteria:**
- [ ] All 5 endpoints implemented
- [ ] Response schemas defined
- [ ] Pagination for large result sets
- [ ] 404 for invalid IDs
- [ ] OpenAPI schema updated

---

#### **T7: Create Frontend Cost Components**

**Priority:** P2 (Medium)  
**Effort:** Large (5-8 hours)  
**Blocks:** Cost visibility in UI  
**Blocked By:** T6  

**Description:** Create React components for displaying cost data throughout the UI.

**Files to Create:**
- `frontend/src/components/cost/CostDisplay.tsx`
- `frontend/src/components/cost/FeatureCostBadge.tsx`
- `frontend/src/components/cost/DesignCostRow.tsx`
- `frontend/src/components/cost/ProjectCostSummary.tsx`
- `frontend/src/components/cost/BudgetPausedLabel.tsx`

**Files to Modify:**
- `frontend/src/components/DesignScreen.tsx` (add CostDisplay)
- `frontend/src/components/FeatureCard.tsx` (add FeatureCostBadge)
- `frontend/src/components/DesignList.tsx` (add DesignCostRow)
- `frontend/src/components/Dashboard.tsx` (add ProjectCostSummary)

**Acceptance Criteria:**
- [ ] CostDisplay shows `$current / $limit` with progress indicator
- [ ] Link to ProjectSettingsModal when limit not set
- [ ] FeatureCostBadge shows cost on feature cards
- [ ] DesignCostRow shows cost in design list
- [ ] ProjectCostSummary shows aggregate on dashboard
- [ ] BudgetPausedLabel shows "Paused: budget limit reached"
- [ ] Responsive design (mobile-friendly)
- [ ] Accessibility (ARIA labels, keyboard navigation)

---

#### **T8: Create Frontend Budget Config**

**Priority:** P2 (Medium)  
**Effort:** Small (2-3 hours)  
**Blocks:** Budget configuration from UI  
**Blocked By:** T6  

**Description:** Add budget configuration input to ProjectSettingsModal.

**Files to Modify:**
- `frontend/src/components/ProjectSettingsModal.tsx`

**Changes:**
- Add number input for `cost_limit_usd`
- Validation: non-negative, max $100,000
- Show current spend alongside limit input
- "Clear Limit" button to set to None

**Acceptance Criteria:**
- [ ] Number input for cost limit
- [ ] Validation: non-negative, max $100,000
- [ ] Current spend displayed
- [ ] "Clear Limit" button
- [ ] Success/error feedback on save

---

#### **T9: Create Pi Extension**

**Priority:** P2 (Medium)  
**Effort:** Medium (3-5 hours)  
**Blocks:** Real-time cost display in pi TUI  
**Blocked By:** None  

**Description:** Create pi extension for real-time cost capture and TUI display.

**Files to Create:**
- `extensions/hephaestus-cost-tracker/package.json`
- `extensions/hephaestus-cost-tracker/src/index.ts`
- `extensions/hephaestus-cost-tracker/README.md`

**Extension API:**
```typescript
interface HephaestusCostTracker {
    turn_end(ctx: PiContext, turn: TurnData): Promise<void>;
}

interface TurnData {
    message: {
        usage: {
            cost: { total: number };
            input: number;
            output: number;
        };
    };
}
```

**Behavior:**
1. On `turn_end`: extract `message.usage.cost.total`
2. POST to `${HEPHAESTUS_API_URL}/cost-entries`
3. Update TUI status: `ctx.ui.setStatus("💰 $0.05 (session: $1.23)")`
4. On error: log warning, don't block turn

**Acceptance Criteria:**
- [ ] Extension installs at `~/.pi/agent/extensions/hephaestus-cost-tracker/`
- [ ] POSTs each turn's cost immediately
- [ ] Shows running cost in TUI
- [ ] Configurable API URL via env var
- [ ] Graceful error handling (doesn't block pi)
- [ ] README with installation instructions

---

#### **T10: Create Unit Tests for cost_derivation**

**Priority:** P1 (High)  
**Effort:** Medium (3-5 hours)  
**Blocks:** Confidence in cost derivation correctness  
**Blocked By:** None  

**Description:** Create comprehensive unit tests for all functions in `cost_derivation.py`.

**Files to Create:**
- `tests/test_cost_derivation.py` (extend existing `tests/test_cost_tracking.py`)

**Test Cases:**

| Test | Function | Scenario |
|------|----------|----------|
| `test_record_cost_creates_entry` | `record_cost()` | Verifies CostEntry created with correct fields |
| `test_record_cost_triggers_task_rollup` | `record_cost()` | Verifies Task.cost_total_usd updated |
| `test_record_cost_triggers_workflow_rollup` | `record_cost()` | Verifies Workflow.cost_total_usd updated |
| `test_derive_task_cost_basic` | `derive_task_cost()` | SUM of cost_entries matches |
| `test_derive_task_cost_self_heal` | `derive_task_cost()` | Corrects mismatched stored value |
| `test_derive_task_cost_no_entries` | `derive_task_cost()` | Returns 0.0 for task with no entries |
| `test_derive_workflow_cost_rollup` | `derive_workflow_cost()` | Verifies rollup to feature/design/project |
| `test_derive_feature_cost_join` | `derive_feature_cost()` | Verifies JOIN through Workflow table |
| `test_derive_design_cost_join` | `derive_design_cost()` | Verifies JOIN through Workflow → Feature |
| `test_derive_project_cost_join` | `derive_project_cost()` | Verifies JOIN through full chain |
| `test_budget_enforcement_triggers` | `_check_budget_enforcement()` | Pauses when over limit |
| `test_budget_enforcement_no_limit` | `_check_budget_enforcement()` | No-op when limit is None |
| `test_budget_enforcement_under_limit` | `_check_budget_enforcement()` | No-op when under limit |
| `test_pause_project_workflows_idempotent` | `_pause_project_workflows()` | Second call finds nothing |
| `test_pause_includes_phase0` | `_pause_project_workflows()` | Matches both definition_ids |
| `test_check_budget_before_new_work` | `check_budget_before_new_work()` | Returns False when over budget |

**Acceptance Criteria:**
- [ ] All 16 test cases implemented
- [ ] Tests use in-memory SQLite
- [ ] Tests are isolated (no side effects)
- [ ] 100% line coverage for `cost_derivation.py`

---

#### **T11: Create Integration Tests for Collection**

**Priority:** P1 (High)  
**Effort:** Medium (3-5 hours)  
**Blocks:** Confidence in collection pipeline  
**Blocked By:** None  

**Description:** Create integration tests for the cost collection service.

**Files to Create:**
- `tests/test_cost_collection_integration.py`

**Test Cases:**

| Test | Scenario |
|------|----------|
| `test_collect_pi_task_cost` | End-to-end: pi session → CostEntry rows |
| `test_collect_claude_code_task_cost` | End-to-end: Claude Code session → CostEntry rows |
| `test_collect_opencode_task_cost` | End-to-end: OpenCode session → CostEntry rows |
| `test_checkpoint_prevents_double_count` | Second collection skips already-processed lines |
| `test_session_file_discovery` | Correct file found by session_id |
| `test_path_traversal_rejected` | Suspicious paths rejected |
| `test_unknown_cli_type_skipped` | Unknown CLI type logs warning |

**Acceptance Criteria:**
- [ ] All 7 test cases implemented
- [ ] Tests use mock session files
- [ ] Tests verify CostEntry creation
- [ ] Tests verify checkpoint advancement

---

#### **T12: Create Integration Tests for Budget Enforcement**

**Priority:** P1 (High)  
**Effort:** Medium (3-5 hours)  
**Blocks:** Confidence in budget enforcement  
**Blocked By:** T1, T2, T3  

**Description:** Create integration tests for budget enforcement end-to-end.

**Files to Create:**
- `tests/test_budget_enforcement_integration.py`

**Test Cases:**

| Test | Scenario |
|------|----------|
| `test_budget_pauses_on_overage` | Cost exceeds limit → workflows paused |
| `test_budget_includes_phase0` | Phase 0 workflows paused |
| `test_budget_pauses_terminate_agents` | Active agents terminated |
| `test_budget_blocks_new_work` | `check_budget_before_new_work` returns False |
| `test_budget_auto_resume_blocked` | Self-heal paths don't resume budget-paused |
| `test_budget_play_button_blocked` | Play button doesn't resume budget-paused |
| `test_limit_raise_clears_pause` | Raising limit clears budget pause |
| `test_limit_clear_clears_pause` | Setting limit to None clears budget pause |
| `test_concurrent_cost_writes` | Multiple parallel CostEntry writes don't cause issues |

**Acceptance Criteria:**
- [ ] All 9 test cases implemented
- [ ] Tests use in-memory SQLite
- [ ] Tests verify end-to-end behavior
- [ ] Tests cover edge cases

---

## 5. Infrastructure Requirements

### 5.1 Database

| Requirement | Specification |
|-------------|---------------|
| Engine | SQLite with WAL mode |
| Tables | `cost_entries`, `session_cost_checkpoints` (already created) |
| Indexes | `ix_cost_entries_task_id`, `ix_cost_entries_workflow_id`, `ix_cost_entries_recorded_at` |
| Migrations | `_migrate_cost_tables()` in `database.py` |
| Thread Safety | WAL mode + SQLAlchemy QueuePool |

### 5.2 API Server

| Requirement | Specification |
|-------------|---------------|
| Framework | FastAPI (existing) |
| New Endpoints | 5 cost query endpoints (T6) |
| Authentication | Existing auth middleware |
| Rate Limiting | Existing rate limiter |

### 5.3 Frontend

| Requirement | Specification |
|-------------|---------------|
| Framework | React 18 + TypeScript |
| Styling | Tailwind CSS |
| State Management | Existing patterns |
| API Client | Existing fetch wrapper |

### 5.4 Pi Extension

| Requirement | Specification |
|-------------|---------------|
| Runtime | pi extension API |
| Build | TypeScript → JavaScript |
| Installation | `~/.pi/agent/extensions/` |
| Configuration | `HEPHAESTUS_API_URL` env var |

---

## 6. Risk Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Claude Code price table stale | High | Medium | Document update process; version-check mechanism |
| `usage.include=true` doesn't survive LangChain | Medium | Medium | Smoke test before implementing T4 |
| Concurrent CostEntry contention | Low | Medium | WAL mode handles; MAX_PARALLEL_FEATURES = 4 |
| Pi extension not loaded | Medium | Low | JSONL tailing fallback works |
| Budget enforcement edge case | Medium | High | Comprehensive tests (T12) |

---

## 7. Implementation Order

### Phase A: Budget Enforcement Completion (T1, T2, T3)
**Goal:** Complete budget enforcement so it actually works end-to-end.

1. **T1:** Generalize `paused_by` guards
2. **T2:** Wire budget checks into orchestrator
3. **T3:** Limit raise clears budget pause

### Phase B: OpenRouter Direct Collection (T4, T5)
**Goal:** Capture costs from backend's own LLM calls.

4. **T5:** Thread `task_id` into LLM methods
5. **T4:** Wire all call sites through `_invoke_and_record()`

### Phase C: API + Frontend (T6, T7, T8)
**Goal:** Surface cost data in the UI.

6. **T6:** Create API cost query endpoints
7. **T7:** Create frontend cost components
8. **T8:** Create frontend budget config

### Phase D: Pi Extension (T9)
**Goal:** Real-time cost display in pi TUI.

9. **T9:** Create pi extension

### Phase E: Testing (T10, T11, T12)
**Goal:** Comprehensive test coverage.

10. **T10:** Unit tests for cost_derivation
11. **T11:** Integration tests for collection
12. **T12:** Integration tests for budget enforcement

---

## 8. Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Cost capture rate | > 95% of LLM calls | Compare CostEntry count vs expected calls |
| Rollup accuracy | < $0.001 discrepancy | Self-heal log frequency |
| Budget enforcement | 100% of over-budget projects paused | Manual verification |
| Test coverage | > 90% line coverage | Coverage report |
| UI responsiveness | < 200ms for cost queries | Performance testing |

---

## 9. Appendix

### A. File Reference

| File | Purpose | Status |
|------|---------|--------|
| `src/core/cost_derivation.py` | Self-healing cost rollup | ✅ Implemented |
| `src/core/database.py` | CostEntry, SessionCostCheckpoint models | ✅ Implemented |
| `src/services/cost_collection_service.py` | Per-CLI collectors | ✅ Implemented |
| `src/interfaces/langchain_llm_client.py` | OpenRouter direct collection | ✅ Implemented |
| `src/autopilot/orchestrator.py` | Budget enforcement guards | ✅ Implemented |
| `src/mcp/autopilot_api.py` | API endpoints (all 7) | ✅ Implemented |
| `tests/test_cost_tracking.py` | Unit + derivation tests (39 tests) | ✅ Implemented |
| `tests/test_budget_enforcement_integration.py` | Budget enforcement tests (13 tests) | ✅ Implemented |
| `extensions/hephaestus-cost-tracker/` | Pi extension | ✅ Implemented |
| `frontend/src/components/cost/` | Cost UI components | ❌ Not created |

### B. Design Decisions Log

| Decision | Rationale | Status |
|----------|-----------|--------|
| Append-only ledger | Self-healing pattern from status_derivation.py | ✅ Accepted |
| Checkpoint by session_id | Survives agent retries | ✅ Accepted |
| Collection on completion | No torn-read risk | ✅ Accepted |
| Pi extension preferred | Real-time display, no file access | ✅ Accepted |
| Price table for Claude Code | No dollar cost in transcripts | ✅ Accepted |
| `is not None` for paused_by | Prevents budget-paused auto-resume | ✅ Implemented |

### C. Open Questions

| Question | Status | Recommendation |
|----------|--------|----------------|
| Force session_id on standalone tasks? | Unresolved | Yes — eliminates permanent gap |
| `usage.include=true` survives LangChain? | ✅ Confirmed working | Cost extraction from response_metadata verified |
| OpenCode actually used? | Needs verification | Check workflow.yaml |

---

*Document generated by Architecture Design Agent (Phase 3) on 2026-07-21*
