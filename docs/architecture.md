# Cost Tracking Database Schema — Technical Architecture

**Feature ID:** cost-tracking-database-schema  
**Complexity:** MODERATE (3 new tables, 1 new module, ~10 modified files, no external deps)  
**Date:** 2026-07-21  
**Source of Truth:** `docs/requirements_analysis.md` (Phase 1), `.hephaestus/design.md`

---

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Cost Sources                              │
│                                                                  │
│  ┌──────────┐  ┌──────────────┐  ┌──────────┐  ┌─────────────┐ │
│  │ Pi JSONL  │  │ Claude Code  │  │ OpenCode │  │ OpenRouter  │ │
│  │ Extension │  │ (tokens→$)   │  │ (stdout) │  │ Direct      │ │
│  └─────┬─────┘  └──────┬───────┘  └────┬─────┘  └──────┬──────┘ │
│        │               │               │               │        │
│        └───────┬───────┴───────┬───────┘               │        │
│                │               │                        │        │
│                ▼               ▼                        ▼        │
│     ┌──────────────────────────────────────────────────────┐     │
│     │              CostEntry (append-only ledger)           │     │
│     │  id │ task_id │ agent_id │ workflow_id │ source │ ... │     │
│     └────────────────────────┬─────────────────────────────┘     │
│                              │                                    │
│                              ▼                                    │
│     ┌──────────────────────────────────────────────────────┐     │
│     │              cost_derivation.py                       │     │
│     │  derive_cost_totals(entry) → rollup on every write    │     │
│     │  SUM per task → feature → design → project            │     │
│     │  budget enforcement check (if limit set)              │     │
│     └──────┬──────────┬──────────┬──────────┬──────────────┘     │
│            │          │          │          │                     │
│            ▼          ▼          ▼          ▼                     │
│     ┌─────────┐ ┌──────────┐ ┌─────────┐ ┌───────────┐          │
│     │  Task   │ │ Feature  │ │ Design  │ │  Project  │          │
│     │.cost_   │ │.cost_    │ │.cost_   │ │.cost_     │          │
│     │total_usd│ │total_usd │ │total_usd│ │total_usd  │          │
│     └─────────┘ └──────────┘ └─────────┘ │.cost_     │          │
│                                           │limit_usd  │          │
│                                           └─────┬─────┘          │
│                                                 │                │
│                                    ┌────────────┴────────────┐   │
│                                    │  if cost >= limit:       │   │
│                                    │    _pause_project_       │   │
│                                    │    workflows("budget")   │   │
│                                    └─────────────────────────┘   │
│                                                                  │
│     ┌──────────────────────────────────────────────────────┐     │
│     │  SessionCostCheckpoint (progress tracker)             │     │
│     │  session_id (PK) │ lines_processed │ updated_at      │     │
│     │  Keyed by session_id, NOT Agent.id (prevents          │     │
│     │  double-counting across agent retries)                │     │
│     └──────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow

1. **Collection**: Each cost source writes a `CostEntry` row (one per LLM turn/call)
2. **Derivation**: `derive_cost_totals()` aggregates upward: task → feature → design → project
3. **Budget Check**: If `project.cost_total_usd >= project.cost_limit_usd`, pause all project workflows
4. **Display**: Frontend reads denormalized `cost_total_usd` columns for fast display

### Key Design Invariants

