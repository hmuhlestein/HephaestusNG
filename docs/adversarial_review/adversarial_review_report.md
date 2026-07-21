# Adversarial Review Report — Cost Tracking Database Schema

**Reviewer**: Hephaestus Adversarial Review Agent  
**Date**: 2025-07-21  
**Scope**: `src/core/cost_derivation.py`, `src/core/database.py` (CostEntry, SessionCostCheckpoint, cost columns), `src/services/cost_collection_service.py`, `src/services/task_completion_service.py` (collect_cost_on_completion), `src/mcp/autopilot_api.py` (cost-entries endpoint, budget clearing)

---

## BLOCKER Findings

### B-1: Cascading `db.commit()` calls cause partial state on failure

**File**: `src/core/cost_derivation.py` — `record_cost()` → `derive_task_cost()` → `derive_workflow_cost()` → `derive_feature_cost()` → `derive_design_cost()` → `derive_project_cost()`

**Failure sequence**:
1. `record_cost()` creates a `CostEntry`, calls `db.flush()` (line 97)
2. `derive_task_cost()` is called — if cost disagrees, it calls `db.commit()` (line 138)
3. `derive_workflow_cost()` is called, which cascades to `derive_feature_cost()` → `derive_design_cost()` → `derive_project_cost()`, each independently calling `db.commit()` when costs disagree
4. If `derive_project_cost()` raises an exception (e.g., database locked, integrity error), the task and feature costs are **already committed** — but the project cost is stale
5. The caller (`get_db()` context manager) catches the exception and calls `db.rollback()`, but the earlier commits are already persisted in separate transactions

**Impact**: Cost hierarchy becomes inconsistent — task says $0.10, feature says $0.10, design says $0.00 (never updated because the chain broke). Self-healing will eventually fix it on next read, but until then, the UI shows contradictory numbers and budget enforcement may use stale data.

**Recommended fix**: Remove all `db.commit()` calls from derive functions. Let the caller (`get_db()` context manager or `session_scope()`) handle the single commit. The derive functions should only mutate in-session state; the caller decides when to persist.

---

### B-2: `_pause_project_workflows` queries ALL agents globally, not filtered by project

**File**: `src/core/cost_derivation.py` lines 333-348

**Code**:
```python
active_agents = (
    db.query(Agent)
    .filter(
        Agent.current_task_id.isnot(None),
        Agent.status.in_(["working", "idle"]),
    )
    .all()
)
```

