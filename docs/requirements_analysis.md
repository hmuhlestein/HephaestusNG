# Product Requirements Analysis: Feature Model Implementation

**Feature ID:** feature-model-implementation  
**Feature Name:** Feature Model Implementation  
**Status:** Requirements Extracted  
**Date:** 2026-06-27  
**Design Document:** `.hephaestus/design.md`

---

## 1. Executive Summary

Implement the Feature model for HephaestusNG's autopilot pipeline. Decomposes complex designs into independently shippable slices before code is written. Each slice runs its own 12-phase pipeline in its own git worktree with controlled parallelism.

---

## 2. Functional Requirements

### FR-1: Feature Database Table
New `Feature` SQLAlchemy table with columns: id, design_id, feature_key, name, scope, files (JSON), depends_on (JSON), execution, status, workflow_id, scope_doc_path, feature_record_path, timestamps, error.

### FR-2: AutopilotDesign Table Modifications
Add columns: file_path (Text), designs_folder (Text), phase0_workflow_id (FK). Extend status constraint: `pending | processing | decomposing | active | completed | failed | skipped`.

### FR-3: Workflow Table Modifications
Add columns: workflow_type (design/feature), feature_id (FK to features).

### FR-4: Database Migration
Idempotent `_migrate_feature_model_columns()` function in `src/core/database.py`.

### FR-5: Phase 0 Workflow Definition
New `config/workflows/autopilot-phase0/` with workflow.yaml and 01_feature_architect.yaml.

### FR-6: Workflow Registry Update
Register `autopilot-phase0` in `src/workflow_registry.py`.

### FR-7: Orchestrator - run_phase0 (Stage 1)
Create integration worktree, launch Phase 0, validate features.json, create permanent designs/ folder.

### FR-8: Orchestrator - run_feature_pipelines (Stage 2)
Topological sort (Kahn's algorithm), ThreadPoolExecutor with MAX_PARALLEL_FEATURES=4.

### FR-9: Orchestrator - run_design_aggregate (Stage 3)
Generate design_report.html via Jinja2, design_metrics.json.

### FR-10: Helper Functions
Create: _create_integration_worktree, _cleanup_worktree, _create_designs_folder, _create_feature_records, _update_feature_status, _update_design_status, _set_workflow_type, _link_workflow_to_feature, _validate_features_json, _should_skip.

### FR-11: CLI Changes - add_to_queue
Store file_path in DB, do not copy file.

### FR-12: API Endpoint - POST /api/autopilot/designs/add
Accept file_path and project_path, create design record.

### FR-13: pick_next_design Update
Prefer file_path column over filename.

### FR-14: Phase YAML Updates
Pass feature_scope and feature_id; reference scope.md as primary input.

### FR-15: Design Report Template
Create `src/autopilot/templates/design_report.html` using Jinja2.

---

## 3. Non-Functional Requirements

### NFR-1: Backward Compatibility
Existing autopilot workflow continues for designs without Feature model.

### NFR-2: Performance
MAX_PARALLEL_FEATURES = 4 concurrent feature pipelines.

### NFR-3: Reliability
MAX_PHASE0_TIME = 3600 seconds timeout.

### NFR-4: Idempotency
Database migrations safe to call on every startup.

---

## 4. Integration Points

| Component | Type | Description |
|-----------|------|-------------|
| `src/core/database.py` | Modify | Add Feature class, columns, migration |
| `src/autopilot/orchestrator.py` | Modify | Three-stage coordinator refactor |
| `src/cli/commands/autopilot.py` | Modify | Rewrite add_to_queue |
| `src/mcp/autopilot_api.py` | Modify | Add POST /api/autopilot/designs/add |
| `src/workflow_registry.py` | Modify | Register autopilot-phase0 |
| `config/workflows/autopilot-phase0/` | New | workflow.yaml + 01_feature_architect.yaml |
| `src/autopilot/templates/` | New | design_report.html (Jinja2) |

---

## 5. Technology Constraints

1. Python 3 (existing stack)
2. SQLAlchemy (existing ORM)
3. Jinja2 (existing template engine)
4. pytest with 74 existing tests
5. Git worktrees for isolation
6. No new external dependencies

---

## 6. Acceptance Criteria

| ID | Criterion | Verification |
|----|-----------|--------------|
| AC-1 | Feature table created | `from src.core.database import Feature` |
| AC-2 | Phase 0 workflow registered | Workflow launchable via SDK |
| AC-3 | Phase 0 produces valid features.json | JSON schema validation |
| AC-4 | Phase 0 produces scope.md per feature | File existence check |
| AC-5 | Parallel features execute concurrently | ThreadPoolExecutor |
| AC-6 | Sequential features respect depends_on | Topological sort |
| AC-7 | design_report.html generated | HTML file written |
| AC-8 | CLI add_to_queue stores file_path | DB record check |
| AC-9 | API endpoint works | POST returns 200 |
| AC-10 | Backward compatible | Old flow runs |
| AC-11 | Existing tests pass | All 74 tests green |

---

## 7. Implementation Order

1. **Step 0:** Run B fixes (spec gate + impasse) - MUST be green first
2. **Step 1:** DB schema (Feature table + columns + migration)
3. **Step 2:** Phase 0 YAML and workflow registration
4. **Step 3:** Orchestrator refactor (three-stage coordinator)
5. **Step 4:** CLI and API changes
6. **Step 5:** Phase YAML updates
7. **Step 6:** Feature report (Jinja2 template)

---

## 8. Testing Requirements

### Unit Tests
- `test_resolve_execution_order.py`
- `test_validate_features_json.py`
- `test_create_feature_records.py`

### Integration Tests
- `test_phase0_workflow.py`
- `test_feature_model_single.py`
- `test_feature_model_parallel.py`
- `test_feature_model_sequential.py`
- `test_feature_dependency_failed.py`

### Regression
- All 74 existing tests must pass

---

**Requirements extracted. Ready for Scope Review and Architecture.**
