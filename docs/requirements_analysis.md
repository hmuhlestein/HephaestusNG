# Product Requirements Analysis: Feature Model Implementation

**Feature ID:** feature-model-implementation  
**Feature Name:** Feature Model Implementation  
**Status:** Requirements Extracted  
**Date:** 2026-06-29  
**Design Document:** `.hephaestus/design.md`

---

## 1. Executive Summary

Implement the Feature model for HephaestusNG's autopilot pipeline. Decomposes complex designs into independently shippable slices (Features) before code is written. Each Feature runs its own 12-phase pipeline in its own git worktree with controlled parallelism. Addresses context window overflow, agent scope loss, and failure isolation issues in the current flat pipeline.

**Current State:** Single flat Design → 11-phase workflow → feature report  
**Target State:** Design → Phase 0 (Feature Architect) → features.json → Per-feature pipelines (parallel/sequential) → Design Aggregate → design_report.html + design_metrics.json

---

## 2. Prerequisites (Run B Fixes)

### PR-1: Spec Gate Must Fire on QA Completion
- **Problem:** `_build_spec_phase_output` not called when `qa_validation` completes
- **Fix A:** Instrument task completion paths in `src/monitoring/monitor.py`
- **Fix B:** Output-existence completion floor in `update_task_status` handler (`src/mcp/server.py` ~line 1794)
- **Output artifact declarations required per phase:**
  - `qa_result.json` (qa_validation)
  - `product_validation.json` (product_validation)
  - `architecture.md` (architecture_design)
  - `requirements_analysis.md` (product_requirements)
  - `scope_review_result.json` (scope_review)

### PR-2: Abandoned Required Phase Must Escalate to Impasse
- **Problem:** `security_review` abandoned after 6 attempts, pipeline continued silently
- **Fix:** In `src/monitoring/monitor.py`, set phase status to `failed`, workflow to `impasse`, trigger human intervention
- **Optional phases (can fail without blocking):** `forensics_analysis`, `git_commit_push`

---

## 3. Functional Requirements

### FR-1: Feature Database Table
- **Requirement:** New `Feature` SQLAlchemy table
- **Columns:** id (PK), design_id (FK), feature_key, name, scope, files (JSON), depends_on (JSON), execution, status, workflow_id (FK), scope_doc_path, feature_record_path, created_at, started_at, completed_at, error
- **Check Constraints:** execution IN ('parallel', 'sequential'), status IN ('pending', 'active', 'completed', 'failed', 'skipped')
- **Relationships:** belongs_to AutopilotDesign, has_one Workflow
- **Acceptance:** `from src.core.database import Feature` succeeds; table created on startup

### FR-2: AutopilotDesign Table Modifications
- **Requirement:** Add columns: file_path (Text), designs_folder (Text), phase0_workflow_id (FK to workflows)
- **Requirement:** Extend status constraint: `pending | processing | decomposing | active | completed | failed | skipped`
- **Acceptance:** New columns exist; design status transitions work

### FR-3: Workflow Table Modifications
- **Requirement:** Add columns: workflow_type (design/feature), feature_id (FK to features)
- **Acceptance:** Workflow type tracking works; feature linkage established

### FR-4: Database Migration
- **Requirement:** Idempotent `_migrate_feature_model_columns()` in `src/core/database.py`
- **Call location:** `DatabaseManager.__init__`
- **Acceptance:** Safe to call on every startup; creates Feature table and adds columns

### FR-5: Phase 0 Workflow Definition
- **Requirement:** New `config/workflows/autopilot-phase0/` directory with:
  - `workflow.yaml`: default_model (xiaomi/mimo-v2.5), execution_order, orchestrator config, launch_template
  - `01_feature_architect.yaml`: phase definition with done_definitions, outputs, instructions
- **Agent role:** feature_architect
- **Acceptance:** Phase 0 runs standalone; produces valid features.json and scope.md files

### FR-6: Workflow Registry Update
- **Requirement:** Register `autopilot-phase0` in `src/workflow_registry.py`
- **Acceptance:** Phase 0 workflow launchable via SDK

