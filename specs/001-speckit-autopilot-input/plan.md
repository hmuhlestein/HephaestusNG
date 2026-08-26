# Implementation Plan: Spec Kit-Aware Autopilot Input

**Branch**: `001-speckit-autopilot-input` | **Date**: 2026-08-26 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-speckit-autopilot-input/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command; its definition describes the execution workflow.

**Related documents** (this feature only reaches one of them per phase today, per FR-002/003/004 — see the Constitution Check and Complexity Tracking notes below for why that's a gap this plan intentionally does not paper over):

- [research.md](./research.md) — Phase 0 technical decisions and rationale
- [data-model.md](./data-model.md) — entities: `SpecKitFeature`, the new `AutopilotProject` column, `DesignQueueEntry`, `ReadinessCheckResult`
- [contracts/cli.md](./contracts/cli.md) — `heph autopilot start`/`check` flag and resolution-order contract
- [contracts/api.md](./contracts/api.md) — `PUT /projects/{project_id}` field addition, new `GET .../spec-kit-features`
- [quickstart.md](./quickstart.md) — 6 runnable validation scenarios, one per user story
- [tasks.md](./tasks.md) — the 50-task breakdown, once `/speckit-tasks` has run

## Summary

Let `heph autopilot start` accept an already-Spec-Kit-initialized project (`specs/<NNN>-<name>/`) as an alternative to a hand-written `design.md`. Mechanism: the selected feature's *entire directory* is copied into the worktree once (FR-002a) — not one file extracted per phase — and each phase's own prompt reads whichever files it needs: `product_requirements` reads `spec.md`, `architecture_design` reads `plan.md` plus its Phase 1 siblings (`data-model.md`, `contracts/`, `research.md`) when present, `development` reads `tasks.md`, and `feature_architect` (Phase 0 decomposition, when it runs) reads `spec.md`+`plan.md`+`tasks.md` together since it needs the full picture before any per-feature phase exists. Detection is always-on and read-only (dashboard picker, CLI error-and-list); *acting* on it — starting a build — stays explicit by default and only becomes automatic on a new per-project opt-in setting that extends Hephaestus's existing design-queue background scanner. Separately, audit `product_requirements.yaml`/`architecture_design.yaml`/`feature_architect.yaml` (both `bugfix` and `autopilot` workflows, plus the standalone `feature_architect` workflow) to adopt Spec Kit's own quality conventions and structural awareness regardless of which input path was used.

## Technical Context

**Language/Version**: Python 3.12 (backend: CLI, orchestrator, phase prompts), TypeScript/React 18 (dashboard UI)

**Primary Dependencies**: FastAPI + SQLAlchemy (existing backend), the existing `src/autopilot/` orchestrator package (`service.py`, `orchestrator/queue.py`, `spec.py`), `src/cli/commands/autopilot.py` (existing CLI entry point), React + TanStack Query (existing dashboard)

**Storage**: SQLite (`hephaestus.db`) via the existing `AutopilotProject` SQLAlchemy model (`src/core/database.py:1141`) — the new automatic-scanning setting is one more boolean column on that table, following the exact precedent already set by its existing `review_mode: Boolean` field

**Testing**: pytest (backend: detection/parsing, queue-scan extension, phase-prompt content), vitest (frontend: settings toggle, feature picker) — matches this repo's existing `TESTING.md` conventions

**Target Platform**: Same as the rest of Hephaestus — the `heph` backend service (macOS/Linux dev machines) and its bundled React dashboard; no new deployment target

**Project Type**: Addition to an existing web-service + CLI hybrid (FastAPI backend, React dashboard, `heph` CLI) — not a new project or a separate service

**Performance Goals**: Spec Kit detection/parsing must not add perceptible latency to `heph autopilot start` (parsing a handful of markdown files, not a hot loop); automatic scanning (User Story 6) must reuse the existing `DESIGN_QUEUE_SCAN_INTERVAL` (60s, `src/autopilot/orchestrator/pipeline.py:100`) rather than add a second polling loop or interval

**Constraints**: Zero behavior change for projects with only a `design.md` and no `specs/` directory (SC-002); new detection/build logic must compose with the existing phase input-injection pattern (`phase_inputs`/`build_input_manifest` in `src/autopilot/spec.py`) rather than bypass it; automatic scanning must reuse the existing design-queue self-heal/already-processed tracking (`scan_design_queue`, `src/autopilot/orchestrator/queue.py:164`) so a Spec Kit feature is never double-built

**Scale/Scope**: One new detection/parsing module, one new DB column + migration, one new CLI flag + one new CLI subcommand (readiness check), one extension to the existing design-queue scanner, one new dashboard settings toggle + one new dashboard picker component, and a prompt-content audit across 4 phase YAML files (2 phases × 2 workflows)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the unfilled placeholder Spec Kit ships by default (`/speckit-constitution` has not been run for this project) — there are no ratified project-specific principles to gate against yet. In its place, this repo's `CLAUDE.md` already functions as the de facto governing constraints and is treated as authoritative here:

- **Minimal touch / surgical changes**: every file this plan proposes touching maps directly to a requirement in spec.md; no drive-by refactors.
- **No fabrication**: every path and pattern cited in this plan (`AutopilotProject.review_mode`, `scan_design_queue`, `DESIGN_QUEUE_SCAN_INTERVAL`, `src/cli/commands/autopilot.py`) was read from the actual codebase during planning, not assumed.
- **Match existing patterns**: the new setting follows `AutopilotProject.review_mode`'s exact shape; the new scan behavior extends `scan_design_queue` rather than replacing it; new phase-prompt wording follows the same "worked example, not the only path" convention already established for language-agnostic coverage instructions in `qa_validation.yaml`.
- **No speculative generality**: the `/heph` Claude Code skill idea raised during clarification is explicitly out of scope (see spec.md) and not designed here.

No violations to justify — Complexity Tracking is empty by design.

**Post-Phase-1 re-check**: Holds. `research.md`'s decisions and `data-model.md`/`contracts/`'s design introduce no new project, no new service, no bypass of the existing `phase_inputs`/`build_input_manifest` pattern, and no new persistence mechanism beyond one column on an existing table (`AutopilotProject`) — every touched surface extends something that already exists in this codebase rather than replacing it.

## Project Structure

### Documentation (this feature)

```text
specs/001-speckit-autopilot-input/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

This feature extends Hephaestus's existing structure — no new top-level directories:

```text
src/
├── autopilot/
│   ├── spec_kit.py                       # NEW — detect/select a Spec Kit feature dir, enumerate what it contains (FR-002a)
│   ├── service.py                        # MODIFY — resolve Spec Kit vs. design.md input at start()
│   ├── spec.py                           # MODIFY — build_input_manifest reports which Spec Kit files are present in the worktree, the same way it already reports requirements.md/architecture.md/etc.
│   └── orchestrator/
│       ├── queue.py                      # MODIFY — scan_design_queue recognizes specs/<NNN>-<name>/ when auto-scan is enabled (extends its existing extra_dirs hook)
│       └── pipeline.py                   # MODIFY — the 3 sites that copy design.md into `<worktree>/.hephaestus/design.md` (lines ~552-561, 1173, 2036) instead copy the whole selected specs/<NNN>-<name>/ directory when the source is a Spec Kit feature (FR-002a) — every file present reaches the worktree, not a per-phase subset
├── cli/commands/
│   └── autopilot.py                      # MODIFY — `--feature <NNN-name>` flag on `start`; NEW `check` subcommand (readiness check, FR-011)
├── core/
│   └── database.py                       # MODIFY — AutopilotProject.spec_kit_auto_scan: Boolean, default False (mirrors review_mode)
├── mcp/autopilot/
│   └── project_routes.py                 # MODIFY — PUT /projects/{project_id} (line 526) is the general project-settings update route; extend it to accept the new field, same as the rest of AutopilotProject's plain settings (review_mode instead got its own dedicated PATCH /projects/{project_id}/review-mode in feature_review_routes.py, for reasons specific to the review-mode feature — the new setting has no such special-case, so the general route is the right fit)
scripts/
└── add_spec_kit_auto_scan_column.py      # NEW — DB migration, matching the existing add_*_column.py script convention
config/workflows/
├── bugfix/{product_requirements,architecture_design}.yaml    # MODIFY — Spec Kit-aware input + quality-convention audit
├── autopilot/{product_requirements,architecture_design}.yaml # MODIFY — same
└── feature_architect/01_feature_architect.yaml               # MODIFY — receive spec.md+plan.md+tasks.md as one combined input (today reads only ./.hephaestus/design.md), and read them as Spec Kit's actual structure rather than undifferentiated prose (FR-017/FR-018)

frontend/src/
├── components/
│   ├── ProjectSettingsModal.tsx          # MODIFY — automatic-scanning toggle
│   └── autopilot/
│       └── SpecKitFeaturePicker.tsx      # NEW — multi-feature picker (FR-006)
└── services/api.ts                       # MODIFY — list-detected-Spec-Kit-features call

tests/
├── test_spec_kit_input.py                          # NEW — detection/selection/manifest-wiring/readiness-check unit tests
├── test_design_queue_spec_kit_scan.py              # NEW — auto-scan extension tests
└── test_product_requirements_speckit_conventions.py # NEW — prompt-content tests, same pattern as test_qa_coverage_gate_is_diff_scoped.py, for FR-008/FR-009/FR-017/FR-018 conventions across product_requirements.yaml/architecture_design.yaml/feature_architect's 01_feature_architect.yaml

frontend/src/components/
└── SpecKitFeaturePicker.test.tsx         # NEW
```

**Structure Decision**: This is an in-place extension of Hephaestus's existing single-repo structure (Python backend + React frontend + CLI, already co-located under `src/`, `frontend/`, `config/workflows/`) — not a new project, service, or repo layout. Every touched path already exists in this codebase except the explicitly-marked `NEW` files, which sit alongside their closest existing sibling (`spec_kit.py` next to `service.py`/`spec.py` in `src/autopilot/`; the new migration script next to the other `add_*_column.py` scripts; the new picker component next to `DesignDetailModal.tsx`/`LaunchWorkflowModal.tsx` in `frontend/src/components/autopilot/`).

## Complexity Tracking

> Fill ONLY if Constitution Check has violations that must be justified

No violations — table intentionally omitted.
