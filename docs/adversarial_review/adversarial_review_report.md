# Adversarial Review — Cost Derivation Engine

**Reviewer:** Hephaestus Adversarial Agent (Phase 6)  
**Date:** 2025-01-27  
**Scope:** All files changed in the Cost Derivation Engine pipeline run  
**Philosophy:** Assume the code is broken. Reason backward from production disasters.

---

## BLOCKER-1: `update_project` silently wipes budget limit on ANY partial update

**Severity:** BLOCKER — Data Poisoning  
**File:** `src/mcp/autopilot_api.py:1838-1842`

### Failure Sequence
1. Admin sets project budget to $50 via `PUT /projects/{id}` with `{"cost_limit_usd": 50}`
2. Later, user renames the project via `PUT /projects/{id}` with `{"name": "New Name"}` — does NOT re-send `cost_limit_usd`
3. Pydantic defaults `cost_limit_usd` to `None` (the field is `Optional[float] = None`)
4. Code path:
   ```python
   if req.cost_limit_usd is not None:        # False — None from default
       proj.cost_limit_usd = req.cost_limit_usd
   elif hasattr(req, "cost_limit_usd") and req.cost_limit_usd is None:  # True
       proj.cost_limit_usd = None              # ← WIPES THE BUDGET
   ```
5. Since `cost_limit_usd` is now `None`, the budget-clear block fires:
   ```python
   if proj.cost_limit_usd is None or proj.cost_total_usd < proj.cost_limit_usd:
       # All budget-paused workflows get RESUMED
   ```
6. **Result:** Budget limit silently wiped; all budget-paused workflows resume with unlimited spending.

### Impact
- Any `PUT /projects/{id}` that doesn't explicitly re-send the current `cost_limit_usd` value will destroy the budget.
- The `ProjectSettingsModal` frontend component has no budget editing UI at all — it only shows cost, it doesn't send `cost_limit_usd` on name changes.
- Silent retroactive data poisoning: the budget the admin relied on is gone with no audit trail.

### Fix
Use a sentinel value or explicit field tracking to distinguish "not sent" from "sent as null":

```python
# Option A: Only clear if the field was explicitly included in the request
class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    base_dir: Optional[str] = None
    is_default: Optional[bool] = None
    cost_limit_usd: Optional[float] = None
    clear_cost_limit: bool = False  # Explicit signal to clear

# In handler:
if req.clear_cost_limit:
    proj.cost_limit_usd = None
elif req.cost_limit_usd is not None:
    proj.cost_limit_usd = req.cost_limit_usd
# else: leave unchanged
```

Or use Pydantic's `exclude_unset=True` to check if the field was actually sent.

---

## BLOCKER-2: Pi extension posts to wrong URL — cost entries never arrive

**Severity:** BLOCKER — Silent Failure  
**File:** `extensions/hephaestus-cost-tracker/src/index.ts:101`

### Failure Sequence
1. Pi extension `postCost()` constructs URL: `` `${this.apiUrl}/cost-entries` ``
2. Default `apiUrl` is `http://localhost:8000` (line 38)
3. Actual server runs on port **8300** (`src/autopilot/orchestrator.py:59`, `src/mcp/server.py:138`)
4. Even if port were correct, the route is `/api/autopilot/cost-entries` (router prefix is `/api/autopilot`)
5. Extension posts to `http://localhost:8000/cost-entries` → connection refused (wrong port) or 404 (wrong path)
6. The `catch` handler just logs a warning and silently continues (line 94)
7. **Result:** Zero real-time cost entries are ever recorded. Cost tracking is completely dead for the Pi extension path.

### Impact
- Real-time cost tracking from the Pi extension is non-functional.
- All cost data depends entirely on the post-hoc `collect_task_cost` path, which has its own issues (see WARNING-1).
- Dashboard shows $0.00 for all costs when the extension is the only cost source.

### Fix
```typescript
// index.ts
this.apiUrl = process.env.HEPHAESTUS_API_URL || 'http://localhost:8300';

// postCost():
const url = `${this.apiUrl}/api/autopilot/cost-entries`;
```

---

## BLOCKER-3: `derive_project_cost` and `derive_design_cost` miss costs on workflows without `feature_id`

**Severity:** BLOCKER — Silent Undercounting  
**File:** `src/core/cost_derivation.py:178-195, 143-161`

### Failure Sequence
1. `derive_project_cost` joins: `CostEntry → Workflow → Feature → AutopilotDesign → project_id`
2. `Workflow.feature_id` is **nullable** — Phase 0 workflows and undecomposed workflows have `feature_id = NULL`
3. The join `Workflow.feature_id == Feature.id` produces NULL for these rows → they're excluded from the SUM
4. Phase 0 (decomposition) can consume significant LLM cost (up to 1 hour timeout)
5. **Result:** Project cost_total_usd is permanently undercounted by the cost of all Phase 0 workflows and any workflow without a feature.

