# Feature Model Implementation

**Status:** Ready for implementation  
**Depends on:** Run B green (spec gate fix + abandoned-phase impasse fix — see §11.2 of `autopilot_architecture_review.md`)  
**References:** `docs/autopilot.md` (design), `design_docs/autopilot_architecture_review.md` (architecture decisions)

---

## 1. Goal

The current pipeline runs a single flat Design → 11-phase workflow. Complex designs
fail because one enormous workflow tries to build everything at once: the context
window overflows, agents lose track of scope, and failures in one area block everything
else.

The Feature model decomposes a design into independently shippable slices before any
code is written. Each slice (Feature) runs its own 12-phase pipeline in its own git
worktree, with controlled parallelism. The result:

- Small-context agents focused on a specific feature scope, not the entire design
- Parallel execution where the Feature Architect declares it safe
- Sequential ordering where inter-feature dependencies require it
- Per-feature pass/fail tracking; one feature's failure does not block unrelated features
- A permanent audit trail in `designs/<timestamp>_<name>_<design-id>/`

This design is the within-design application of §12's across-design concurrency DAG.
The integration model is §9.6 (per-design integration worktrees), applied at the
feature level.

---

## 2. Prerequisites (must land before Feature Model)

These are Run B failures documented in §11.2 of the architecture review:

### 2.1 Spec gate must fire on QA completion

**Problem:** `_build_spec_phase_output` is not called when `qa_validation` completes.
QA agent marks done with no `qa_report.md` → pipeline continues, spec gate never
scores.

**Fix (two complementary approaches):**

**A. Make the gate fire** — instrument every task completion path in
`src/monitoring/monitor.py`. Find the path that completes `qa_validation` tasks.
Ensure it calls `_build_spec_phase_output` for phases configured with a spec evaluator.
Add `[SPEC-GATE]` log statements. Verify with a seeded failing test.

