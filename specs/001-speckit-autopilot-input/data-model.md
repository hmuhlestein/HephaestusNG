# Phase 1 Data Model: Spec Kit-Aware Autopilot Input

## SpecKitFeature (derived, not persisted)

Represents one `specs/<NNN>-<name>/` directory discovered on disk. Not a database table — resolved fresh from the filesystem each time it's needed (detection is unconditional per the Awareness Model in spec.md), the same way `design.md`'s content is read fresh rather than cached.

| Field | Type | Notes |
|---|---|---|
| `number` | string (3-digit) | The `NNN` prefix; Spec Kit's own sequential-numbering convention |
| `name` | string (slug) | The `<name>` suffix |
| `directory` | path | `specs/<NNN>-<name>/` |
| `has_spec` | bool | `spec.md` present — **required** for the directory to be recognized at all (FR-001) |
| `has_plan` | bool | `plan.md` present |
| `has_tasks` | bool | `tasks.md` present |
| `present_files` | list[string] | Every file/subdirectory actually found (`spec.md`, `plan.md`, `tasks.md`, `data-model.md`, `contracts/`, `research.md`, `quickstart.md`, `checklists/`, ...) — copied into the worktree as a whole (FR-002a); this list is what `build_input_manifest` reports, the same "here's what's present" shape used for every other phase input |
| `unresolved_clarifications` | list[string] | Every `[NEEDS CLARIFICATION: ...]` marker text still present in `spec.md` — used only by the readiness check (FR-011); never blocks `start` (FR-007) |

Note: `SpecKitFeature` no longer carries extracted file *content* as typed fields (an earlier draft of this data model did) — per research.md's whole-folder-copy decision, Hephaestus copies the files, and each phase's own prompt reads whichever ones it's told to read directly from the worktree, rather than Hephaestus extracting and injecting content per phase.

**Validation rules**:
- A directory under `specs/` with no `spec.md` is not a SpecKitFeature — it is invisible to detection (FR-001 requires `spec.md`'s presence).
- `number` and `name` are derived from the directory name, not re-validated against `spec.md`'s own `**Feature Branch**` field — the directory name is the source of truth for selection (matches how `--feature <NNN-name>` and the dashboard picker both address it).

**Selection (not a stored field — resolved per invocation)**:
- Exactly one SpecKitFeature is "selected" for a given `heph autopilot start` invocation: the one named by `--feature`, or the sole one found when exactly one exists, or none when zero exist (falls through to requiring `design.md`, FR-005) or more than one exists without `--feature` (errors, FR-006).

## AutopilotProject (existing table, one new column)

`src/core/database.py:1141`. One new field on the existing model:

| Field | Type | Default | Notes |
|---|---|---|---|
| `spec_kit_auto_scan` | Boolean | `False` | Opts this project into automatic building of detected Spec Kit features (FR-013). Same shape and precedent as the existing `review_mode` column on this same table. |

**State transitions**: `False → True` (user enables via dashboard toggle or `PUT /projects/{project_id}`) and `True → False` (disable) are the only transitions. Toggling either direction has no effect on builds already queued or in progress — it only changes whether the *next* design-queue scan (`scan_design_queue`) considers `specs/` entries at all (FR-016).

## DesignQueueEntry (existing concept in `scan_design_queue`, one new source)

Not a new table — `scan_design_queue` (`src/autopilot/orchestrator/queue.py:164`) already produces `DesignEntry` objects from flat files under `.hephaestus/designs/`/`docs/spec-queue`. This feature adds a second source feeding the same list: when `spec_kit_auto_scan` is enabled for the project being scanned, each SpecKitFeature not yet processed (by the same content-hash/already-processed tracking `scan_design_queue` already uses) becomes an additional `DesignEntry`.

**Validation rules**:
- A SpecKitFeature already built (has a completed or in-progress `Feature`/`Workflow` row traceable to it) MUST NOT produce a new `DesignEntry` on a later scan — reuses the existing content-hash/self-heal logic, keyed on the feature directory's content rather than a single file's hash (extension needed, tracked as a task).

## ReadinessCheckResult (ephemeral, CLI output only — never persisted)

Produced by `heph autopilot check --feature <NNN-name>` (FR-011). Not written to the database; exists only for the duration of that command's output.

| Field | Type | Notes |
|---|---|---|
| `feature_directory` | path | Which SpecKitFeature was checked |
| `unresolved_clarifications` | list[string] | From `SpecKitFeature.unresolved_clarifications` |
| `missing_files` | list[string] | e.g. `["plan.md"]` when absent |
| `ready` | bool | `true` iff both lists above are empty |