### Impact
- Budget enforcement (`_check_budget_enforcement`) reads from `project.cost_total_usd`, which is derived from this undercounted sum.
- A project could spend 2x its budget in Phase 0 costs and never trigger the budget pause.
- Same issue affects `derive_design_cost` — designs with direct workflow costs (no feature intermediary) are invisible.

### Fix
Add an alternative join path for workflows linked directly to the design/project:

```python
def derive_project_cost(db: Session, project_id: str, write_back: bool = True) -> float:
    # Primary path: through Feature
    via_feature = (
        db.query(func.sum(CostEntry.cost_usd))
        .join(Workflow, CostEntry.workflow_id == Workflow.id)
        .join(Feature, Workflow.feature_id == Feature.id)
        .join(AutopilotDesign, Feature.design_id == AutopilotDesign.id)
        .filter(AutopilotDesign.project_id == project_id)
        .scalar() or 0.0
    )
    # Direct path: workflows linked to project without feature
    direct = (
        db.query(func.sum(CostEntry.cost_usd))
        .join(Workflow, CostEntry.workflow_id == Workflow.id)
        .filter(Workflow.project_id == project_id, Workflow.feature_id.is_(None))
        .scalar() or 0.0
    )
    total = via_feature + direct
    # ... rest of self-heal
```

---

## BLOCKER-4: `CostEntryCreate` missing token count validators — tests WILL FAIL

**Severity:** BLOCKER — Test Failure  
**File:** `src/mcp/autopilot_api.py:1523-1555` vs `tests/test_cost_tracking.py:388-414`

### Failure Sequence
1. `test_reject_negative_token_counts` expects `ValidationError` with message `"token counts must be non-negative"` when `input_tokens=-100`
2. `test_reject_excessive_token_counts` expects `ValidationError` with message `"token count exceeds maximum"` when `input_tokens=100_000_000`
3. The `CostEntryCreate` model has **no validators** for any token count fields — `input_tokens: int = 0` accepts any integer
4. Tests will fail with no ValidationError raised.

### Impact
- Tests that claim to validate token count bounds are testing phantom validation.
- A malicious or corrupt caller can send negative token counts (deflating cost display) or absurd values (polluting the ledger).
- CI should catch this, but the tests were committed without being run.

### Fix
Add validators to `CostEntryCreate`:

```python
@validator("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
def validate_token_count(cls, v):
    if v < 0:
        raise ValueError("token counts must be non-negative")
    if v > 10_000_000:
        raise ValueError("token count exceeds maximum")
    return v
```

---

## BLOCKER-5: `record_cost` bypasses the $1000 cost cap validation

**Severity:** BLOCKER — Data Integrity  
**File:** `src/core/cost_derivation.py:31-100`

### Failure Sequence
1. `CostEntryCreate` Pydantic model validates `cost_usd <= 1000.0` (line 1549)
2. The `/cost-entries` API endpoint uses this model — so API callers are validated.
3. But `collect_task_cost` calls `record_cost()` **directly**, bypassing the Pydantic model entirely.
4. `PiJsonlCollector` reads `cost_usd` from the session file's `usage.cost.total` field with **no validation**.
5. A corrupt session file with `cost.total = 999999.0` writes directly to the DB.
6. **Result:** Retroactive data poisoning — a single corrupt entry can inflate project costs by $999K, triggering false budget enforcement or corrupting cost displays.

### Impact
- Any path that calls `record_cost()` directly (not through the API) bypasses the $1000 cap.
- The `cost_collection_service` is the primary production caller and it validates nothing.
- A single corrupt JSONL line can poison the entire cost hierarchy.

### Fix
Add validation inside `record_cost()` itself — it's the single source of truth:

```python
def record_cost(db: Session, cost_usd: float, ...) -> CostEntry:
    if cost_usd < 0:
        raise ValueError("cost_usd must be non-negative")
    if cost_usd > 1000.0:
        logger.warning(f"[COST] Capping unusually high cost ${cost_usd:.2f} to $1000")
        cost_usd = 1000.0
    # ... rest of function
```

---

## WARNING-1: `_extract_session_id` is a heuristic guess, not reliable extraction

**Severity:** WARNING — Silent Data Loss  
**File:** `src/services/cost_collection_service.py:426-441`

### Issue
The function tries to parse the session ID from the tmux session name using a format assumption (`hephaestus-<project>-<design>-<role>-<suffix>`), then returns everything after the first dash. The comment admits: *"For now, return None and log"*.

If extraction fails (which it will for any non-standard session name), `collect_task_cost` returns early with a debug log — silently skipping cost collection for that task.

