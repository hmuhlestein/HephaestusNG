# Design-Level Workflow Type: Feature vs. Bug Fix

## Goal

Every design that enters the queue today runs through the same 12-phase
`autopilot` pipeline (`config/workflows/autopilot/`) — full requirements
extraction, architecture design, development, three review gates, QA,
product validation, docs, git, deploy. That pipeline is sized for adding
new capability. A one-line bug fix pays the same fixed cost: an
architecture-design phase invents structure for a change that doesn't need
any, and product_requirements re-derives requirements from a bug report
that already states them.

This adds a **workflow type** to each design — `feature` (today's behavior,
unchanged) or `bugfix` (a new, shorter pipeline) — selectable when the
design is added, auto-detected by default, with a new `bugfix` workflow
definition to run it.

## Current State

**Design → Workflow, today:**

1. `AddDesignModal.tsx` posts `{name, content, extension}` to
   `POST /projects/{id}/designs` (`DesignAddRequest`,
   `design_file_routes.py:57`). No type is captured anywhere.
2. The file lands in `.hephaestus/designs/`, gets synced into an
   `AutopilotDesign` row (`database.py:1221`) with no type column.
3. `run_phase0` (`pipeline.py:861`) always launches the `feature_architect`
   workflow to decompose the design into `features.json`.
4. For each decomposed feature, `_run_one_feature` (`pipeline.py:1678`)
   always calls `run_single_workflow(sdk, "autopilot", ...)`
   (`pipeline.py:1890`) — the definition_id is a hardcoded literal, not
   read from anywhere per-design or per-feature.
5. `"autopilot"` itself isn't discovered like other workflows — it's
   specially assembled at process start from `src/autopilot/phases.py`,
   which loads `config/workflows/autopilot/` directly. Every *other*
   directory under `config/workflows/` that contains a `workflow.yaml` is
   auto-discovered by `src/workflow_registry.py:get_all_workflow_definitions()`
   and registered under its directory name as `definition_id`. This is
   already how `config/workflows/feature_architect/` (Phase 0) gets
   registered — a sibling `config/workflows/bugfix/` directory would be
   picked up the same way with zero registry code changes.

**Implication:** the type has to be decided before Phase 0 runs (so the UI
can show it immediately in the queue) and threaded through two hops —
`AutopilotDesign` → `features.json` → `Feature` — before it reaches the one
line that currently hardcodes `"autopilot"`.

## Proposed Design

### 1. Data model

Add `AutopilotDesign.workflow_type` (`String(20)`, default `"feature"`,
values `"feature"` / `"bugfix"`), migrated the same idempotent
`ALTER TABLE` way as every other column in `schema_migrations.py`.

Add the same column to `Feature` (denormalized copy, not a join) — a
feature's pipeline can be resumed long after its parent design row's
lifecycle is otherwise irrelevant, and every other per-feature run
parameter (`design_id`, worktree path, etc.) is already copied down at
decomposition time rather than looked up through the parent on every read.

`workflow_type` maps to `definition_id` via a fixed table:

```python
WORKFLOW_TYPE_DEFINITION_IDS = {"feature": "autopilot", "bugfix": "bugfix"}
```

Keeping `"feature"` mapped to the existing `"autopilot"` id (not renaming
it) avoids touching every other place that already matches on
`definition_id == "autopilot"` (`DESIGN_WORKFLOW_DEFINITION_IDS` in
`constants.py`, the `phase0_wf` queries in `pipeline.py`, etc.).

### 2. API surface

`DesignAddRequest` gains:

```python
workflow_type: Optional[Literal["feature", "bugfix"]] = None  # None = auto-detect
```

`add_project_design` resolves `None` via the detector (below) before
storing the `AutopilotDesign` row, so the queue UI can show a real badge
immediately rather than "detecting…" until Phase 0 finishes minutes later.

### 3. Propagation into the pipeline

- `run_phase0` passes `design_entry.workflow_type` into the Phase 0 launch
  context (a prompt-visible field, same as it already passes the design
  name/content) — mechanical, no behavior change to Phase 0 itself except
  it now sees the type as an FYI.
- Phase 0's `features.json` output already carries per-feature dicts
  created by `features.py`; when each `Feature` row is created from that
  decomposition, copy `design_entry.workflow_type` onto
  `Feature.workflow_type`.
- `_run_one_feature` (`pipeline.py:1888-1890`) changes from the hardcoded
  literal to:
  ```python
  definition_id = WORKFLOW_TYPE_DEFINITION_IDS.get(feat_record.workflow_type, "autopilot")
  wf_status = run_single_workflow(sdk, definition_id, ...)
  ```

