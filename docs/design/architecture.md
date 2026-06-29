# Feature Model Implementation — Technical Architecture

**Feature ID:** feature-model-implementation
**Status:** Architecture Complete
**Date:** 2026-06-29
**Design Document:** `design_docs/feature_model_implementation.md`
**Requirements:** `docs/requirements_analysis.md`

---

## 1. System Architecture Overview

### 1.1 Current Architecture (Before)

```
Design Document
    │
    ▼
Autopilot Orchestrator (run_single_design)
    │
    ▼
Single Workflow (11 phases, one agent per phase)
    │
    ▼
Feature Report (HTML) → Human Review
```

**Problem:** One enormous workflow tries to build everything at once. Context window overflows, agents lose track of scope, failures in one area block everything else.

### 1.2 Target Architecture (After)

```
                                    ┌──────────────────────┐
                                    │    Design Document    │
                                    │   (file_path in DB)   │
                                    └──────────┬───────────┘
                                               │
                                               ▼
                                    ┌──────────────────────┐
                                    │  Phase 0: Feature     │
                                    │  Architect Workflow   │
                                    │  (autopilot-phase0)   │
                                    └──────────┬───────────┘
                                               │
                                               ▼
                                    ┌──────────────────────┐
                                    │    features.json      │
                                    │  + scope.md per feat  │
                                    └──────────┬───────────┘
                                               │
                              ┌─────────────────┼─────────────────┐
                              ▼                 ▼                 ▼
                    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
                    │  Feature A   │  │  Feature B   │  │  Feature C   │
                    │  (parallel)  │  │  (parallel)  │  │  (sequential)│
                    │  12-phase    │  │  12-phase    │  │  waits for A │
                    │  pipeline    │  │  pipeline    │  │  12-phase    │
                    └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
                           │                 │                 │
                           └─────────────────┼─────────────────┘
                                             ▼
                                    ┌──────────────────────┐
                                    │  Design Aggregate     │
                                    │  (orchestrator, no    │
                                    │   agent)              │
                                    └──────────┬───────────┘
                                               │
                                               ▼
                                    ┌──────────────────────┐
                                    │  design_report.html   │
                                    │  design_metrics.json  │
                                    └──────────────────────┘
```