### Recommended Fix
Store the session ID in the Agent model at launch time (it's already passed via `--session-id` flag). Read it back from `agent.session_id` instead of parsing the tmux name.

---

## WARNING-2: `ClaudeCodeCollector` hardcoded prices with no staleness detection

**Severity:** WARNING — Incorrect Costs  
**File:** `src/services/cost_collection_service.py:108-122`

### Issue
The price table is hardcoded with the comment "Update these when Anthropic reprices." There's no:
- Version field or "as of" date
- Staleness warning when prices are >N months old
- Per-entry price version tracking

If Anthropic reprices, ALL historical entries will retroactively use new prices for old data, or vice versa depending on when the code was updated.

### Recommended Fix
Record the price table version in each CostEntry's `raw_usage` field, and add a config-driven price table that can be updated without code changes.

---

## WARNING-3: `collect_task_cost` error handling silently swallows failures

**Severity:** WARNING — Silent Data Loss  
**File:** `src/services/task_completion_service.py:842-847`

### Issue
```python
try:
    collect_task_cost(task_id)
except Exception as e:
    logger.warning(f"Cost collection failed for task {task_id[:8]}: {e}")
```

This bare `except Exception` swallows ALL errors — including database corruption, permission errors, and programming bugs. No metric is incremented, no retry is attempted, no task is flagged.

### Recommended Fix
At minimum, log the full traceback. Consider retrying on transient errors (disk I/O, database locked).

---

## WARNING-4: `BudgetPausedLabel` component exists but is never rendered

**Severity:** WARNING — Dead Code  
**File:** `frontend/src/components/cost/BudgetPausedLabel.tsx`

### Issue
`BudgetPausedLabel` is defined and exported from the cost barrel file, but the Dashboard and all other views never render it. A workflow paused by budget shows the generic "paused" status with no indication that it's budget-related.

### Recommended Fix
Integrate `BudgetPausedLabel` into the workflow/feature status display when `workflow.paused_by === "budget"`.

---

## WARNING-5: Dashboard `ProjectCostSummary` has no `onConfigureBudget` handler

**Severity:** WARNING — Missing Functionality  
**File:** `frontend/src/pages/Dashboard.tsx:280-293`

### Issue
The `ProjectCostSummary` component accepts `onConfigureBudget` prop (shows a settings gear icon), but the Dashboard never passes it. Users see the cost summary but have no way to configure the budget from the Dashboard.

### Recommended Fix
Wire `onConfigureBudget` to open `ProjectSettingsModal` or a dedicated budget dialog.

---

## WARNING-6: `derive_workflow_cost` rollup triggers redundant re-derivation

**Severity:** WARNING — Performance  
**File:** `src/core/cost_derivation.py:125-141`

### Issue
When `record_cost` is called, the chain is:
1. `derive_task_cost(db, task_id)` — queries SUM(cost_entries) for task
2. `derive_workflow_cost(db, workflow_id)` — queries SUM(cost_entries) for workflow (redundant — same entries)
3. `derive_feature_cost(db, feature_id)` — queries SUM via join
4. `derive_design_cost(db, design_id)` — queries SUM via join  
5. `derive_project_cost(db, project_id)` — queries SUM via join

Each function independently queries the database. For a single `record_cost` call, this is 5 separate SUM queries. Under high throughput (many concurrent cost entries), this could create database contention.

### Recommended Fix
Consider batching: derive top-down (project → design → feature → workflow → task) and skip re-derivation if the child's cost hasn't changed.

---

## NIT-1: `CostEntry.id` uses `cost-<uuid8>` — collision risk

**Severity:** NIT  
**File:** `src/core/cost_derivation.py:68`

### Issue
`f"cost-{uuid.uuid4().hex[:8]}"` gives 32 bits of randomness. At scale (millions of cost entries), birthday paradox gives ~50% collision probability around 65K entries. The same pattern appears in both `record_cost` and the collectors.

### Recommended Fix
Use the full UUID or at least 12 hex chars (48 bits).

---

## NIT-2: `FeatureCostBadge` threshold is arbitrary

**Severity:** NIT  
**File:** `frontend/src/components/cost/FeatureCostBadge.tsx:28`

### Issue
`cost >= 5` triggers red color, `cost < 5` shows blue. This threshold is hardcoded and not configurable. For high-budget projects, $5 is trivial; for small projects, it's significant.

### Recommended Fix
Make the threshold proportional to the project's budget, or make it configurable via props.

---

## NIT-3: Inconsistent cost formatting across components

**Severity:** NIT  
**File:** Multiple frontend files

### Issue
- `CostDisplay` formats: `$X.XX` (2 decimals), `$X.Xk` (1 decimal for ≥1000)
- `FeatureCostBadge` formats: `$X.XX` (2 decimals), `$X.X` (1 decimal for ≥10), `$X` (0 decimals for ≥100)
- `ProjectCostSummary` uses `CostDisplay` formatting

The inconsistency means the same cost value displays differently in different places.

### Recommended Fix
Create a shared `formatCost` utility function used by all components.

---

## Summary

| Severity | Count |
|----------|-------|
| BLOCKER  | 5     |
| WARNING  | 6     |
| NIT      | 3     |

**Verdict:** The cost derivation engine has 5 BLOCKERs that must be fixed before merge. The most critical is BLOCKER-1 (silent budget wipe on any project update) which will cause data loss in production. BLOCKER-2 (wrong URL/port) means real-time cost tracking is completely non-functional. BLOCKER-3 (missing join path) means cost totals are systematically undercounted.