### FR-7: Orchestrator - run_phase0 (Stage 1)
- **Requirement:** Create integration worktree, copy design.md, launch Phase 0, poll until complete, validate features.json, create permanent designs/ folder, copy outputs, create Feature DB records
- **Constants:** MAX_PHASE0_TIME = 3600, MAX_PARALLEL_FEATURES = 4
- **Acceptance:** Phase 0 completes; features.json valid; Feature records created

### FR-8: Orchestrator - run_feature_pipelines (Stage 2)
- **Requirement:** Resolve execution order via topological sort (Kahn's algorithm), run features in parallel groups or sequential based on depends_on and execution fields
- **Algorithm:** Build dependency graph, process in layers, separate parallel from sequential features
- **Acceptance:** Parallel features run concurrently; sequential features respect ordering

### FR-9: Orchestrator - run_design_aggregate (Stage 3)
- **Requirement:** Aggregate results, generate design_metrics.json, generate design_report.html via Jinja2, update final design status
- **Acceptance:** design_report.html written; final status correct

### FR-10: Helper Functions
- **Requirement:** Implement helpers:
  - `_create_integration_worktree`: Create git worktree for feature isolation
  - `_cleanup_worktree`: Remove worktree after feature completes
  - `_create_designs_folder`: Create permanent record folder with timestamp
  - `_create_feature_records`: Create Feature DB records from features.json
  - `_update_feature_status`: Update Feature status in DB
  - `_update_design_status`: Update AutopilotDesign status in DB
  - `_set_workflow_type`: Mark workflow as design or feature type
  - `_link_workflow_to_feature`: Associate workflow with Feature
  - `_validate_features_json`: Validate features.json schema
  - `_should_skip`: Check if feature dependency failed
- **Acceptance:** All helpers functional; orchestrator uses them

### FR-11: CLI Changes - add_to_queue
- **Requirement:** Store file_path in DB, do not copy file. Resolve absolute path, create AutopilotDesign record with file_path = str(abs_path)
- **Acceptance:** `heph autopilot add <path>` registers design without copying

### FR-12: API Endpoint - POST /api/autopilot/designs/add
- **Requirement:** Accept file_path and project_path, find/create project, check duplicates, create design record
- **Acceptance:** API returns design id, name, status

### FR-13: pick_next_design Update
- **Requirement:** Prefer file_path column over filename; fallback to filename relative to project base dir + DESIGN_SUBDIR
- **Acceptance:** Designs with file_path work; fallback works for legacy

### FR-14: Phase YAML Updates
- **Requirement:** Update workflow.yaml launch_template to pass feature_scope and feature_id; update phase YAMLs to reference scope.md as primary input
- **Acceptance:** Phase 1 receives feature_scope; reads scope.md first

### FR-15: Design Report Template
- **Requirement:** Create `src/autopilot/templates/design_report.html` using Jinja2
- **Content:** Summary table, aggregate metrics, PRs merged, forensics highlights
- **Acceptance:** HTML generated with all required sections

---

## 4. Non-Functional Requirements

### NFR-1: Backward Compatibility
- **Requirement:** Existing autopilot workflow continues for designs without Feature model
- **Acceptance:** Old flow still runs; feature_scope and feature_id parameters optional

### NFR-2: Performance
- **Requirement:** MAX_PARALLEL_FEATURES = 4 concurrent feature pipelines
- **Acceptance:** System doesn't exceed resource limits

### NFR-3: Reliability
- **Requirement:** MAX_PHASE0_TIME = 3600 seconds timeout
- **Acceptance:** Long-running Phase 0 doesn't block indefinitely

### NFR-4: Idempotency
- **Requirement:** Database migrations safe to call on every startup
- **Acceptance:** No errors on repeated startup

---

## 5. Integration Points

| Component | Type | Description |
|-----------|------|-------------|
| `src/core/database.py` | Modify | Add Feature class, new columns, migration function |
| `src/autopilot/orchestrator.py` | Modify | Refactor run_single_design to three-stage coordinator |
| `src/cli/commands/autopilot.py` | Modify | Rewrite add_to_queue |
| `src/mcp/autopilot_api.py` | Modify | Add POST /api/autopilot/designs/add |
| `src/mcp/server.py` | Modify | Output-existence completion floor (PR-1 Fix B) |
| `src/monitoring/monitor.py` | Modify | Spec gate firing + impasse handling (PR-1, PR-2) |
| `src/workflow_registry.py` | Modify | Register autopilot-phase0 |
| `config/workflows/autopilot-phase0/` | New | workflow.yaml + 01_feature_architect.yaml |
| `src/autopilot/templates/` | New | design_report.html (Jinja2) |

**No new external dependencies required.** All changes use existing stack.

---

## 6. Technology Constraints

1. **Language:** Python 3 (existing HephaestusNG stack)
2. **ORM:** SQLAlchemy (existing)
3. **Template Engine:** Jinja2 (existing)
4. **Testing:** pytest with `-p no:libtmux` flag (broken plugin)
5. **Version Control:** Git worktrees for feature isolation
6. **No new dependencies:** Pure extensions of existing patterns

---

## 7. File Structure (Permanent Storage)

```
<project>/
  designs/
    <timestamp>_<name>_<design-id>/
      design.md
      features.json
      design_report.html
      design_metrics.json
      features/
        <feature-id>/
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
```

---

## 8. Acceptance Criteria Summary

| ID | Criterion | Verification |
|----|-----------|--------------|
| AC-1 | Feature table created | `from src.core.database import Feature` |
| AC-2 | Phase 0 workflow registered | Workflow launchable via SDK |
| AC-3 | Phase 0 produces valid features.json | JSON schema validation |
| AC-4 | Phase 0 produces scope.md per feature | File existence check |
| AC-5 | Parallel features execute concurrently | ThreadPoolExecutor with MAX_PARALLEL_FEATURES |
| AC-6 | Sequential features respect depends_on | Topological sort ordering |
| AC-7 | design_report.html generated | HTML file written to designs_folder |
| AC-8 | CLI add_to_queue stores file_path | DB record has file_path column |
| AC-9 | API endpoint works | POST /api/autopilot/designs/add returns 200 |
| AC-10 | Backward compatible | Old flow runs without feature_scope |
| AC-11 | Existing tests pass | All 74 tests green |
| AC-12 | Spec gate fires | Seeded failing test triggers GOTO |
| AC-13 | Impasse on abandoned phase | Abandoned required phase escalates to human |

---

## 9. Implementation Order

1. **Step 0:** Run B fixes (spec gate + abandoned phase impasse) - MUST be green first
2. **Step 1:** DB schema (Feature table + column additions + migration)
3. **Step 2:** Phase 0 YAML and workflow registration
4. **Step 3:** Orchestrator refactor (three-stage coordinator)
5. **Step 4:** CLI and API changes
6. **Step 5:** Phase YAML updates (scope.md references)
7. **Step 6:** Feature report (Jinja2 template)

---

## 10. Testing Requirements

### Unit Tests
- `test_resolve_execution_order.py`: parallel, sequential, depends_on DAG, cycles
- `test_validate_features_json.py`: valid JSON, missing fields, duplicate IDs, cycles, overlapping files
- `test_create_feature_records.py`: DB records created, status starts pending

### Integration Tests
- `test_phase0_workflow.py`: Phase 0 runs against real design doc
- `test_feature_model_single.py`: Single-feature design end-to-end
- `test_feature_model_parallel.py`: Two-feature parallel design
- `test_feature_model_sequential.py`: Feature A → Feature B sequential
- `test_feature_dependency_failed.py`: Dependency failure propagation

### Regression
- All existing 74 tests must pass
- Smoke test with calculator project (single-feature design)

---

## 11. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Run B not green | High | Blocker | Must fix spec gate + impasse first |
| Context overflow in Phase 0 | Low | High | Design doc size; may need chunking |
| Worktree cleanup failures | Medium | Medium | Robust cleanup helpers with error handling |
| Backward compatibility break | Medium | High | Optional parameters; fallback logic |

---

**Requirements extracted. Ready for Scope Review and Architecture.**