### 1.3 Key Architectural Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Feature decomposition timing | Before any code (Phase 0) | Agents get focused scope, avoiding context overflow |
| Worktree isolation | Per-feature integration worktree (§9.6 model) | Each feature's pipeline runs in its own git worktree branched from main |
| Parallelism control | Topological sort (Kahn's algorithm) with `depends_on` + `execution` fields | Deterministic ordering; parallel features at same topological depth |
| Max parallel features | 4 (ThreadPoolExecutor) | Balances resource usage with throughput |
| Permanent storage | `designs/<ts>_<name>_<id>/` | Audit trail; survives worktree cleanup |
| Backward compatibility | Old single-workflow path preserved | Designs without Feature model still work |
| Scope as primary input | Phase 0 writes `scope.md` per feature; Phases 1, 2, 3, 10 read it | Smaller context window per agent |

---

## 2. Component Architecture

### 2.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CLI Layer                                   │
│  src/cli/commands/autopilot.py                                      │
│  - add_to_queue: stores file_path in DB (no file copy)             │
│  - start_pipeline, stop_pipeline, pipeline_status, show_queue       │
└────────────────────────────┬────────────────────────────────────────┘
                             │ HTTP
┌────────────────────────────▼────────────────────────────────────────┐
│                         API Layer                                   │
│  src/mcp/autopilot_api.py                                          │
│  - POST /api/autopilot/designs/add (file_path + project_path)      │
│  - GET  /api/autopilot/status                                      │
│  - GET  /api/autopilot/queue                                       │
│  - POST /api/autopilot/queue (add by content, legacy)              │
└────────────────────────────┬────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────────┐
│                     Orchestrator Layer                               │
│  src/autopilot/orchestrator.py                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ run_single_design (three-stage coordinator)                  │   │
│  │  Stage 1: run_phase0 → features.json + designs_folder       │   │
│  │  Stage 2: run_feature_pipelines → per-feature results       │   │
│  │  Stage 3: run_design_aggregate → report + metrics           │   │
│  └──────────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Helper functions:                                            │   │
│  │  _resolve_execution_order (Kahn's algorithm)                 │   │
│  │  _create_integration_worktree, _cleanup_worktree            │   │
│  │  _create_designs_folder, _create_feature_records            │   │
│  │  _update_feature_status, _update_design_status              │   │
│  │  _validate_features_json, _should_skip                      │   │
│  │  _set_workflow_type, _link_workflow_to_feature              │   │
│  └──────────────────────────────────────────────────────────────┘   │
└────────────────────────────┬────────────────────────────────────────┘
                             │
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ Phase 0 WF   │  │ Feature WF A │  │ Feature WF B │
│ (Feature     │  │ (autopilot   │  │ (autopilot   │
│  Architect)  │  │  12-phase)   │  │  12-phase)   │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       ▼                 ▼                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Data Layer                                    │
│  src/core/database.py                                              │
│  Tables: AutopilotDesign, Feature, Workflow, Phase, Task, Agent    │
│  Migrations: _migrate_feature_model_columns()                      │
└─────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Workflow Engine                                   │
│  src/workflow_registry.py                                          │
│  - Auto-discovers from config/workflows/*/workflow.yaml             │
│  - Registers autopilot-phase0 alongside autopilot                  │
│  src/workflow_engine/yaml_loader.py                                │
│  - Parses workflow.yaml + phase YAMLs into WorkflowDefinition      │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 Component Responsibilities

| Component | File | Responsibility |
|-----------|------|----------------|
| CLI | `src/cli/commands/autopilot.py` | User interface for queue management; stores file_path in DB |
| API | `src/mcp/autopilot_api.py` | HTTP endpoints for dashboard and programmatic access |
| Orchestrator | `src/autopilot/orchestrator.py` | Three-stage coordinator; topological sort; parallel execution |
| Database | `src/core/database.py` | Feature model; migration; relationships |
| Workflow Registry | `src/workflow_registry.py` | Auto-discovers workflow definitions |
| Phase 0 YAML | `config/workflows/autopilot-phase0/` | Feature Architect agent prompt and evaluation |
| Design Report | `src/autopilot/templates/design_report.html` | Jinja2 template for aggregate report |
| Spec Gate | `src/autopilot/spec.py` | Evaluator for workflow engine (existing, used by Phase 0) |

---

## 3. Data Model

### 3.1 Entity Relationship Diagram

```
AutopilotProject 1──N AutopilotDesign 1──N Feature N──1 Workflow
                                         │            │
                                         │            │
                                    feature_id    workflow_id
                                    (FK)          (FK)
```

### 3.2 New Table: `Feature`

```python
class Feature(Base):
    __tablename__ = "features"

    id = Column(String, primary_key=True)  # feat-<uuid8>
    design_id = Column(String, ForeignKey("autopilot_designs.id"), nullable=False)
    feature_key = Column(String(100), nullable=False)  # slug from features.json "id" field
    name = Column(String, nullable=False)
    scope = Column(Text, nullable=False)   # one-paragraph summary
    files = Column(JSON, nullable=True)    # list of file paths owned
    depends_on = Column(JSON, nullable=True)  # list of feature_key strings
    execution = Column(String, CheckConstraint("... IN ('parallel', 'sequential')"),
                       nullable=False, default="parallel")
    status = Column(String, CheckConstraint("... IN ('pending','active','completed','failed','skipped')"),
                    nullable=False, default="pending")
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True)
    scope_doc_path = Column(Text, nullable=True)  # abs path to scope.md in permanent record
    feature_record_path = Column(Text, nullable=True)  # abs path to designs/.../features/<key>/
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)

    # Relationships
    design = relationship("AutopilotDesign", back_populates="features")
    workflow = relationship("Workflow", foreign_keys=[workflow_id])
```

### 3.3 Modified Table: `AutopilotDesign`

New columns:
```python
file_path = Column(Text, nullable=True)        # absolute path to design file
designs_folder = Column(Text, nullable=True)   # path to designs/<ts>_<name>_<id>/
phase0_workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True)
```

Extended status constraint:
```
pending | processing | decomposing | active | completed | failed | skipped
```

New relationship:
```python
features = relationship("Feature", back_populates="design", cascade="all, delete-orphan")
```

### 3.4 Modified Table: `Workflow`

New columns:
```python
workflow_type = Column(String, CheckConstraint("... IN ('design', 'feature')"),
                       nullable=True, default="feature")
feature_id = Column(String, ForeignKey("features.id"), nullable=True)
```

New relationship:
```python
feature = relationship("Feature", foreign_keys=[feature_id])
```

### 3.5 `features.json` Schema (Phase 0 Output)

```json
{
  "design_name": "<human name>",
  "features": [
    {
      "id": "<short-slug>",
      "name": "<Human Name>",
      "scope": "<one paragraph>",
      "files": ["src/auth/", "tests/test_auth.py"],
      "depends_on": [],
      "execution": "parallel"
    }
  ]
}
```

**Invariants:**
- All `id` values unique
- All `depends_on` values reference existing `id` values
- No file path overlaps between features
- `execution` is exactly `"parallel"` or `"sequential"`
- 1–5 features per design (simple designs = 1 feature)

---

## 4. Interface Contracts

### 4.1 CLI Interface

**`heph autopilot add <file> --project-path <path>`**

- Resolves `file` to absolute path
- Calls `POST /api/autopilot/designs/add` with `{file_path, project_path}`
- Does NOT copy the file
- Returns: design ID, name, status

### 4.2 API Interface

**`POST /api/autopilot/designs/add`**

Request:
```json
{
  "file_path": "/absolute/path/to/design.md",
  "project_path": "/absolute/path/to/project"
}
```

Response:
```json
{
  "id": "des-abc123",
  "name": "Auth System",
  "status": "pending"
}
```

Behavior:
1. Validates file exists
2. Finds or creates `AutopilotProject` for `project_path`
3. Checks for duplicate `file_path` (returns existing if found)
4. Creates `AutopilotDesign` record with `file_path`, `status=pending`

### 4.3 Phase 0 Workflow Interface

**Input (launch_params):**
```python
{
    "design_document": "/path/to/design.md",
    "project_path": "/path/to/project",
    "design_id": "des-abc123",
}
```

**Output (files in worktree):**
```
.hephaestus/features.json
.hephaestus/features/<id>/scope.md  (one per feature)
```

**Evaluation point:** After `feature_architect` phase, heuristic evaluator checks:
- `features.json` exists and is valid JSON matching schema
- All `scope.md` files exist
- All `depends_on` references resolve
- No file overlaps

### 4.4 Feature Pipeline Interface

**Input (per feature):**
```python
{
    "design_document": "/path/to/design.md",
    "project_path": "/path/to/project",
    "feature_id": "auth",
    "feature_scope": ".hephaestus/features/auth/scope.md",
    "project_context": "Building feature: JWT Authentication. Scope: .hephaestus/features/auth/scope.md",
}
```

**Working directory:** Feature's integration worktree (branched from main)

**Scope document is PRIMARY input** for Phases 1, 2, 3, and 10.

### 4.5 Orchestrator Internal Interface

**`run_single_design(sdk, design_entry, project_path, logger, state, max_iterations)`**

Returns: `(DesignStatus, FeatureReport)`

**`run_phase0(sdk, design_entry, project_path, logger, state)`**

Returns: `(Optional[dict], Optional[Path])` — features.json dict and designs_folder path

**`run_feature_pipelines(sdk, design_entry, features_json, designs_folder, project_path, logger, state, max_iterations)`**

Returns: `dict[str, DesignStatus]` — feature_id → status mapping

**`run_design_aggregate(design_entry, feature_results, designs_folder, logger)`**

Returns: `(DesignStatus, FeatureReport)`

---

## 5. Data Flow

### 5.1 End-to-End Flow

```
1. User: heph autopilot add ~/designs/auth.md --project-path ~/my-project
   → CLI resolves path, calls POST /api/autopilot/designs/add
   → DB: AutopilotDesign record created (status=pending, file_path=~/designs/auth.md)

2. Orchestrator picks next design from queue
   → DB: status → processing
   → DesignEntry created with path=file_path from DB

3. Stage 1: run_phase0
   a. DB: status → decomposing
   b. Create integration worktree (branch: autopilot/run-<id>)
   c. Copy design.md into worktree .hephaestus/
   d. Launch autopilot-phase0 workflow
   e. Poll until complete (timeout: 3600s)
   f. Read + validate features.json
   g. Create permanent designs/<ts>_<name>_<id>/ folder
   h. Copy Phase 0 outputs to permanent storage
   i. Create Feature DB records (status=pending each)
   j. DB: status → active
   k. Discard Phase 0 worktree

4. Stage 2: run_feature_pipelines
   a. _resolve_execution_order(features) → execution_groups
   b. For each group:
      - If single feature: _run_one_feature()
      - If multiple parallel: ThreadPoolExecutor(max_workers=4)
      - Skip features whose dependencies failed
   c. _run_one_feature():
      - DB: Feature status → active
      - Create feature record folder
      - Create per-feature integration worktree
      - Populate .hephaestus/ with design.md, features.json, scope.md
      - Launch autopilot workflow (12-phase)
      - Poll until complete
      - Sweep artifacts to permanent record
      - DB: Feature status → completed/failed/skipped
      - Cleanup worktree

5. Stage 3: run_design_aggregate
   a. Write design_metrics.json
   b. Generate design_report.html (Jinja2)
   c. DB: AutopilotDesign status → completed/failed
   d. Return FeatureReport
```

### 5.2 Topological Sort Algorithm

```
Input: features with depends_on and execution fields
Output: execution_groups (list of lists)

1. Build dependency graph (adjacency + in_degree)
2. Kahn's algorithm:
   a. Queue = features with in_degree == 0
   b. While queue not empty:
      - Collect current layer
      - Separate parallel vs sequential features
      - Parallel features at same depth → one group
      - Sequential features → each in own group
      - Reduce in-degrees of dependents
3. If cycles detected → fall back to fully sequential
4. Log execution plan
```

### 5.3 Permanent Storage Layout

```
<project>/
  designs/
    20260629-143022_auth_system_des-abc1/    ← designs_folder
      design.md                              ← copy of original
      features.json                          ← Phase 0 output
      design_report.html                     ← Stage 3 aggregate
      design_metrics.json                    ← timing + cost
      features/
        auth/
          scope.md
          feature_report.html
          docs/
            requirements_analysis.md
            architecture.md
            review_report.md
            doc_review_report.md
            security_report.md
            qa_report.md
            qa_result.json
            product_validation.md
            product_validation.json
            forensics_report.md
            pipeline_metrics.json
            phase_prompts/
        session/
          scope.md
          ...
```

---

## 6. Infrastructure Requirements

### 6.1 Dependencies

| Dependency | Version | Purpose |
|------------|---------|---------|
| Python 3 | ≥3.9 | Runtime (existing) |
| SQLAlchemy | existing | ORM (existing) |
| Jinja2 | existing | HTML report template |
| Git | existing | Worktree isolation |
| pytest | existing | Testing (74 existing tests) |

**No new external dependencies required.**

### 6.2 Constants

```python
MAX_PHASE0_TIME = 3600      # 1 hour timeout for Phase 0
MAX_PARALLEL_FEATURES = 4   # max concurrent feature pipelines
```

### 6.3 Database Migration

```python
def _migrate_feature_model_columns(engine) -> None:
    """Idempotent — safe to call on every startup."""
    with engine.connect() as conn:
        _add_column_if_missing(conn, "autopilot_designs", "file_path", "TEXT")
        _add_column_if_missing(conn, "autopilot_designs", "designs_folder", "TEXT")
        _add_column_if_missing(conn, "autopilot_designs", "phase0_workflow_id", "TEXT")
        _add_column_if_missing(conn, "workflows", "workflow_type", "TEXT DEFAULT 'feature'")
        _add_column_if_missing(conn, "workflows", "feature_id", "TEXT")
        conn.commit()
    Base.metadata.create_all(engine, tables=[Feature.__table__], checkfirst=True)
```

### 6.4 Workflow Registration

`autopilot-phase0` is auto-discovered by `src/workflow_registry.py` because:
- It lives in `config/workflows/autopilot-phase0/workflow.yaml`
- `get_all_workflow_definitions()` scans all subdirectories of `config/workflows/`

No manual registration code change needed in `workflow_registry.py`.

---

## 7. Task Breakdown with Blocking Relationships

### Task Dependency Graph

```
Task 0 (Run B fixes)
    │
    ├── blocked by nothing (prerequisite for all)
    │
    ▼
Task 1 (DB Schema) ─── blocked by Task 0
    │
    ├── blocked by Task 1
    │
    ▼
Task 2 (Phase 0 YAML) ─── blocked by Task 1
    │
    ├── blocked by Task 2
    │
    ▼
Task 3 (Orchestrator Refactor) ─── blocked by Tasks 1, 2
    │
    ├── blocked by Task 3
    │
    ▼
Task 4 (CLI + API) ─── blocked by Tasks 1, 3
    │
    ├── blocked by Task 4
    │
    ▼
Task 5 (Phase YAML Updates) ─── blocked by Tasks 2, 3
    │
    ├── blocked by Tasks 4, 5
    │
    ▼
Task 6 (Design Report Template) ─── blocked by Task 3
    │
    ├── blocked by Task 6
    │
    ▼
Task 7 (Integration Testing) ─── blocked by all above
```

### Task 0: Run B Fixes (Prerequisite)

**Description:** Fix spec gate not firing on QA completion and abandoned required phase escalation to impasse. These must be green before Feature Model work begins.

**Blocked by:** Nothing
**Blocks:** Task 1

**Acceptance Criteria:**
- [ ] Spec gate fires when `qa_validation` completes (instrumented in monitor.py)
- [ ] `required_output` declarations added to workflow.yaml for each phase
- [ ] Abandoned required phase sets workflow status to `impasse` (not `skip`)
- [ ] `optional_phases` config added to workflow.yaml (forensics_analysis, git_commit_push)
- [ ] Seeded failing test triggers GOTO
- [ ] Seeded abandoned phase triggers impasse with human input flow
- [ ] All 74 existing tests pass

**Files Modified:**
- `src/monitoring/monitor.py`
- `src/mcp/server.py` (update_task_status handler)
- `config/workflows/autopilot/workflow.yaml`

**Testing:**
- Unit test: spec gate fires on QA completion
- Integration test: abandoned phase triggers impasse

---

### Task 1: Database Schema

**Description:** Add Feature table, modify AutopilotDesign and Workflow tables, add idempotent migration.

**Blocked by:** Task 0
**Blocks:** Tasks 2, 3, 4

**Acceptance Criteria:**
- [ ] `Feature` class added to `src/core/database.py` with all columns per §3.2
- [ ] `file_path`, `designs_folder`, `phase0_workflow_id` columns added to `AutopilotDesign`
- [ ] Status constraint extended: `pending|processing|decomposing|active|completed|failed|skipped`
- [ ] `workflow_type`, `feature_id` columns added to `Workflow`
- [ ] `features` relationship added to `AutopilotDesign` (cascade all, delete-orphan)
- [ ] `_migrate_feature_model_columns()` function added (idempotent)
- [ ] Migration called from `DatabaseManager.__init__` alongside existing migrations
- [ ] `from src.core.database import Feature` works without error
- [ ] `Feature.__table__` created in SQLite on startup

**Files Modified:**
- `src/core/database.py`

**Testing:**
- `python -c "from src.core.database import Feature; print('OK')"`
- Verify migration is idempotent (call twice, no errors)
- Existing 74 tests still pass

---

### Task 2: Phase 0 YAML and Workflow

**Description:** Create `autopilot-phase0` workflow definition with Feature Architect agent prompt.

**Blocked by:** Task 1
**Blocks:** Tasks 3, 5

**Acceptance Criteria:**
- [ ] `config/workflows/autopilot-phase0/workflow.yaml` created with:
  - `default_model: xiaomi/mimo-v2.5`
  - `execution_order: [1]`
  - `session_roles.feature_architect: architect`
  - `orchestrator.type: evaluating`
  - `evaluation_points` after `feature_architect`
  - `launch_template.parameters` with `design_document`, `project_path`, `design_id`
- [ ] `config/workflows/autopilot-phase0/01_feature_architect.yaml` created with:
  - Complete agent prompt per design doc §5.2
  - `done_definitions` listing all completion criteria
  - `outputs` declaring `.hephaestus/features.json` and `.hephaestus/features/<id>/scope.md`
- [ ] Workflow auto-discovered by `workflow_registry.py` (no code changes needed)
- [ ] `autopilot-phase0` appears in `get_all_workflow_definitions()` output
- [ ] Smoke test: Phase 0 runs against a simple design doc, produces valid features.json

**Files Created:**
- `config/workflows/autopilot-phase0/workflow.yaml`
- `config/workflows/autopilot-phase0/01_feature_architect.yaml`

**Testing:**
- Unit test: workflow loads from YAML
- Integration test: Phase 0 produces valid features.json + scope.md files

---

### Task 3: Orchestrator Refactor

**Description:** Refactor `run_single_design` into three-stage coordinator with all helper functions.

**Blocked by:** Tasks 1, 2
**Blocks:** Tasks 4, 5, 6

**Acceptance Criteria:**
- [ ] Helper functions implemented:
  - `_create_integration_worktree(project_path, design_id, branch, logger)`
  - `_cleanup_worktree(worktree, branch, project_path, logger)`
  - `_create_designs_folder(project_path, design_entry, logger)`
  - `_create_feature_records(design_id, features_json, designs_folder, logger)`
  - `_update_feature_status(feature_id, design_id, status, error=None)`
  - `_update_design_status(design_id, status, **kwargs)`
  - `_set_workflow_type(workflow_id, workflow_type)`
  - `_link_workflow_to_feature(workflow_id, feature_id)`
  - `_validate_features_json(features_json)` (raises ValueError on invalid)
  - `_should_skip(feature, feature_results)` (True if dependency failed)
- [ ] `_resolve_execution_order(features, logger)` implements Kahn's algorithm
- [ ] `run_phase0(sdk, design_entry, project_path, logger, state)` implements §6.2
- [ ] `_run_one_feature(sdk, design_entry, feature, designs_folder, ...)` implements §6.5
- [ ] `run_feature_pipelines(...)` implements §6.3 with ThreadPoolExecutor
- [ ] `run_design_aggregate(...)` implements §6.6
- [ ] `run_single_design` rewritten to call three stages
- [ ] Constants added: `MAX_PHASE0_TIME = 3600`, `MAX_PARALLEL_FEATURES = 4`
- [ ] `FeatureReport` dataclass updated for feature-level reports
- [ ] Two-feature parallel design test passes
- [ ] Existing single-feature flow still works (backward compatible)

**Files Modified:**
- `src/autopilot/orchestrator.py`

**Testing:**
- Unit test: `_resolve_execution_order` with various dependency graphs
- Unit test: `_validate_features_json` with valid/invalid inputs
- Unit test: `_should_skip` logic
- Integration test: two-feature parallel design runs both features concurrently
- Regression: existing 74 tests pass

---

### Task 4: CLI and API Changes

**Description:** Rewrite `add_to_queue` to store file_path, add POST endpoint, update `pick_next_design`.

**Blocked by:** Tasks 1, 3
**Blocks:** Task 7

**Acceptance Criteria:**
- [ ] `add_to_queue` in `src/cli/commands/autopilot.py`:
  - Resolves absolute path
  - Calls `POST /api/autopilot/designs/add`
  - Does NOT copy the file
  - Returns design ID and status
- [ ] `POST /api/autopilot/designs/add` endpoint:
  - Accepts `{file_path, project_path}`
  - Validates file exists
  - Finds/creates `AutopilotProject`
  - Checks for duplicate (returns existing)
  - Creates `AutopilotDesign` record
  - Returns `{id, name, status}`
- [ ] `pick_next_design` updated:
  - Reads `file_path` column from DB
  - Falls back to filename-based path
  - Handles missing files gracefully
- [ ] CLI test: `heph autopilot add ~/designs/test.md --project-path ~/project`
- [ ] API test: `curl -X POST http://localhost:8300/api/autopilot/designs/add -d '{"file_path": "...", "project_path": "..."}'`

**Files Modified:**
- `src/cli/commands/autopilot.py`
- `src/mcp/autopilot_api.py`
- `src/autopilot/orchestrator.py` (pick_next_design)

**Testing:**
- Unit test: add_to_queue via API
- Unit test: duplicate detection
- Integration test: CLI → API → DB round-trip

---

### Task 5: Phase YAML Updates

**Description:** Update existing autopilot workflow to pass feature_scope and feature_id; reference scope.md as primary input.

**Blocked by:** Tasks 2, 3
**Blocks:** Task 7

**Acceptance Criteria:**
- [ ] `config/workflows/autopilot/workflow.yaml` updated:
  - `launch_template.parameters` includes `feature_id` and `feature_scope` (optional)
  - `phase_1_task_prompt` references `{feature_scope}` as PRIMARY input
- [ ] Phase YAMLs updated (highest priority first):
  - `product_requirements.yaml`: reads `scope.md` first
  - `architecture_design.yaml`: reads `scope.md` first
  - `scope_review.yaml`: reads `scope.md` (already reads design.md)
  - `product_validation.yaml`: reads `scope.md` (already reads design.md)
- [ ] Backward compatible: empty `feature_scope` → falls back to `design.md`
- [ ] Existing single-feature pipeline still works without Feature model

**Files Modified:**
- `config/workflows/autopilot/workflow.yaml`
- `config/workflows/autopilot/product_requirements.yaml`
- `config/workflows/autopilot/architecture_design.yaml`
- `config/workflows/autopilot/scope_review.yaml`
- `config/workflows/autopilot/product_validation.yaml`

**Testing:**
- Integration test: feature pipeline reads scope.md
- Regression: non-feature pipeline still reads design.md

---

### Task 6: Design Report Template

**Description:** Create Jinja2 HTML template for design-level aggregate report.

**Blocked by:** Task 3
**Blocks:** Task 7

**Acceptance Criteria:**
- [ ] `src/autopilot/templates/design_report.html` created with:
  - Summary table: feature name, status, time, QA passed, product validated
  - Aggregate cost and time across all features
  - List of PRs merged (from feature git commits)
  - Forensics highlights per feature
- [ ] `_generate_design_report_html` in orchestrator renders template
- [ ] HTML file written to `designs_folder/design_report.html`
- [ ] `_write_design_metrics` writes `designs_folder/design_metrics.json`
- [ ] Report opens correctly in browser

**Files Created:**
- `src/autopilot/templates/design_report.html`

**Files Modified:**
- `src/autopilot/orchestrator.py` (add `_generate_design_report_html`, `_write_design_metrics`)

**Testing:**
- Unit test: template renders without errors
- Integration test: design_report.html is generated after aggregate

---

### Task 7: Integration Testing

**Description:** End-to-end tests covering all Feature Model scenarios.

**Blocked by:** Tasks 4, 5, 6
**Blocks:** Nothing (final task)

**Acceptance Criteria:**
- [ ] `test_resolve_execution_order.py`:
  - Parallel features at same depth
  - Sequential features in order
  - depends_on DAG resolution
  - Cycle detection and fallback
- [ ] `test_validate_features_json.py`:
  - Valid JSON passes
  - Missing required fields rejected
  - Duplicate IDs rejected
  - Cycle in depends_on rejected
  - Overlapping file paths rejected
- [ ] `test_create_feature_records.py`:
  - DB records created correctly
  - Status starts as "pending"
  - relationships link to design
- [ ] `test_phase0_workflow.py`:
  - Phase 0 runs against simple design doc
  - Produces valid features.json
  - Produces scope.md per feature
- [ ] `test_feature_model_single.py`:
  - Single-feature design runs end-to-end
  - Feature report generated
  - design_report.html written
- [ ] `test_feature_model_parallel.py`:
  - Two parallel features execute concurrently
  - ThreadPoolExecutor used
  - Both complete independently
- [ ] `test_feature_model_sequential.py`:
  - Feature A → Feature B sequential
  - B does not start until A completes
- [ ] `test_feature_dependency_failed.py`:
  - Feature A fails
  - Feature B (depends_on A) is marked skipped
- [ ] All 74 existing tests still pass

**Files Created:**
- `tests/test_resolve_execution_order.py`
- `tests/test_validate_features_json.py`
- `tests/test_create_feature_records.py`
- `tests/test_phase0_workflow.py`
- `tests/test_feature_model_single.py`
- `tests/test_feature_model_parallel.py`
- `tests/test_feature_model_sequential.py`
- `tests/test_feature_dependency_failed.py`

---

## 8. Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Phase 0 agent produces invalid features.json | Feature pipelines cannot start | `_validate_features_json()` with detailed error messages; retry up to 1 time |
| Parallel feature merge conflicts | Code integration broken | Features own non-overlapping files; worktrees isolated per feature |
| Phase 0 timeout (3600s) | Pipeline stuck | Timeout kills Phase 0; design marked failed |
| Feature dependency cycle | Deadlock | Kahn's algorithm detects cycles; falls back to sequential |
| Context overflow in feature pipeline | Agent fails | scope.md is smaller than full design.md; agents focused on narrow scope |
| Backward compatibility break | Existing workflows fail | Empty `feature_scope`/`feature_id` params trigger legacy path |

---

## 9. Implementation Order Summary

```
Step 0: Run B fixes (spec gate + impasse)  ← MUST be green first
Step 1: DB schema (Feature + columns + migration)
Step 2: Phase 0 YAML and workflow registration
Step 3: Orchestrator refactor (three-stage coordinator)
Step 4: CLI and API changes
Step 5: Phase YAML updates
Step 6: Design report template
Step 7: Integration testing
```

Each step builds on the previous. Do not skip ahead.

---

**Architecture complete. Ready for implementation.**