**B. Output-existence completion floor** — in `update_task_status` handler
(`src/mcp/server.py` around line 1794), when a phase agent marks a task done, check
whether the phase's declared output artifact exists on disk in the worktree. If it does
not exist, reject the `done` status: send the agent a message ("Your declared output
`qa_report.md` is missing — produce it, then mark done"). This catches hallucinated
completions mechanically, at the source.

Output artifact declarations per phase (add to `workflow.yaml` or phase YAML):
```yaml
required_output: qa_report.md         # qa_validation
required_output: product_validation.md  # product_validation
required_output: architecture.md     # architecture_design
required_output: requirements_analysis.md  # product_requirements
required_output: scope_review_result.md  # scope_review
```

Implement both A and B; they are complementary. B is the general fix; A makes the
specific gate work even if B has edge cases.

### 2.2 Abandoned required phase must escalate to impasse, not skip

**Problem:** `security_review` (phase 6) was abandoned after 6 recovery attempts and
the pipeline continued to completion with it silently skipped.

**Fix:** In `src/monitoring/monitor.py`, when bounded recovery is exhausted on a
**required phase**, do NOT advance the pipeline. Instead:
- Set the phase status to `failed`
- Set the workflow status to `impasse`  
- Trigger the human-input intervention flow (§9.4 of architecture review)
- Log `[IMPASSE] Required phase {name} failed after {n} attempts — escalating to human`

"Required" means any phase in the pipeline whose absence means the pipeline did not
run its full quality checks. All phases are required by default. Only `forensics_analysis`
and `git_commit_push` are optional (pipeline completes without them if they fail).

Mark optional phases in `workflow.yaml`:
```yaml
optional_phases: [forensics_analysis, git_commit_push]
```

---

## 3. Overview of Changes

```
Before:  Design → Workflow (11 phases) → feature report

After:   Design → Phase 0 (Feature Architect) → features.json
                                                     │
                              ┌──────────────────────┤
                              ▼                      ▼
                         auth pipeline          session pipeline    (parallel)
                         (Phases 1–12)          (Phases 1–12)
                              │
                              ▼
                         admin pipeline                              (sequential)
                         (Phases 1–12, waits for auth + session)
                              │
                              ▼
                    Design Aggregate (orchestrator, no agent)
                    → design_report.html + design_metrics.json
```

---

## 4. Database Changes

### 4.1 New: `Feature` table

```python
class Feature(Base):
    __tablename__ = "features"

    id = Column(String, primary_key=True)  # e.g. "feat-<uuid>"
    design_id = Column(String, ForeignKey("autopilot_designs.id"), nullable=False)
    feature_key = Column(String(100), nullable=False)  # slug from features.json "id" field
    name = Column(String, nullable=False)
    scope = Column(Text, nullable=False)   # one-paragraph summary from features.json
    files = Column(JSON, nullable=True)    # list of file paths this feature owns
    depends_on = Column(JSON, nullable=True)  # list of feature_key strings
    execution = Column(
        String,
        CheckConstraint("execution IN ('parallel', 'sequential')"),
        nullable=False,
        default="parallel"
    )
    status = Column(
        String,
        CheckConstraint("status IN ('pending', 'active', 'completed', 'failed', 'skipped')"),
        nullable=False,
        default="pending"
    )
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True)
    scope_doc_path = Column(Text, nullable=True)  # abs path to this feature's scope.md in permanent record
    feature_record_path = Column(Text, nullable=True)  # abs path to designs/<ts>_<name>_<id>/features/<key>/
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)

    # Relationships
    design = relationship("AutopilotDesign", back_populates="features")
    workflow = relationship("Workflow", foreign_keys=[workflow_id])
```

Add to `AutopilotDesign`:
```python
features = relationship("Feature", back_populates="design", cascade="all, delete-orphan")
```

### 4.2 Modify: `AutopilotDesign` table

Add columns:
```python
# Full absolute path to design file — can live anywhere on the filesystem.
# Set by `heph autopilot add <path>`; takes precedence over filename-derived path.
file_path = Column(Text, nullable=True)

# Path to the permanent record folder: designs/<timestamp>_<name>_<design-id>/
designs_folder = Column(Text, nullable=True)

# Phase 0 workflow ID (the DesignWorkflow run)
phase0_workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True)
```

Extend the `status` check constraint to include new design states:
```
pending | processing | decomposing | active | completed | failed | skipped
```
- `pending`: in queue, not started
- `processing`: being picked up (transient, set before Phase 0 starts)
- `decomposing`: Phase 0 Feature Architect is running
- `active`: features.json written; per-feature pipelines running
- `completed`: all features completed
- `failed`: one or more features failed beyond max iterations
- `skipped`: removed from queue

### 4.3 Modify: `Workflow` table

Add columns:
```python
# Distinguishes design-level (Phase 0) from feature-level (Phases 1–12) workflows
workflow_type = Column(
    String,
    CheckConstraint("workflow_type IN ('design', 'feature')"),
    nullable=True,
    default="feature"
)

# FK to the Feature this workflow serves (null for design-level workflows)
feature_id = Column(String, ForeignKey("features.id"), nullable=True)
```

Add relationship:
```python
feature = relationship("Feature", foreign_keys=[feature_id])
```

### 4.4 Migration

Add a migration function to `src/core/database.py` (following the existing
`_migrate_autopilot_designs_columns` pattern):

```python
def _migrate_feature_model_columns(engine) -> None:
    """Add Feature model columns. Idempotent — safe to call on every startup."""
    with engine.connect() as conn:
        _add_column_if_missing(conn, "autopilot_designs", "file_path", "TEXT")
        _add_column_if_missing(conn, "autopilot_designs", "designs_folder", "TEXT")
        _add_column_if_missing(conn, "autopilot_designs", "phase0_workflow_id", "TEXT")
        _add_column_if_missing(conn, "workflows", "workflow_type", "TEXT DEFAULT 'feature'")
        _add_column_if_missing(conn, "workflows", "feature_id", "TEXT")
        conn.commit()
    # Create the features table
    Base.metadata.create_all(engine, tables=[Feature.__table__], checkfirst=True)
```

Call this in `DatabaseManager.__init__` alongside existing migrations.

---

## 5. New Workflow Definition: `autopilot-phase0`

Create `config/workflows/autopilot-phase0/` directory with:

### 5.1 `workflow.yaml`

```yaml
default_model: xiaomi/mimo-v2.5
default_thinking_level: low

execution_order: [1]

session_roles:
  feature_architect: architect

orchestrator:
  type: evaluating
  max_phase_retries: 1
  max_total_gotos: 2
  evaluation_points:
    - after_phase: feature_architect
      evaluator: heuristic
      max_retries: 1
      conditions:
        - if: "score >= 0.0"
          action: continue
          reason: "Feature decomposition complete"

workflow:
  result_criteria: "features.json and all scope.md files written"
  on_result_found: do_nothing
  enable_tickets: false

launch_template:
  parameters:
    - name: design_document
      label: Design Document Path
      type: text
      required: true
    - name: project_path
      label: Project Working Directory
      type: text
      required: true
    - name: design_id
      label: AutopilotDesign DB ID
      type: text
      required: true
  phase_1_task_prompt: |
    Phase 0: Feature Architect

    **Design Document:** ./.hephaestus/design.md (in your worktree)
    **Project Path:** . (your current working directory)
    **Design ID:** {design_id}

    Your job is to decompose this design into features and write features.json
    and one scope.md per feature. See the feature_architect phase YAML for
    full instructions.
```

### 5.2 `01_feature_architect.yaml`

```yaml
id: 1
name: feature_architect
thinking_level: low
description: |
  Decompose a design document into independently shippable Features.
  Each Feature gets its own scope document and will run the full 12-phase
  pipeline in its own git worktree.

done_definitions:
  - ".hephaestus/features.json written with valid JSON matching the schema"
  - "One .hephaestus/features/<id>/scope.md written per feature entry"
  - "All scope.md files cover: what is included, what is excluded, inter-feature interfaces, constraints"
  - "features.json execution field set per feature (parallel or sequential)"
  - "depends_on lists contain only feature IDs that exist in the same features.json"
  - "Task marked as done"

outputs:
  - ".hephaestus/features.json"
  - ".hephaestus/features/<id>/scope.md (one per feature)"

additional_notes: |
  OUTPUT STYLE: Terse. Technical terms exact. No pleasantries. Fragments OK.

  ═══════════════════════════════════════════════════════════════════
  YOU ARE THE FEATURE ARCHITECT — DECOMPOSE THE DESIGN
  ═══════════════════════════════════════════════════════════════════

  YOUR MISSION: Read design.md. Decompose into features. Write features.json
  and one scope.md per feature. You are a blocking gate — no feature pipeline
  starts until your output is validated.

  ═══════════════════════════════════════════════════════════════════
  STEP 0: GATHER CONTEXT
  ═══════════════════════════════════════════════════════════════════

  Before decomposing:
  1. Read ./.hephaestus/design.md (the full design document)
  2. Read AGENTS.md if it exists (project conventions and patterns)
  3. Search `designs/` directory for previously shipped features to inform scope boundaries
  4. Call search_memory("feature decomposition patterns scope boundaries")
  5. Call search_memory("parallel sequential execution dependency patterns")
  6. Call search_memory("completed features implemented components")

  ═══════════════════════════════════════════════════════════════════
  STEP 1: DECOMPOSE THE DESIGN INTO FEATURES
  ═══════════════════════════════════════════════════════════════════

  A Feature is a vertically-scoped, independently shippable slice:
  - Has a clear name and narrow scope (e.g. "JWT authentication")
  - Owns a specific set of files (non-overlapping)
  - Can be built, tested, and validated without building the other features first
    (or explicitly declares what it depends on)

  Rules for decomposition:
  - Simple designs (one self-contained capability) → ONE feature. Do not
    over-decompose. A single-feature design is the normal case for small designs.
  - Complex designs → 2–5 features. More than 5 is almost always wrong.
  - Features must not own overlapping files.
  - A feature with no dependencies should be parallel.
  - A feature that needs another feature's runtime output (shared DB tables,
    auth tokens, session state) must declare depends_on and be sequential.
  - Infrastructure shared across features (DB setup, shared models) should be
    its own feature that others depend on.

  Execution field rules:
  - parallel: feature can start immediately once its dependencies are complete.
    Multiple parallel features run concurrently.
  - sequential: feature waits for ALL preceding features (any that aren't its
    explicit dependencies) to complete before starting. Use this sparingly —
    only when you genuinely cannot predict which features must complete first.
    Prefer explicit depends_on over sequential for ordering.

  ═══════════════════════════════════════════════════════════════════
  STEP 2: WRITE features.json
  ═══════════════════════════════════════════════════════════════════

  Write to: ./.hephaestus/features.json

  Schema (strict — the orchestrator parses this):
  {
    "design_name": "<human name from design.md title>",
    "features": [
      {
        "id": "<short-slug>",           // unique, lowercase, hyphens OK, no spaces
        "name": "<Human Name>",
        "scope": "<one paragraph: what this feature covers and what it excludes>",
        "files": ["src/auth/", "tests/test_auth.py"],  // file/dir paths this feature owns
        "depends_on": [],               // list of feature id strings, or []
        "execution": "parallel"         // "parallel" or "sequential"
      }
    ]
  }

  Validate before writing:
  - All id values are unique
  - All depends_on values reference existing id values
  - No file paths overlap between features
  - execution is exactly "parallel" or "sequential"

  ═══════════════════════════════════════════════════════════════════
  STEP 3: WRITE scope.md PER FEATURE
  ═══════════════════════════════════════════════════════════════════

  For each feature in features.json, write a scope document:
  Path: ./.hephaestus/features/<feature-id>/scope.md

  Each scope.md must contain:

  # Feature Scope: <Feature Name>

  ## What This Feature Covers
  <Expanded prose description — what the agents building this feature must implement>

  ## What This Feature Excludes
  <Explicit boundaries — what adjacent features handle instead>

  ## Inter-Feature Interfaces
  <APIs, shared data models, events, or contracts with other features>
  <If this feature depends_on another, describe what it expects to already exist>

  ## File Ownership
  <The specific files/directories this feature owns and will create/modify>

  ## Constraints and Non-Functional Requirements
  <Performance, security, compatibility constraints specific to this feature>

  The scope.md is the PRIMARY input for Phases 1 (Product Requirements),
  2 (Scope Review), 3 (Architecture), and 10 (Product Validation).
  Make it complete and specific.

  ═══════════════════════════════════════════════════════════════════
  STEP 4: VERIFY YOUR OUTPUT
  ═══════════════════════════════════════════════════════════════════

  ```bash
  # Verify features.json is valid JSON
  python3 -c "import json; d=json.load(open('.hephaestus/features.json')); print(len(d['features']), 'features')"

  # Verify all scope.md files exist
  python3 -c "
  import json, pathlib
  d = json.load(open('.hephaestus/features.json'))
  for f in d['features']:
      p = pathlib.Path(f'.hephaestus/features/{f[\"id\"]}/scope.md')
      assert p.exists(), f'Missing: {p}'
      print(f'OK: {p}')
  "
  ```

  Fix any issues before marking done.

  ═══════════════════════════════════════════════════════════════════
  STEP 5: SAVE LEARNINGS TO MEMORY
  ═══════════════════════════════════════════════════════════════════

  Save the decomposition decision to memory:
  - Why you chose this number of features
  - Which features are parallel vs sequential and why
  - Any non-obvious scope boundaries

  save_memory(content="Feature decomposition [<design_name>]: <N> features. <key decisions>", memory_type="decision")

  ═══════════════════════════════════════════════════════════════════
  STEP 6: MARK TASK DONE
  ═══════════════════════════════════════════════════════════════════

  Call update_task_status with status="done".
```

Register `autopilot-phase0` in `src/workflow_registry.py` alongside the existing
`autopilot` registration.

---

## 6. Orchestrator Changes (`src/autopilot/orchestrator.py`)

### 6.1 New flow: `run_single_design` becomes a three-stage coordinator

Replace the current `run_single_design` function with:

```python
def run_single_design(sdk, design_entry, project_path, logger, state=None, max_iterations=10):
    """Three-stage coordinator: Phase 0, per-feature pipelines, design aggregate."""
    
    # Stage 1: Phase 0 — Feature Architect
    features_json, designs_folder = run_phase0(sdk, design_entry, project_path, logger, state)
    if features_json is None:
        return DesignStatus.FAILED, _empty_report(design_entry)
    
    # Stage 2: Per-feature pipelines (parallel + sequential)
    feature_results = run_feature_pipelines(
        sdk, design_entry, features_json, designs_folder,
        project_path, logger, state, max_iterations
    )
    
    # Stage 3: Design aggregate (no agent)
    return run_design_aggregate(design_entry, feature_results, designs_folder, logger)
```

### 6.2 Stage 1: `run_phase0`

```python
def run_phase0(sdk, design_entry, project_path, logger, state):
    """Run the Feature Architect (Phase 0) and return parsed features.json."""
    
    # 1. Update design status to decomposing
    _update_design_status(design_entry.db_id, "decomposing")
    
    # 2. Create design-level integration worktree (§9.6 model)
    design_id = design_entry.db_id or file_hash(design_entry.path)
    design_branch = f"autopilot/run-{design_id[:8]}"
    design_worktree = _create_integration_worktree(project_path, design_id, design_branch, logger)
    
    # 3. Copy design.md into worktree's .hephaestus/
    wt_heph = design_worktree / ".hephaestus"
    wt_heph.mkdir(parents=True, exist_ok=True)
    shutil.copy2(design_entry.path, wt_heph / "design.md")
    
    # 4. Launch autopilot-phase0 workflow
    description = (
        f"Phase 0: Feature Architect for {design_entry.name}\n"
        f"Design Document: ./.hephaestus/design.md\n"
        f"Project Path: . (your working directory)\n"
        f"Project Root (absolute): {project_path}\n"
        f"Design ID: {design_entry.db_id or ''}"
    )
    launch_params = {
        "design_document": str(design_entry.path),
        "project_path": str(project_path),
        "design_id": design_entry.db_id or "",
    }
    phase0_exec_id = sdk.start_workflow(
        definition_id="autopilot-phase0",
        description=description,
        working_directory=str(design_worktree),
        launch_params=launch_params,
        design_id=design_entry.db_id,
    )
    # Mark this as a design-type workflow
    _set_workflow_type(phase0_exec_id, "design")
    
    # 5. Poll until Phase 0 completes
    wf_status = _poll_workflow(phase0_exec_id, "autopilot-phase0", logger,
                                timeout=MAX_PHASE0_TIME)
    if wf_status != "completed":
        logger.error(f"Phase 0 failed: {wf_status}")
        _cleanup_worktree(design_worktree, design_branch, project_path, logger)
        return None, None
    
    # 6. Read and validate features.json
    features_path = design_worktree / ".hephaestus" / "features.json"
    if not features_path.exists():
        logger.error("Phase 0 completed but features.json not written")
        _cleanup_worktree(design_worktree, design_branch, project_path, logger)
        return None, None
    
    try:
        features_json = json.loads(features_path.read_text())
        _validate_features_json(features_json)  # raises ValueError on invalid
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Invalid features.json: {e}")
        _cleanup_worktree(design_worktree, design_branch, project_path, logger)
        return None, None
    
    # 7. Create permanent designs/ folder
    designs_folder = _create_designs_folder(project_path, design_entry, logger)
    
    # 8. Copy Phase 0 outputs to permanent storage
    shutil.copy2(design_entry.path, designs_folder / "design.md")
    shutil.copy2(features_path, designs_folder / "features.json")
    for feature in features_json["features"]:
        fid = feature["id"]
        scope_src = design_worktree / ".hephaestus" / "features" / fid / "scope.md"
        scope_dst = designs_folder / "features" / fid / "scope.md"
        scope_dst.parent.mkdir(parents=True, exist_ok=True)
        if scope_src.exists():
            shutil.copy2(scope_src, scope_dst)
    
    # 9. Create Feature DB records
    feature_records = _create_feature_records(design_entry.db_id, features_json,
                                               designs_folder, logger)
    
    # 10. Update design status to active
    _update_design_status(design_entry.db_id, "active",
                           designs_folder=str(designs_folder))
    
    # 11. Discard Phase 0 worktree (outputs are now in permanent storage)
    _cleanup_worktree(design_worktree, design_branch, project_path, logger)
    
    return features_json, designs_folder
```

Constants to add:
```python
MAX_PHASE0_TIME = 3600  # 1 hour for Phase 0
MAX_PARALLEL_FEATURES = 4  # max concurrent feature pipelines
```

### 6.3 Stage 2: `run_feature_pipelines`

```python
def run_feature_pipelines(sdk, design_entry, features_json, designs_folder,
                          project_path, logger, state, max_iterations):
    """Run all feature pipelines with parallel/sequential control."""
    
    features = features_json["features"]
    feature_results = {}  # feature_id → DesignStatus
    
    # Resolve execution groups (topological order by depends_on)
    execution_groups = _resolve_execution_order(features, logger)
    # Returns: list of groups, where each group is a list of feature dicts
    # that can run concurrently. Groups run sequentially relative to each other.
    
    for group in execution_groups:
        if len(group) == 1:
            # Single feature — run directly
            feature = group[0]
            if _should_skip(feature, feature_results):
                logger.info(f"Skipping {feature['id']} (dependency failed)")
                feature_results[feature['id']] = DesignStatus.SKIPPED
                _update_feature_status(feature['id'], design_entry.db_id, "skipped")
                continue
            result = _run_one_feature(sdk, design_entry, feature, designs_folder,
                                       project_path, logger, state, max_iterations)
            feature_results[feature['id']] = result
        else:
            # Multiple parallel features — use ThreadPoolExecutor
            parallel = [f for f in group if not _should_skip(f, feature_results)]
            skipped = [f for f in group if _should_skip(f, feature_results)]
            
            for f in skipped:
                logger.info(f"Skipping {f['id']} (dependency failed)")
                feature_results[f['id']] = DesignStatus.SKIPPED
                _update_feature_status(f['id'], design_entry.db_id, "skipped")
            
            if parallel:
                logger.info(f"Running {len(parallel)} features in parallel: "
                            f"{[f['id'] for f in parallel]}")
                
                with ThreadPoolExecutor(max_workers=min(len(parallel), MAX_PARALLEL_FEATURES)) as ex:
                    future_to_feature = {
                        ex.submit(_run_one_feature, sdk, design_entry, f, designs_folder,
                                  project_path, logger, state, max_iterations): f
                        for f in parallel
                    }
                    for future in as_completed(future_to_feature):
                        feat = future_to_feature[future]
                        try:
                            result = future.result()
                        except Exception as e:
                            logger.error(f"Feature {feat['id']} raised exception: {e}")
                            result = DesignStatus.FAILED
                        feature_results[feat['id']] = result
    
    return feature_results
```

### 6.4 `_resolve_execution_order`

```python
def _resolve_execution_order(features: list, logger) -> list:
    """
    Resolve features into execution groups using depends_on and execution fields.
    
    Returns a list of groups. Each group is a list of features that can run
    concurrently. Groups must run sequentially (group N+1 waits for group N).
    
    Algorithm:
    1. Build a dependency graph from depends_on fields.
    2. Topological sort (Kahn's algorithm) to determine ordering.
    3. Features with execution == "sequential" and no unmet deps are placed
       in their own single-feature group (forces sequential execution).
    4. Features with execution == "parallel" are grouped with other parallel
       features at the same topological depth.
    """
    from collections import defaultdict, deque
    
    # Build adjacency and in-degree
    id_to_feature = {f["id"]: f for f in features}
    in_degree = {f["id"]: 0 for f in features}
    dependents = defaultdict(list)  # id → list of features that depend on it
    
    for feature in features:
        for dep in (feature.get("depends_on") or []):
            if dep not in id_to_feature:
                logger.warning(f"Feature {feature['id']} depends_on unknown id '{dep}' — ignoring")
                continue
            in_degree[feature["id"]] += 1
            dependents[dep].append(feature["id"])
    
    # Kahn's algorithm — process in layers
    queue = deque([fid for fid, deg in in_degree.items() if deg == 0])
    execution_groups = []
    
    while queue:
        # Collect current layer
        layer = list(queue)
        queue.clear()
        
        # Separate parallel features from sequential ones
        parallel_layer = [id_to_feature[fid] for fid in layer
                          if id_to_feature[fid].get("execution") == "parallel"]
        sequential_in_layer = [id_to_feature[fid] for fid in layer
                                if id_to_feature[fid].get("execution") != "parallel"]
        
        # Parallel features in this layer run together
        if parallel_layer:
            execution_groups.append(parallel_layer)
        
        # Sequential features in this layer each get their own group
        for feat in sequential_in_layer:
            execution_groups.append([feat])
        
        # Reduce in-degrees
        for fid in layer:
            for dep_id in dependents[fid]:
                in_degree[dep_id] -= 1
                if in_degree[dep_id] == 0:
                    queue.append(dep_id)
    
    # Check for cycles
    if sum(in_degree.values()) > 0:
        cyclic = [fid for fid, deg in in_degree.items() if deg > 0]
        logger.error(f"Cycle detected in feature depends_on: {cyclic}")
        # Fall back to sequential execution of all features
        return [[f] for f in features]
    
    logger.info(f"Execution plan: {len(execution_groups)} groups")
    for i, group in enumerate(execution_groups):
        ids = [f['id'] for f in group]
        mode = "parallel" if len(group) > 1 else "sequential"
        logger.info(f"  Group {i+1} ({mode}): {ids}")
    
    return execution_groups
```

### 6.5 `_run_one_feature`

```python
def _run_one_feature(sdk, design_entry, feature, designs_folder,
                     project_path, logger, state, max_iterations):
    """Run a single feature through the full 12-phase pipeline."""
    
    fid = feature["id"]
    fname = feature["name"]
    logger.info(f"Starting feature: {fid} ({fname})")
    
    # 1. Update Feature DB status to active
    feature_db_id = _get_feature_db_id(fid, design_entry.db_id)
    _update_feature_status(feature_db_id, design_entry.db_id, "active")
    
    # 2. Create feature record folder
    feature_record = designs_folder / "features" / fid
    feature_record.mkdir(parents=True, exist_ok=True)
    (feature_record / "docs").mkdir(exist_ok=True)
    
    # 3. Create per-feature integration worktree (§9.6 model, feature-scoped)
    #    Branch from main (or the parent design's integration branch if designs
    #    become concurrent — for now, from main)
    feature_branch = f"autopilot/{design_entry.name.lower().replace(' ', '-')}-{fid}"
    feature_worktree = _create_integration_worktree(
        project_path, f"{design_entry.db_id[:8]}-{fid}", feature_branch, logger
    )
    
    # 4. Populate worktree .hephaestus/ with feature context
    wt_heph = feature_worktree / ".hephaestus"
    wt_heph.mkdir(parents=True, exist_ok=True)
    
    # Copy design.md
    shutil.copy2(design_entry.path, wt_heph / "design.md")
    
    # Copy features.json (agents may need cross-feature context)
    shutil.copy2(designs_folder / "features.json", wt_heph / "features.json")
    
    # Copy this feature's scope.md
    scope_dst = wt_heph / "features" / fid / "scope.md"
    scope_dst.parent.mkdir(parents=True, exist_ok=True)
    scope_src = designs_folder / "features" / fid / "scope.md"
    if scope_src.exists():
        shutil.copy2(scope_src, scope_dst)
    
    # 5. Copy phase YAML files for forensics
    feature_phases_dir = feature_record / "docs" / "phase_prompts"
    feature_phases_dir.mkdir(parents=True, exist_ok=True)
    autopilot_yaml_dir = HEPHAESTUS_DIR / "config" / "workflows" / "autopilot"
    for pf in sorted(autopilot_yaml_dir.glob("*.yaml")):
        if pf.name != "workflow.yaml":
            shutil.copy2(pf, feature_phases_dir / pf.name)
    
    # 6. Write initial pipeline_metrics.json for this feature
    _write_feature_metrics(feature_record / "docs", design_entry, feature, fid)
    
    # 7. Build task description (per-feature, references scope.md not design.md)
    feature_scope_path = f".hephaestus/features/{fid}/scope.md"
    description = (
        f"Autopilot: {design_entry.name} / Feature: {fname}\n"
        f"Feature ID: {fid}\n"
        f"Feature Scope: {feature_scope_path} (your primary input — read this first)\n"
        f"Design Document: ./.hephaestus/design.md (for Scope Review and Product Validation)\n"
        f"Project Path: . (your working directory)\n"
        f"Project Root (absolute): {project_path}\n"
        f"Feature Folder: {feature_record}\n"
        f"Docs Path (absolute): {feature_record / 'docs'}\n"
        f"File Ownership: {feature.get('files', [])}\n"
        f"Depends On: {feature.get('depends_on', [])}\n"
        f"Implementation code goes in your working directory.\n"
        f"Generated docs go in ./docs/\n"
        f"Read scope.md for what to build. Do not implement features outside your file ownership."
    )
    
    launch_params = {
        "design_document": str(design_entry.path),
        "project_path": str(project_path),
        "feature_id": fid,
        "feature_scope": feature_scope_path,
        "project_context": f"Building feature: {fname}. Scope: {feature_scope_path}",
    }
    
    # 8. Launch autopilot workflow for this feature
    exec_id = sdk.start_workflow(
        definition_id="autopilot",
        description=description,
        working_directory=str(feature_worktree),
        launch_params=launch_params,
        design_id=design_entry.db_id,
    )
    _set_workflow_type(exec_id, "feature")
    _link_workflow_to_feature(exec_id, feature_db_id)
    
    # 9. Update Feature record with workflow_id
    _update_feature_workflow(feature_db_id, exec_id)
    
    # 10. Poll until feature workflow completes
    wf_status = run_single_workflow(
        sdk, exec_id, str(project_path), description, logger,
        launch_params=launch_params,
        state=state,
        max_iterations=max_iterations,
        design_id=design_entry.db_id,
    )
    
    # 11. Copy feature artifacts to permanent record
    _sweep_feature_artifacts(feature_worktree, feature_record / "docs", logger)
    
    # 12. Update Feature status
    if wf_status in ("completed",):
        status = DesignStatus.COMPLETED
        _update_feature_status(feature_db_id, design_entry.db_id, "completed")
    elif wf_status in ("skipped",):
        status = DesignStatus.SKIPPED
        _update_feature_status(feature_db_id, design_entry.db_id, "skipped")
    else:
        status = DesignStatus.FAILED
        _update_feature_status(feature_db_id, design_entry.db_id, "failed",
                               error=f"workflow status: {wf_status}")
    
    # 13. Keep the feature worktree (agents merged their branches; worktree already
    #     checked out on feature_branch after phase 11 git_commit_push merges to main)
    #     The orchestrator removes the worktree only after recording all artifacts.
    _cleanup_worktree(feature_worktree, feature_branch, project_path, logger)
    
    logger.info(f"Feature {fid} complete: {status}")
    return status
```

### 6.6 Stage 3: `run_design_aggregate`

```python
def run_design_aggregate(design_entry, feature_results, designs_folder, logger):
    """Orchestrator-level post-processing: aggregate report, final design status."""
    
    all_completed = all(s == DesignStatus.COMPLETED for s in feature_results.values())
    any_failed = any(s == DesignStatus.FAILED for s in feature_results.values())
    
    # Write design_metrics.json
    _write_design_metrics(design_entry, feature_results, designs_folder)
    
    # Generate design_report.html
    _generate_design_report_html(design_entry, feature_results, designs_folder, logger)
    
    # Update AutopilotDesign status
    final_status = "completed" if all_completed else ("failed" if any_failed else "completed")
    _update_design_status(design_entry.db_id, final_status)
    
    # Build summary FeatureReport (for compatibility with existing callers)
    report = FeatureReport(
        design_name=design_entry.name,
        project_path=str(designs_folder.parent),
        feature_folder=str(designs_folder),
        design_document=str(design_entry.path),
        iterations=1,
        total_time_seconds=0,
        qa_passed=all_completed,
        product_validated=all_completed,
        stop_reason="completed" if all_completed else "failed",
    )
    
    return (DesignStatus.COMPLETED if all_completed else DesignStatus.FAILED), report
```

### 6.7 Helper: `_create_designs_folder`

```python
def _create_designs_folder(project_path, design_entry, logger):
    """Create the permanent record folder: <project>/designs/<ts>_<name>_<design-id>/"""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = design_entry.name.lower().replace(" ", "_")[:40]
    design_id_short = (design_entry.db_id or "unknown")[:8]
    folder_name = f"{timestamp}_{safe_name}_{design_id_short}"
    designs_folder = project_path / "designs" / folder_name
    designs_folder.mkdir(parents=True, exist_ok=True)
    (designs_folder / "features").mkdir(exist_ok=True)
    logger.info(f"Designs folder: {designs_folder}")
    return designs_folder
```

### 6.8 Helper: `_generate_design_report_html`

Generate `designs_folder/design_report.html` using Jinja2 with:
- Summary table: feature name, status, time, QA passed, product validated
- Aggregate cost and time across all features
- List of PRs merged
- Forensics highlights per feature

Template file: `src/autopilot/templates/design_report.html`

---

## 7. CLI Changes (`src/cli/commands/autopilot.py`)

### 7.1 `add_to_queue` — store file_path in DB, do not copy

**Current behavior:** copies file to `<project>/docs/design-queue/` and that copy
is what gets processed.

**New behavior:** resolve the absolute path, create an `AutopilotDesign` DB record
with `file_path = str(abs_path)`, do NOT copy the file.

```python
def add_to_queue(args):
    """Register a design document in the DB queue. File can live anywhere."""
    source = Path(args.file).resolve()
    if not source.exists():
        print(f"File not found: {source}")
        return 1
    
    project_path = Path(args.project_path).resolve()
    
    try:
        resp = requests.post(
            "http://127.0.0.1:8300/api/autopilot/designs/add",
            json={
                "file_path": str(source),
                "project_path": str(project_path),
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            print(f"Added to queue: {data['name']} (id={data['id'][:8]})")
            print(f"File: {source}")
            return 0
        else:
            print(f"Error: {resp.status_code} - {resp.text}")
            return 1
    except requests.exceptions.ConnectionError:
        print("Error: Backend not running. Start it with: heph start")
        return 1
```

### 7.2 API endpoint: `POST /api/autopilot/designs/add`

Add to `src/mcp/autopilot_api.py`:

```python
@router.post("/designs/add")
async def add_design(body: dict, db: Session = Depends(get_db)):
    """Register a design document by file path. File can live anywhere."""
    file_path = Path(body["file_path"]).resolve()
    project_path = Path(body["project_path"]).resolve()
    
    if not file_path.exists():
        raise HTTPException(400, f"File not found: {file_path}")
    
    # Find or create project
    project = db.query(AutopilotProject).filter_by(
        base_dir=str(project_path)
    ).first()
    if not project:
        project = AutopilotProject(
            id=f"proj-{uuid4().hex[:8]}",
            name=project_path.name,
            base_dir=str(project_path),
            is_active=True,
        )
        db.add(project)
    
    # Check for duplicate (same file_path)
    existing = db.query(AutopilotDesign).filter_by(
        project_id=project.id,
        file_path=str(file_path)
    ).first()
    if existing:
        return {"id": existing.id, "name": existing.name, "status": existing.status}
    
    content_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()[:16]
    name = file_path.stem.replace("_", " ").replace("-", " ").title()
    
    design = AutopilotDesign(
        id=f"des-{uuid4().hex[:8]}",
        project_id=project.id,
        filename=file_path.name,        # kept for backward compat
        file_path=str(file_path),        # new: absolute path
        name=name,
        ordinal=_next_ordinal(db, project.id),
        size_bytes=file_path.stat().st_size,
        extension=file_path.suffix,
        content_hash=content_hash,
        status="pending",
    )
    db.add(design)
    db.commit()
    
    return {"id": design.id, "name": design.name, "status": design.status}
```

### 7.3 `pick_next_design` — read file_path first, fall back to filename

Update `pick_next_design` in `orchestrator.py`:

```python
# When building DesignEntry from DB record, prefer file_path over filename
design_path = None
if design.file_path and Path(design.file_path).exists():
    design_path = Path(design.file_path)
else:
    # Fallback: filename relative to project base dir + DESIGN_SUBDIR
    design_path = Path(project.base_dir) / DESIGN_SUBDIR / design.filename
    if not design_path.exists():
        logger.warning(f"Design file not found: {design_path}")
        design.status = "failed"
        db.commit()
        continue
```

---

## 8. Phase YAML Updates (per-feature scope references)

The existing `workflow.yaml` `phase_1_task_prompt` currently reads the full
design doc. Update it to read `scope.md` first:

```yaml
# In workflow.yaml launch_template.phase_1_task_prompt:
phase_1_task_prompt: |
  Phase 1: Product Requirements Extraction

  **Feature Scope:** {feature_scope} (your PRIMARY input — read this first)
  **Design Document:** ./.hephaestus/design.md (for full context if needed)
  **Feature ID:** {feature_id}
  **Project Path:** . (your current working directory)
  ...
```

Also add `{feature_id}` and `{feature_scope}` to `launch_template.parameters`:
```yaml
- name: feature_id
  label: Feature ID
  type: text
  required: false
- name: feature_scope
  label: Feature Scope Document Path
  type: text
  required: false
```

These are optional (empty string for designs without the Feature model, which can
still run the current flow for backward compatibility).

---

## 9. Permanent Storage Layout

```
<project>/
  designs/
    20260701-143022_auth_system_fb36c8e3/   ← designs_folder
      design.md                              ← copy of original design doc
      features.json                          ← copy of Phase 0 output
      design_report.html                     ← generated by Stage 3 aggregate
      design_metrics.json                    ← total time, cost, feature count
      features/
        auth/
          scope.md                           ← copy from Phase 0 output
          feature_report.html                ← generated per feature
          docs/
            requirements_analysis.md
            architecture.md
            review_report.md
            doc_review_report.md
            security_report.md
            qa_report.md
            product_validation.md
            forensics_report.md
            pipeline_metrics.json
            phase_prompts/
        session/
          scope.md
          ...
        admin/
          scope.md
          ...
```

---

## 10. Implementation Order

**Must implement in this order (each step is a prerequisite for the next):**

### Step 0 — Run B fixes (before touching Feature model)
1. Fix spec gate not firing on qa_validation completion
2. Fix abandoned required phase → impasse (not skip)
3. Run smoke test — seeded failing test must trigger GOTO, abandoned phase must trigger impasse
4. **Do not start Step 1 until smoke test is green.**

### Step 1 — DB schema
1. Add `Feature` class to `src/core/database.py`
2. Add `file_path`, `designs_folder`, `phase0_workflow_id` columns to `AutopilotDesign`
3. Add `workflow_type`, `feature_id` columns to `Workflow`
4. Add `_migrate_feature_model_columns()` and call it from `DatabaseManager.__init__`
5. Verify with: `python -c "from src.core.database import Feature; print('OK')"`

### Step 2 — Phase 0 YAML and workflow registration
1. Create `config/workflows/autopilot-phase0/` directory
2. Write `workflow.yaml` and `01_feature_architect.yaml`
3. Register `autopilot-phase0` in `src/workflow_registry.py`
4. Smoke test Phase 0 standalone on a simple design doc

### Step 3 — Orchestrator refactor
1. Add helper functions: `_create_integration_worktree`, `_cleanup_worktree`,
   `_create_designs_folder`, `_create_feature_records`, `_update_feature_status`,
   `_update_design_status`, `_set_workflow_type`, `_link_workflow_to_feature`,
   `_validate_features_json`, `_should_skip`
2. Implement `_resolve_execution_order`
3. Implement `run_phase0`
4. Implement `_run_one_feature`
5. Implement `run_feature_pipelines`
6. Implement `run_design_aggregate` + `_generate_design_report_html`
7. Rewrite `run_single_design` to call these three stages
8. Run a two-feature design test (one parallel pair)

### Step 4 — CLI and API
1. Rewrite `add_to_queue` in `src/cli/commands/autopilot.py`
2. Add `POST /api/autopilot/designs/add` to `src/mcp/autopilot_api.py`
3. Update `pick_next_design` to use `file_path` column
4. Test: `heph autopilot add ~/my-designs/auth.md --project-path ~/my-project`

### Step 5 — Phase YAML updates
1. Update `workflow.yaml` launch_template to pass `feature_scope` and `feature_id`
2. Update each phase YAML's additional_notes to reference `scope.md` as primary input
   (Phases 1, 3 — highest priority; Phases 2, 10 already read design.md too per doc)
3. Run full end-to-end test with a multi-feature design

### Step 6 — Feature report
1. Create `src/autopilot/templates/design_report.html` (Jinja2)
2. Implement `_generate_design_report_html` in orchestrator
3. Verify HTML is written to `designs_folder/design_report.html` after aggregate

---

## 11. Testing

### Unit tests
- `test_resolve_execution_order.py`: parallel features, sequential features, depends_on DAG, cycles
- `test_validate_features_json.py`: valid JSON, missing required fields, duplicate IDs, cycle in depends_on, overlapping file paths
- `test_create_feature_records.py`: DB records created correctly, status starts pending

### Integration tests
- `test_phase0_workflow.py`: Phase 0 runs against a real simple design doc, produces valid features.json and scope.md
- `test_feature_model_single.py`: A single-feature design runs end-to-end; produces feature report; design_report.html written
- `test_feature_model_parallel.py`: A two-feature parallel design runs with both features executing concurrently
- `test_feature_model_sequential.py`: Feature A → Feature B sequential; B does not start until A completes
- `test_feature_dependency_failed.py`: Feature A fails; Feature B (depends_on A) is marked skipped

### Regression
- Run the existing 74 tests; all should pass
- Smoke test: single-feature design on the calculator project (mirrors Run A/B setup)