### 4. Auto-detection

**Decision: deterministic heuristic at add-time.** A pure function
`detect_workflow_type(name: str, content: str) -> Literal["feature", "bugfix"]`
scoring keyword hits — `bug|fix|broken|regression|crash|doesn't work|
incorrect|error` vs. `add|implement|new feature|support for` — weighted,
title matches counting more than body matches. Zero cost, zero latency,
runs synchronously inside `add_project_design`, resolved type is stored
and visible in the queue instantly.

Rejected alternative: folding the classification into Phase 0's existing
structured output (`feature_architect` already reads the whole design doc
to decompose it, so it could emit `workflow_type` alongside `features.json`
at higher accuracy). Not worth it here — the type wouldn't be known until
Phase 0 completes (typically minutes after the design is added), so the
queue UI would sit on "detecting…" and the add-time selector couldn't
confirm its own auto-detect choice synchronously. The manual dropdown
(below) always overrides the heuristic, so a wrong guess is a one-click fix
rather than a pipeline restart — that safety net is what makes the cheaper,
occasionally-wrong heuristic an acceptable default.

### 5. UI

`AddDesignModal.tsx`: a third control alongside Name/Format — a
`Workflow Type` select: `Auto-detect` (default) / `Feature` / `Bug Fix`.
Wired straight into `DesignAddRequest.workflow_type` (`null` for
auto-detect).

`DesignQueuePanel.tsx`: a small badge on each queue item showing the
resolved type (mirrors the existing `StatusBadge` pattern already used for
design/feature status).

### 6. New `bugfix` workflow definition

New `config/workflows/bugfix/workflow.yaml` (+ per-phase prompt YAMLs),
same schema as `config/workflows/autopilot/workflow.yaml`, auto-discovered
by `get_all_workflow_definitions()` — no registry code changes needed.

Finalized phase list:

```
development → adversarial_review → security_review → qa_validation
  → git_expert → deploy
```

Dropped relative to the full pipeline:
- **product_requirements** — a bug report already states the expected
  behavior; re-deriving formal requirements from it is overhead a feature
  needs but a fix doesn't.
- **architecture_design** — a bug fix works within existing structure by
  definition; inventing new architecture for it is the wrong shape of work
  and the source of the "over-engineered one-line fix" failure mode this
  whole change exists to avoid.
- **architectural_review** — nothing new was architected, so there's
  nothing for this gate to review.
- **doc_review** — most bug fixes don't change documented behavior; kept
  out of the default list but easy to re-add if that's wrong in practice.
- **product_validation** — dropped by explicit decision. Its job (verify
  the change against the original spec) is redundant with `qa_validation`
  once there's no separate requirements doc to check against — the bug
  report itself is the spec, and `qa_validation`'s own acceptance criteria
  cover "is the reported bug actually gone."

Kept: `adversarial_review`/`security_review`/`qa_validation` — a fix can
still introduce a regression or a security hole, and `qa_validation` is now
the pipeline's sole verification that the original bug is fixed.

`max_review_runs` and other thresholds start as a straight copy of the
matching `autopilot` eval_points (e.g. `qa_validation`'s existing value),
tuned independently later since it's now a fully separate config file — the
recent fix that makes a capped-out review phase's `completion_summary` say
so explicitly (`phase_transitions.py`'s `_cap_out_review_phase`) applies
here too, so a bugfix run that gets capped out will already show up
correctly in its own phase history.

## Decisions

1. Auto-detection: deterministic keyword heuristic at add-time (§4).
2. Phase list: `development → adversarial_review → security_review →
   qa_validation → git_expert → deploy` — `product_validation` explicitly
   dropped, on top of the already-dropped
   product_requirements/architecture_design/architectural_review/doc_review
   (§6).
3. `max_review_runs`/thresholds start as a straight copy of the matching
   `autopilot` eval_points, tuned independently later (§6).

## Sequencing

1. `AutopilotDesign.workflow_type` / `Feature.workflow_type` columns +
   migration.
2. `detect_workflow_type` heuristic + wire into `add_project_design`.
3. `AddDesignModal.tsx` selector + `DesignAddRequest.workflow_type` +
   queue badge.
4. Propagate `workflow_type` through `run_phase0` → `features.json` →
   `Feature.workflow_type` → `_run_one_feature`'s `run_single_workflow`
   call.
5. Author `config/workflows/bugfix/` (workflow.yaml + phase YAMLs).

Steps 1–4 are safe to ship independently of step 5 (every design defaults
to `"feature"` → `"autopilot"`, identical to current behavior) — step 5 is
the one that actually changes what happens for a design marked `bugfix`.