- **Append-only ledger**: `cost_entries` is the source of truth. Denormalized rollups are derived, not maintained.
- **Self-healing derivation**: Every new `CostEntry` triggers a full rollup recompute. Missed updates never permanently desync.
- **Checkpoint by session_id**: `SessionCostCheckpoint` is keyed by `session_id` (which survives agent retries), not `Agent.id` (which doesn't).
- **Budget pause idempotency**: `_pause_project_workflows` matches `status IN ("active","running")` — second concurrent call finds nothing to do.
- **Spend always lands at-or-slightly-over limit**: Cost only knowable after the fact; enforcement stops the *next* call.

---

## 2. Component Interfaces

### 2.1 CostEntry Model (`src/core/database.py`)

```python
class CostEntry(Base):
    __tablename__ = "cost_entries"

    id = Column(String, primary_key=True)  # cost-<uuid8>
    task_id = Column(String, ForeignKey("tasks.id"), nullable=True, index=True)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=True)
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True, index=True)

    source = Column(String, nullable=False)  # 'pi'|'claude_code'|'opencode'|'codex'|'openrouter_direct'
    model = Column(String, nullable=True)    # e.g. "anthropic/claude-sonnet-4"

    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cache_read_tokens = Column(Integer, default=0)
    cache_write_tokens = Column(Integer, default=0)
    cost_usd = Column(Float, nullable=False)

    recorded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    raw_usage = Column(JSON, nullable=True)
```

### 2.2 SessionCostCheckpoint Model (`src/core/database.py`)

```python
class SessionCostCheckpoint(Base):
    __tablename__ = "session_cost_checkpoints"

    session_id = Column(String, primary_key=True)
    lines_processed = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

### 2.3 Rollup Columns (added to existing models)

| Model             | Column                                    |
|-------------------|-------------------------------------------|
| `Task`            | `cost_total_usd = Column(Float, default=0.0, nullable=False)` |
| `Feature`         | `cost_total_usd = Column(Float, default=0.0, nullable=False)` |
| `AutopilotDesign` | `cost_total_usd = Column(Float, default=0.0, nullable=False)` |
| `AutopilotProject`| `cost_total_usd = Column(Float, default=0.0, nullable=False)` |
| `AutopilotProject`| `cost_limit_usd = Column(Float, nullable=True)`  — `None` = no limit |

### 2.4 Cost Derivation Module (`src/core/cost_derivation.py`)

Mirrors `src/core/status_derivation.py` pattern exactly.

```python
def derive_task_cost(db: Session, task_id: str) -> float:
    """SUM(cost_entries.cost_usd) WHERE task_id = this task."""

def derive_feature_cost(db: Session, feature_id: str) -> float:
    """SUM task costs where Task.workflow_id == Feature.workflow_id."""

def derive_design_cost(db: Session, design_id: str) -> float:
    """SUM feature costs where Feature.design_id = this design."""

def derive_project_cost(db: Session, project_id: str) -> float:
    """SUM design costs where AutopilotDesign.project_id = this project."""

def derive_cost_totals(db: Session, cost_entry: CostEntry) -> None:
    """Full rollup triggered on every new CostEntry write.
    
    1. Recompute task-level cost (or use entry.cost_usd directly if task_id set)
    2. Roll up to feature → design → project
    3. Write back all denormalized cost_total_usd columns
    4. Check budget enforcement: if project.cost_total_usd >= project.cost_limit_usd,
       call _pause_project_workflows(project_id, "budget")
    """
```

### 2.5 Budget Enforcement (`src/autopilot/orchestrator.py`)

```python
def _pause_project_workflows(project_id: str, paused_by: str) -> int:
    """Shared pause function. Both /autopilot/stop endpoint and budget enforcement call this.
    
    Filters: Workflow.definition_id.in_(["autopilot", "autopilot-phase0"])
    Matches: Workflow.status.in_(["active", "running"]) and Workflow.project_id == project_id
    
    Sets: status="paused", paused_by=paused_by, paused_at=now()
    Returns: number of workflows paused (0 means already paused — idempotent)
    """
```

Guards added to:
- `pick_next_design()` — skip designs for over-budget projects
- `_run_one_feature()` — block new workflow launches for over-budget projects

### 2.6 `paused_by` Generalization

Change these guards from `== "user"` to `is not None`:
- `_try_auto_resume_paused_workflow()` (orchestrator.py:3710)
- `_create_corrective_task()` (orchestrator.py:5534)
- Stuck-workflow restart in `attempt_recovery()` (orchestrator.py:5718)

**Exception — keep `== "user"` in `AutopilotService.start()` resume-on-play** (orchestrator.py:390). Clicking play resumes user-paused but NOT budget-paused.

When `cost_limit_usd` raised or cleared via `PUT /projects/{id}`: clear `paused_by` on that project's `"budget"`-paused workflows.

### 2.7 Collector Abstraction (`src/services/cost_collection_service.py`)

```python
class CostCollector(ABC):
    @abstractmethod
    def collect(self, session_id: str, task_id: str, workflow_id: str,
                agent_id: Optional[str], session_file: Path,
                checkpoint: int) -> tuple[list[CostEntry], int]:
        """Return new CostEntry rows and new checkpoint (line count)."""

class PiJsonlCollector(CostCollector):
    """Tails pi session JSONL. Reads message.usage.cost.total.
    Checkpoint = lines_processed from SessionCostCheckpoint."""

class ClaudeCodeCollector(CostCollector):
    """Tails Claude Code JSONL. Converts tokens → $ via price table.
    Two cache-write tiers: ephemeral_1h, ephemeral_5m."""

class OpenCodeCollector(CostCollector):
    """One-shot stdout capture from `opencode run --format json`.
    No checkpoint needed — each run is one task."""

class CodexStubCollector(CostCollector):
    """Logs 'unsupported' — does not report zero."""

def collect_task_cost(task_id: str) -> None:
    """Entry point called from task_completion_service.
    Looks up task → agent → session_id → discovers session file → delegates to collector."""
```

### 2.8 OpenRouter Direct Helper (`src/interfaces/langchain_llm_client.py`)

```python
async def _invoke_and_record(self, model, messages, component: str,
                              task_id: Optional[str] = None) -> Any:
    """Wraps model.ainvoke(). Extracts cost from response_metadata.
    Writes CostEntry(source="openrouter_direct", ...).
    All 9 existing call sites route through this instead of calling model.ainvoke directly."""
```

### 2.9 Pi Extension (`extensions/hephaestus-cost-tracker.ts`)

Hooks `turn_end` → reads `message.usage.cost.total` → POSTs to `HEPHAESTUS_API_URL/api/cost-entries`. Shows running cost via `ctx.ui.setStatus()`. Fails silently (JSONL tailing fallback still works).

### 2.10 API Endpoint (`src/mcp/autopilot_api.py`)

```python
@router.post("/cost-entries")
async def create_cost_entry(req: CostEntryCreate):
    """Used by Pi extension (real-time, no checkpoint needed).
    Body: {session_id, model, usage, cost_usd, ...}
    Writes CostEntry, triggers derive_cost_totals."""

@router.put("/projects/{project_id}")
async def update_project(project_id: str, req: ProjectUpdate):
    """Extended: req.cost_limit_usd field.
    After setting new limit: if limit is null or > cost_total_usd,
    clear paused_by='budget' on this project's budget-paused workflows."""
```

---

## 3. File Change Map

| File | Action | Summary |
|------|--------|---------|
| `src/core/database.py` | MODIFY | Add `CostEntry`, `SessionCostCheckpoint` tables. Add `cost_total_usd` to Task/Feature/AutopilotDesign/AutopilotProject. Add `cost_limit_usd` to AutopilotProject. Add 5 migration functions. |
| `src/core/cost_derivation.py` | CREATE | Self-healing cost rollup module (mirror `status_derivation.py` pattern). Budget enforcement check. |
| `src/services/cost_collection_service.py` | CREATE | `CostCollector` ABC + `PiJsonlCollector` + `ClaudeCodeCollector` + `OpenCodeCollector` + `CodexStubCollector` + `collect_task_cost()` entry point. |
| `src/autopilot/orchestrator.py` | MODIFY | Extract `_pause_project_workflows`. Generalize `paused_by` guards (`== "user"` → `is not None`). Add budget guards to `pick_next_design` and `_run_one_feature`. |
| `src/mcp/autopilot_api.py` | MODIFY | Add `POST /cost-entries`. Extend `PUT /projects/{id}` for `cost_limit_usd` + budget-pause clearing. |
| `src/interfaces/langchain_llm_client.py` | MODIFY | Add `_invoke_and_record` helper. Wire all 9 call sites. Add `usage.include=true` to `extra_body`. Thread `task_id` through methods. |
| `src/services/task_completion_service.py` | MODIFY | Call `collect_task_cost(task_id)` on task completion. |
| `src/interfaces/cli_interface.py` | MODIFY | ClaudeCodeAgent already has UUID5 session-id fix. No change needed (verified). |
| `extensions/hephaestus-cost-tracker/` | CREATE | Pi extension: `package.json`, `index.ts`, `tsconfig.json`. |
| `frontend/src/components/ProjectSettingsModal.tsx` | MODIFY | Add `cost_limit_usd` number input. |
| `frontend/src/components/autopilot/DesignQueuePanel.tsx` | MODIFY | Add cost display indicator. |
| `src/interfaces/cost_tracker.py` | KEEP | Dead code — leave as-is for now (no import changes needed). |

---

## 4. Task Breakdown with Blocking Relationships

### Dependency Graph

```
T1 (Schema: Tables + Migrations)
 └─┬── T2 (Cost Derivation Module)
   │    ├─ T3 (Pi JSONL Collector)
   │    │   └─ T4 (Budget Enforcement)
   │    │       └─ T5 (paused_by Generalization)
   │    │           └─ T6 (API: /cost-entries + Update Project)
   │    │               └─ T8 (UI: Cost Display + Budget Config)
   │    └─ T7 (OpenRouter Direct + Claude Code Collector)
   │         └─ T8 (UI: Cost Display + Budget Config)
   └─ T9 (Pi Extension)
       └─ T10 (task_completion_service wiring)

T11 (Codex Stub) — independent, no blockers, can land anytime
T12 (OpenCode Collector) — gated on workflow.yaml check
```

---

### T1: Schema — Tables + Migrations

**Blocked by:** Nothing (starting task)  
**Blocks:** T2, T3, T7, T9, T11, T12  
**Files:** `src/core/database.py`  
**Estimated effort:** Small  

**Changes:**
1. Add `CostEntry` class with all columns and indexes (`ix_cost_entries_task_id`, `ix_cost_entries_workflow_id`)
2. Add `SessionCostCheckpoint` class
3. Add `cost_total_usd = Column(Float, default=0.0, nullable=False)` to `Task`, `Feature`, `AutopilotDesign`, `AutopilotProject`
4. Add `cost_limit_usd = Column(Float, nullable=True)` to `AutopilotProject`
5. Add 5 migration functions following `_migrate_workflow_paused_by_column` pattern:
   - `_migrate_cost_entries_table()` — `CREATE TABLE IF NOT EXISTS`
   - `_migrate_session_cost_checkpoints_table()` — `CREATE TABLE IF NOT EXISTS`
   - `_migrate_task_cost_total_column()`
   - `_migrate_feature_cost_total_column()`
   - `_migrate_design_cost_total_column()`
   - `_migrate_project_cost_total_column()`
   - `_migrate_project_cost_limit_column()`
6. Call all migrations from `__init__` alongside existing ones (after line 1413)

**Acceptance:** `from src.core.database import CostEntry, SessionCostCheckpoint` succeeds. All tables/columns exist in DB after startup. Existing tests pass.

---

### T2: Cost Derivation Module

**Blocked by:** T1  
**Blocks:** T3, T4, T7  
**Files:** `src/core/cost_derivation.py` (new)  
**Estimated effort:** Medium  

**Changes:**
1. Create `src/core/cost_derivation.py` mirroring `src/core/status_derivation.py`
2. Implement `derive_task_cost(db, task_id)` — `SUM(cost_entries.cost_usd) WHERE task_id = :task_id`
3. Implement `derive_feature_cost(db, feature_id)` — join Feature → Task by workflow_id
4. Implement `derive_design_cost(db, design_id)` — sum feature costs by design_id
5. Implement `derive_project_cost(db, project_id)` — sum design costs by project_id
6. Implement `derive_cost_totals(db, cost_entry)` — full rollup chain, writes back all `cost_total_usd` columns
7. Budget enforcement check inside `derive_cost_totals`: after project update, if `cost_limit_usd is not None and cost_total_usd >= cost_limit_usd`, call `_pause_project_workflows(project_id, "budget")`

**Thread safety:** WAL mode handles concurrent reads. The write path is a single `db.commit()` per derivation. Up to 4 concurrent writers (MAX_PARALLEL_FEATURES) — contention is low because each writes to different task_id rows.

**Acceptance:** Unit tests for each derive function. Self-heal test: insert missing CostEntry for a completed task, call derive, confirms cost_total_usd updated correctly.

---

### T3: Pi JSONL Collector

**Blocked by:** T1, T2  
**Blocks:** T10  
**Files:** `src/services/cost_collection_service.py` (new)  
**Estimated effort:** Medium  

**Changes:**
1. Create `CostCollector` ABC with `collect(session_id, task_id, workflow_id, agent_id, session_file, checkpoint) -> (entries, new_checkpoint)` 
2. Implement `PiJsonlCollector`:
   - `collect()`: reads lines after `checkpoint` from session JSONL file
   - Filters `type == "message"` and `message.role == "assistant"`
   - Extracts `message.usage.cost.total`, `message.usage.input`, `message.usage.output`, `message.usage.cacheRead`, `message.usage.cacheWrite`, `message.message.model`
   - Returns `CostEntry` list + new line count
3. Implement `CodexStubCollector.collect()`: logs warning "Codex cost collection not supported", returns empty
4. Implement `collect_task_cost(task_id)` entry point:
   - Query task → find assigned agent → get session_id from agent launch_params
   - Discover session file: glob `*_<session_id>.jsonl` in cwd-keyed directory (`~/.pi/agent/sessions/--<sanitized_cwd>--/`)
   - Read first line to verify session ID match
   - Get/update `SessionCostCheckpoint` for this session_id
   - Delegate to appropriate collector based on agent's `cli_type`
   - Write `CostEntry` rows, trigger `derive_cost_totals` for each

**Session file discovery:**
```python
def _discover_session_file(session_id: str, cwd: str) -> Optional[Path]:
    sanitized = cwd.replace("/", "-")
    sessions_dir = Path.home() / ".pi" / "agent" / "sessions" / f"--{sanitized}--"
    matches = list(sessions_dir.glob(f"*_{session_id}.jsonl"))
    if not matches:
        return None
    # Verify first line
    with open(matches[0]) as f:
        first = json.loads(f.readline())
        if first.get("id") == session_id:
            return matches[0]
    return None
```

**Acceptance:** Test against a real `.jsonl` session file. Verify checkpoint advances. Verify no double-counting on second call. Verify CostEntry rows have correct `source`, `model`, `cost_usd`.

---

### T4: Budget Enforcement

**Blocked by:** T2 (cost_derivation.py triggers enforcement), T3 (pi collector provides real cost data to test against)  
**Blocks:** T5  
**Files:** `src/autopilot/orchestrator.py`, `src/core/cost_derivation.py`  
**Estimated effort:** Medium  

**Changes:**
1. Extract `_pause_project_workflows(project_id, paused_by)` from `/autopilot/stop` handler logic (autopilot_api.py:2943):
   ```python
   def _pause_project_workflows(db, project_id: str, paused_by: str) -> int:
       workflows = db.query(Workflow).filter(
           Workflow.project_id == project_id,
           Workflow.definition_id.in_(["autopilot", "autopilot-phase0"]),  # FIX: includes Phase 0
           Workflow.status.in_(["active", "running"]),
       ).all()
       count = 0
       for wf in workflows:
           wf.status = "paused"
           wf.paused_by = paused_by
           wf.paused_at = datetime.utcnow()
           count += 1
           # Terminate active agents on this workflow
           active_agents = db.query(Agent).filter(
               Agent.workflow_id == wf.id,
               Agent.status == "active",
           ).all()
           for agent in active_agents:
               agent.status = "terminated"
               agent.terminated_at = datetime.utcnow()
       if count > 0:
           db.commit()
       return count
   ```
2. Refactor `/autopilot/stop` endpoint to call `_pause_project_workflows(db, project_id, "user")` instead of inline logic — fixes existing Phase 0 gap for free
3. Add budget guard to `pick_next_design()` at top of project loop:
   ```python
   if project.cost_limit_usd is not None and project.cost_total_usd >= project.cost_limit_usd:
       logger.info(f"Project {project.name} over budget — skipping")
       continue
   ```
4. Add budget guard to `_run_one_feature()` before `run_single_workflow`:
   ```python
   if project.cost_limit_usd is not None and project.cost_total_usd >= project.cost_limit_usd:
       logger.info(f"Project over budget — blocking new workflow for feature {feature_id[:8]}")
       return "budget_blocked"
   ```
5. Wire `_pause_project_workflows` import into `cost_derivation.py` (called from `derive_cost_totals`)

**Acceptance:** End-to-end test: set `cost_limit_usd = 0.01`, insert CostEntry that exceeds it, verify workflows paused with `paused_by="budget"`. Verify Phase 0 workflow also paused. Verify `pick_next_design` skips project. Verify no double-pause on concurrent trigger.

---

### T5: `paused_by` Generalization

**Blocked by:** T4 (budget enforcement creates "budget" paused_by value)  
**Blocks:** T6, T8  
**Files:** `src/autopilot/orchestrator.py`, `src/mcp/autopilot_api.py`  
**Estimated effort:** Small  

**Changes:**
1. `_try_auto_resume_paused_workflow()` (line 3710): change `if wf.paused_by == "user":` → `if wf.paused_by is not None:`
2. `_create_corrective_task()` (line 5534): change `if wf.paused_by == "user":` → `if wf.paused_by is not None:`
3. Stuck-workflow restart in `attempt_recovery()` (line 5718): change `wf.paused_by == "user"` → `wf.paused_by is not None`
4. `AutopilotService.start()` (line 390): **KEEP** `== "user"` — play button resumes user-paused but NOT budget-paused
5. In `update_project()` (autopilot_api.py): after setting `cost_limit_usd`, add:
   ```python
   if req.cost_limit_usd is not None:
       proj.cost_limit_usd = req.cost_limit_usd
   elif req.cost_limit_usd is None and hasattr(req, 'cost_limit_usd'):
       proj.cost_limit_usd = None  # clearing limit
   
   # Clear budget-paused workflows if limit raised or cleared
   if proj.cost_limit_usd is None or (proj.cost_total_usd and proj.cost_total_usd < proj.cost_limit_usd):
       budget_paused = db.query(Workflow).filter(
           Workflow.project_id == project_id,
           Workflow.paused_by == "budget",
       ).all()
       for wf in budget_paused:
           wf.paused_by = None
           wf.status = "active"
   ```

**Acceptance:** Budget-paused workflow does NOT auto-resume through self-heal, corrective task, or stuck-workflow restart. User click play does NOT clear budget pause. Raising limit DOES clear budget pause and allows pipeline to resume.

---

### T6: API Endpoints

**Blocked by:** T5 (paused_by generalization must land first so clearing logic works)  
**Blocks:** T8, T9, T10  
**Files:** `src/mcp/autopilot_api.py`  
**Estimated effort:** Small  

**Changes:**
1. Add `CostEntryCreate` Pydantic model:
   ```python
   class CostEntryCreate(BaseModel):
       session_id: str
       task_id: Optional[str] = None
       agent_id: Optional[str] = None
       workflow_id: Optional[str] = None
       source: str
       model: Optional[str] = None
       input_tokens: int = 0
       output_tokens: int = 0
       cache_read_tokens: int = 0
       cache_write_tokens: int = 0
       cost_usd: float
       raw_usage: Optional[dict] = None
   ```
2. Add `POST /cost-entries` endpoint:
   - Creates `CostEntry` with `id=cost-<uuid8>`
   - Calls `derive_cost_totals(db, entry)` after commit
   - Returns `{"id": entry.id, "cost_usd": entry.cost_usd}`
3. Extend `ProjectUpdate` model: add `cost_limit_usd: Optional[float] = None`
4. Extend `update_project()` handler: persist `cost_limit_usd` + call budget-pause clearing logic (from T5)
5. Extend `ProjectItem` response model: add `cost_total_usd: float` and `cost_limit_usd: Optional[float]`

**Acceptance:** `POST /cost-entries` creates a row and triggers rollup. `PUT /projects/{id}` sets limit. Clearing limit resumes budget-paused workflows.

---

### T7: OpenRouter Direct + Claude Code Collector

**Blocked by:** T2 (cost_derivation.py)  
**Blocks:** T8  
**Files:** `src/interfaces/langchain_llm_client.py`, `src/services/cost_collection_service.py`  
**Estimated effort:** Medium-Large  

**Changes:**

**Part A: OpenRouter Direct**
1. Add `usage: {include: true}` to `extra_body` in `ChatOpenAI` construction (~line 239)
2. Add `_invoke_and_record()` helper method to `LangChainLLMClient`
3. Router all 9 call sites through helper: `classify_complexity`, `enrich_task`, `resolve_ticket_clarification`, `analyze_agent_state`, `analyze_agent_trajectory`, `analyze_system_coherence`, `review_qa_report`, `generate_agent_prompt`, `generate_embedding`
4. Thread `task_id` parameter into methods that don't have it (check each caller)
5. Helper extracts `response.response_metadata` for cost data (needs smoke test to confirm structure)
6. Writes `CostEntry(source="openrouter_direct")` directly — no checkpoint needed

**Part B: Claude Code Collector**
1. Add `ClaudeCodeCollector(CostCollector)` to `cost_collection_service.py`
2. Price table (needs updating when Anthropic reprices):
   ```python
   CLAUDE_PRICING = {
       "claude-sonnet-4": {"input_per_mtok": 3.0, "output_per_mtok": 15.0, "cache_write_per_mtok": 3.75, "cache_read_per_mtok": 0.30},
       # ephemeral_1h cache write = 3.75/MTok, ephemeral_5m = standard write price
   }
   ```
3. Collector reads `cache_creation_input_tokens`, `cache_read_input_tokens`, `input_tokens`, `output_tokens` from session JSONL
4. Two cache-write tiers handled: `ephemeral_1h_input_tokens` and `ephemeral_5m_input_tokens` (from `cache_creation` dict)
5. `collect()` converts token counts to dollars via price table, writes `CostEntry(source="claude_code")`

**Acceptance:** Smoke test `usage.include=true` returns cost in `response_metadata`. `_invoke_and_record` wraps all 9 sites without breaking existing behavior. Claude Code collector produces reasonable cost estimates for real sessions.

---

### T8: UI — Cost Display + Budget Configuration

**Blocked by:** T5 (paused_by generalization), T6 (API endpoints), T7 (real cost data)  
**Blocks:** Nothing (terminal task)  
**Files:** `frontend/src/components/ProjectSettingsModal.tsx`, `frontend/src/components/autopilot/DesignQueuePanel.tsx`  
**Estimated effort:** Small  

**Changes:**
1. **ProjectSettingsModal.tsx**: Add `cost_limit_usd` number input field (optional, blank = no limit). Wire to existing `PUT /projects/{id}` mutation.
2. **DesignQueuePanel.tsx** (or `PipelineStatusCard.tsx`): Add cost indicator showing `$current / $limit` (or just `$current spent` when no limit). Link opens ProjectSettingsModal.
3. **Workflow status badges**: When workflow `paused_by == "budget"`, show "Paused: budget limit reached" instead of generic "Paused".
4. **ProjectItem type**: Add `cost_total_usd` and `cost_limit_usd` to TypeScript interface.

**Acceptance:** Cost indicator visible on design screen. Budget input works in settings. Budget-paused workflow shows distinct label. No limit set = no indicator clutter.

---

### T9: Pi Extension

**Blocked by:** T1 (CostEntry table)  
**Blocks:** T10  
**Files:** `extensions/hephaestus-cost-tracker/package.json`, `extensions/hephaestus-cost-tracker/index.ts`, `extensions/hephaestus-cost-tracker/tsconfig.json` (all new)  
**Estimated effort:** Small-Medium  

**Changes:**
1. Create extension structure at `~/.pi/agent/extensions/hephaestus-cost-tracker/`
2. Hook `turn_end` event: read `message.usage.cost.total` from turn usage data
3. POST to `HEPHAESTUS_API_URL/api/cost-entries` (from `/cost-entries` endpoint in T6)
4. Read `session_id` from `ctx.sessionManager` to tag entries
5. Show running cost via `ctx.ui.setStatus("Cost: $X.XX")`
6. Error handling: fail silently, log warning (JSONL tailing fallback active)

**Extension package.json:**
```json
{
  "name": "hephaestus-cost-tracker",
  "version": "1.0.0",
  "main": "index.js",
  "pi": {
    "name": "Hephaestus Cost Tracker",
    "description": "Reports LLM cost to Hephaestus API in real-time"
  }
}
```

**Acceptance:** Extension loads without errors. Cost entry appears on Hephaestus API after a pi turn. Running cost displays in pi TUI status bar.

---

### T10: Task Completion Wiring

**Blocked by:** T3 (collector module), T6 (API endpoint), T9 (extension preferred but not blocking)  
**Blocks:** Nothing (terminal task)  
**Files:** `src/services/task_completion_service.py`  
**Estimated effort:** Small  

**Changes:**
1. At the point where `update_task_status(done)` handler performs end-of-task bookkeeping, add:
   ```python
   try:
       from src.services.cost_collection_service import collect_task_cost
       collect_task_cost(task_id)
   except Exception as e:
       logger.warning(f"Cost collection failed for task {task_id[:8]}: {e}")
   ```
2. Import is inside try block so cost collection failure never blocks task completion.
3. `collect_task_cost()` is the single entry point from T3 that handles session discovery, checkpoint, and delegation to the right collector.

**Acceptance:** Task completion triggers cost collection. CostEntry rows appear in DB after task done. Collection failure doesn't block task completion.

---

### T11: Codex Stub

**Blocked by:** Nothing  
**Blocks:** Nothing  
**Files:** `src/services/cost_collection_service.py`  
**Estimated effort:** Minimal  

**Changes:**
1. `CodexStubCollector.collect()`: log warning "Codex cost collection not yet supported", return empty list
2. Wire into collector dispatch in `collect_task_cost()`: when `cli_type == "codex"`, use `CodexStubCollector`

**Acceptance:** No zero-cost CostEntry rows created for codex sessions. Warning logged.

---

### T12: OpenCode Collector (Gated)

**Blocked by:** Nothing (but gated on workflow.yaml check)  
**Blocks:** Nothing  
**Files:** `src/services/cost_collection_service.py`  
**Estimated effort:** Small (if in use)  

**Gate check first:** Read `config/workflows/autopilot/workflow.yaml` and `phase_cli_tool` overrides. If `cli_type: opencode` is not set on any live phase, defer indefinitely.

**If in use:**
1. Smoke test `opencode run --format json "hi"` to verify payload shape
2. Implement `OpenCodeCollector` capturing from process stdout
3. One-shot collection after process exits (no checkpoint needed)

**If not in use:** Note in code comments, skip implementation.

**Acceptance (if built):** Cost captured from opencode run. No checkpoint mechanism needed.

---

## 5. Migration Order

All in `src/core/database.py`, called from `__init__` after existing migrations:

```
1413: self._migrate_workflow_paused_retry_count_column()  # existing
1414: self._migrate_task_action_target_phase_column()     # existing
1415: self._migrate_cost_tables()                          # NEW
1416: self._migrate_task_cost_column()                     # NEW
1417: self._migrate_feature_cost_column()                  # NEW
1418: self._migrate_design_cost_column()                   # NEW
1419: self._migrate_project_cost_columns()                 # NEW (cost_total_usd + cost_limit_usd)
```

---

## 6. Acceptance Criteria (from requirements_analysis.md §10)

| ID | Criterion | Verified By |
|----|-----------|-------------|
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

## 7. Risk Mitigations

| Risk | Mitigation |
|------|------------|
| Claude Code price table stale | Document update process; fallback to zero with warning log |
| Pi extension not loaded | JSONL tailing fallback in `collect_task_cost()` |
| OpenRouter `usage.include=true` doesn't surface | Smoke test before building; fallback to token-only estimation |
| Concurrent CostEntry writes | WAL mode + natural idempotency of `_pause_project_workflows` |
| Budget enforcement misses edge case | Comprehensive tests for pause/resume paths |
| Historical data gap | Non-goal: rollups start from deploy time |

---

## 8. Implementation Sequence (Recommended)

| Order | Task | Depends On | Effort |
|-------|------|------------|--------|
| 1 | T1: Schema | — | Small |
| 2 | T2: Cost Derivation | T1 | Medium |
| 3 | T3: Pi JSONL Collector | T1, T2 | Medium |
| 4 | T4: Budget Enforcement | T2, T3 | Medium |
| 5 | T5: paused_by Generalization | T4 | Small |
| 6 | T6: API Endpoints | T5 | Small |
| 7 | T9: Pi Extension | T1 | Small-Medium |
| 8 | T10: Task Completion Wiring | T3, T6, T9 | Small |
| 9 | T7: OpenRouter Direct + Claude Code | T2 | Medium-Large |
| 10 | T8: UI | T5, T6, T7 | Small |
| 11 | T11: Codex Stub | — | Minimal |
| 12 | T12: OpenCode (gated) | — | Small |

T11 can land anytime. T12 depends on workflow.yaml check. Everything else follows the linear path above — the pi collector (T3) lands first so budget enforcement (T4) has real data to test against.