**Failure sequence**:
1. Project A goes over budget
2. `_pause_project_workflows` is called for project A
3. The query fetches **every active agent in the entire system** (not just project A's agents)
4. For each workflow in project A, it iterates ALL agents and checks if their task belongs to the workflow
5. With 50 agents and 10 workflows, this is 500 DB queries (N+1 pattern)

**Impact**: 
- **Performance bomb**: O(workflows × total_agents) queries instead of a single filtered query
- **Correctness**: Currently safe because of the `task.workflow_id == wf.id` check, but the pattern is fragile — if the inner query is ever optimized away or the check is accidentally removed, agents from other projects would be terminated

**Recommended fix**: Replace with a single query joining Agent → Task → Workflow filtered by project_id:
```python
agents_to_terminate = (
    db.query(Agent)
    .join(Task, Agent.current_task_id == Task.id)
    .filter(
        Task.workflow_id.in_([wf.id for wf in active_workflows]),
        Agent.status.in_(["working", "idle"]),
    )
    .all()
)
```

---

### B-3: Budget-unpause logic bug — `cost_total_usd == 0.0` short-circuits

**File**: `src/mcp/autopilot_api.py` line 1976

**Code**:
```python
if proj.cost_limit_usd is None or (proj.cost_total_usd and proj.cost_total_usd < proj.cost_limit_usd):
```

**Failure sequence**:
1. Project has `cost_total_usd = 0.0` (the default) and `cost_limit_usd = 50.0`
2. Project goes over budget → workflows are paused with `paused_by = "budget"`
3. Admin raises the limit to $100.0 via the API
4. The condition evaluates: `proj.cost_total_usd` is `0.0` (falsy) → `0.0 and ...` short-circuits to `0.0` (falsy)
5. `False or 0.0` = `0.0` (falsy) → the un-pause block is **never entered**
6. Workflows remain permanently paused even though the budget was raised

**Impact**: Projects that haven't spent anything yet ($0.00 cost) can never have their budget-paused workflows unpaused via the API. The only workaround is to manually set `cost_total_usd` to a non-zero value first.

**Recommended fix**:
```python
if proj.cost_limit_usd is None or proj.cost_total_usd < proj.cost_limit_usd:
```

---

### B-4: `_get_agent_cwd` opens nested `get_db()` sessions inside caller's session

**File**: `src/services/cost_collection_service.py` lines 551-573

**Code**:
```python
def _get_agent_cwd(agent: Any, task: Any) -> Optional[str]:
    if task.workflow_id:
        from src.core.database import get_db
        with get_db() as db:  # NEW session
            wf = db.query(Workflow).filter_by(id=task.workflow_id).first()
            ...
    from src.core.database import get_db
    with get_db() as db:  # ANOTHER new session
        worktree = db.query(AgentWorktree).filter_by(agent_id=agent.id).first()
        ...
```

**Failure sequence**:
1. `collect_task_cost()` opens a `get_db()` session (line 395)
2. Inside that session, it calls `_get_agent_cwd(agent, task)`
3. `_get_agent_cwd` opens **two more** `get_db()` sessions (lines 562, 569)
4. These nested sessions are independent transactions — they may see different data (stale workflow, race-deleted worktree)
5. The outer session's `agent` and `task` objects were loaded in a different transaction scope

**Impact**: 
- **Connection leak**: Each `get_db()` call acquires a connection from the pool. With nested calls, the pool can be exhausted under load
- **Inconsistent reads**: The inner sessions may see committed data that the outer session doesn't (or vice versa), leading to the CWD being derived from stale data
- **SQLite-specific**: In WAL mode, readers can see different snapshots; the inner session might read a workflow that was just deleted by another thread

**Recommended fix**: Pass the existing `db` session to `_get_agent_cwd` instead of opening new ones:
```python
def _get_agent_cwd(db: Session, agent: Any, task: Any) -> Optional[str]:
    if task.workflow_id:
        wf = db.query(Workflow).filter_by(id=task.workflow_id).first()
        if wf and wf.working_directory:
            return wf.working_directory
    worktree = db.query(AgentWorktree).filter_by(agent_id=agent.id).first()
    if worktree:
        return worktree.worktree_path
    return None
```

---

### B-5: `derive_workflow_cost` doesn't persist workflow cost — no `cost_total_usd` column on Workflow

**File**: `src/core/cost_derivation.py` lines 143-175

**Observation**: `derive_workflow_cost()` computes `total` from cost_entries, then rolls up to feature/design/project via their `cost_total_usd` columns. But the `Workflow` model in `database.py` has **no `cost_total_usd` column**. The function returns the value but never persists it.

**Impact**:
- Workflow-level cost is only available by recomputing from entries every time — no fast lookup
- No self-healing at the workflow level (every other entity in the hierarchy has it)
- The API has no endpoint to query per-workflow cost without scanning all entries
- If a caller reads the return value and assumes it's persisted (like all the other derive functions do), it will be surprised

**Recommended fix**: Add `cost_total_usd = Column(Float, default=0.0, nullable=False)` to the `Workflow` model and write back in `derive_workflow_cost`, consistent with the pattern used by Task/Feature/Design/Project.

---

## WARNING Findings

### W-1: Budget enforcement TOCTOU race condition

**File**: `src/autopilot/orchestrator.py` lines 2018-2023 and `src/core/cost_derivation.py` `check_budget_before_new_work()`

**Scenario**: `check_budget_before_new_work()` is called in one session, returns `True` (under budget). Between this check and the actual workflow launch, another concurrent workflow completes and records costs that push the project over budget. The launch proceeds despite being over budget.

**Impact**: Budget limits are advisory, not enforced. A burst of completions can overshoot the limit by the cost of one full workflow.

**Mitigation**: Acceptable if documented as "best-effort" budget. If hard enforcement is needed, the check and launch must be in the same transaction with a `SELECT ... FOR UPDATE` equivalent (or SQLite's `BEGIN IMMEDIATE`).

---

### W-2: `collect_task_cost` silently swallows all failures

**File**: `src/services/task_completion_service.py` lines 832-840

**Code**:
```python
@staticmethod
def collect_cost_on_completion(task_id: str) -> None:
    try:
        from src.services.cost_collection_service import collect_task_cost
        collect_task_cost(task_id)
    except Exception as e:
        logger.warning(f"Cost collection failed for task {task_id[:8]}: {e}")
```

**Impact**: If cost collection consistently fails (e.g., session file format changed, path convention changed, permission denied), every task will have $0 cost with only a `WARNING` log line. No alerting, no metric, no way to know costs aren't being tracked until someone checks the UI.

**Recommended fix**: Add a metric/counter for cost collection failures. After N consecutive failures, escalate to ERROR or emit an alert.

---

### W-3: Cost entries can be duplicated on crash recovery

**File**: `src/services/cost_collection_service.py` lines 487-505

**Scenario**:
1. `collect_task_cost` reads 5 new lines from the session file
2. Creates 5 `CostEntry` rows and calls `record_cost` for each (which triggers derive chain)
3. Process crashes (OOM, kill -9) before reaching the `db.commit()` at line 505
4. On restart, `SessionCostCheckpoint` still shows the old `lines_processed`
5. The same 5 lines are re-read and 5 duplicate `CostEntry` rows are created

**Impact**: Cost data is doubled for the affected session. Self-healing will eventually correct the rollup totals (since they're derived from entries), but the ledger itself has permanent duplicates with no deduplication mechanism.

**Recommended fix**: Use the `CostEntry.id` (derived from session_id + line number, not random UUID) as a natural key, and `INSERT OR IGNORE` / merge on conflict.

---

### W-4: `derive_project_cost` commits THEN checks budget — ordering issue

**File**: `src/core/cost_derivation.py` lines 266-270

**Code**:
```python
project.cost_total_usd = total
db.commit()  # Cost is now visible to other transactions

# Check budget enforcement AFTER commit
_check_budget_enforcement(db, project)
```

**Scenario**: Two concurrent workflows both complete and call `derive_project_cost`. Both compute the project total, both commit, then both check budget. If the combined total exceeds the limit, neither may trigger the pause (depending on timing and what each computed).

**Impact**: Budget enforcement can miss the over-budget condition in concurrent scenarios.

**Recommended fix**: Check budget BEFORE committing, or use a single transaction that atomically updates cost and checks budget.

---

### W-5: `CostEntry` IDs are random — no idempotency on re-collection

**File**: `src/services/cost_collection_service.py` line 83

**Code**:
```python
"id": f"cost-{uuid.uuid4().hex[:8]}",
```

**Impact**: If `collect_task_cost` is called twice for the same task (e.g., manual retry, race), each call generates new random IDs for the same underlying usage data. No mechanism to detect or prevent duplicates.

**Recommended fix**: Derive the ID from (session_id, line_number, task_id) to make re-collection idempotent.

---

## NIT Findings

### N-1: `ClaudeCodeCollector.PRICES` will silently go stale

**File**: `src/services/cost_collection_service.py` lines 118-135

The price table is hardcoded with today's prices. When Anthropic reprices:
- All new cost calculations will use wrong prices until the code is updated
- Historical entries already in the DB have the old prices (no versioning)
- No date-range awareness — a session spanning a price change will use one price for all entries

---

### N-2: `_extract_session_id` is fragile string parsing

**File**: `src/services/cost_collection_service.py` lines 525-540

The function splits `tmux_session_name` on `"-"` and takes everything after the first part. If the naming convention changes (e.g., project names with hyphens), this will silently return wrong session IDs, causing costs to not be collected for affected agents.

---

### N-3: `derive_design_cost` only counts costs through Feature→Workflow chain

**File**: `src/core/cost_derivation.py` lines 218-226

The query joins `CostEntry → Workflow → Feature` to sum costs. But if a workflow has `design_id` set but `feature_id` is NULL (e.g., phase0 workflows), those costs are **not counted** in the design total. The design cost only reflects feature-associated workflows.

---

### N-4: `_check_budget_enforcement` imports `Agent` and `Task` inside the function body

**File**: `src/core/cost_derivation.py` lines 310, 337

These imports are inside the function to avoid circular imports, but they're executed on every call. If this function is called frequently (e.g., on every cost recording), the import overhead adds up. Consider lazy module-level imports or restructuring to avoid the circular dependency.

---

## Summary

| Severity | Count |
|----------|-------|
| BLOCKER  | 5     |
| WARNING  | 5     |
| NIT      | 4     |

**Most critical**: B-1 (cascading commits) and B-3 (budget-unpause logic bug) are the highest-impact findings. B-1 can cause data inconsistency that persists until the next self-heal cycle. B-3 permanently locks projects that haven't spent anything yet.

**Architecture concern**: The cost derivation module uses `db.commit()` inside utility functions that are called as part of larger operations. This violates the principle that transaction boundaries should be controlled by the caller, not the callee. Every derive function should be a pure in-session mutation; the caller decides when to commit.
