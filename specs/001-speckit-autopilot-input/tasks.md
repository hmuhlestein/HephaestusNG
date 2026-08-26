---

description: "Task list for Spec Kit-Aware Autopilot Input"
---

# Tasks: Spec Kit-Aware Autopilot Input

**Input**: Design documents from `/specs/001-speckit-autopilot-input/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md — all present

**Tests**: Included, even though not explicitly requested in spec.md — this repo's own established convention (every feature touched this session, and `CLAUDE.md`'s goal-driven-execution rule) writes a test alongside every behavior change, so it's treated as a standing implicit request rather than skipped by the letter of the "optional" rule.

**Organization**: Tasks are grouped by user story (spec.md's P1–P6) so each can be implemented and validated independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1–US6)
- File paths are exact, per plan.md's Project Structure

## Phase 1: Setup

- [ ] T001 [P] Create `src/autopilot/spec_kit.py` with function signatures for detection, selection, presence-enumeration, and readiness-check computation (bodies raise `NotImplementedError`; filled in by Phase 2)
- [ ] T002 [P] Create `tests/test_spec_kit_input.py` with imports and fixtures for a temp project directory containing `specs/<NNN>-<name>/`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Detection, presence-enumeration, and the worktree copy mechanism every user story depends on.

**⚠️ CRITICAL**: No user story's Independent Test can pass end-to-end until this phase is complete — even a story whose own tasks don't touch `pipeline.py` still needs the copy mechanism working for its worktree to contain anything to read.

- [ ] T003 Implement Spec Kit feature-directory detection in `src/autopilot/spec_kit.py` — scan `specs/<NNN>-<name>/`, require `spec.md` to recognize a directory (FR-001) (depends on T001)
- [ ] T004 Implement `present_files` enumeration (which of `spec.md`/`plan.md`/`tasks.md`/`data-model.md`/`contracts/`/`research.md`/`quickstart.md`/`checklists/` actually exist) and `[NEEDS CLARIFICATION: ...]` marker extraction from `spec.md`, in `src/autopilot/spec_kit.py` (depends on T003 — same file)
- [ ] T005 Extend the 3 design-doc-copy sites in `src/autopilot/orchestrator/pipeline.py` (~lines 552-561, 1173, 2036) to copy the selected Spec Kit feature's **entire directory** into the worktree, instead of a single file, when the source is a Spec Kit feature (FR-002a) (depends on T003)
- [ ] T006 Foundational tests in `tests/test_spec_kit_input.py`: detection (zero/one/many features; a directory missing `spec.md` is not recognized), `present_files` enumeration, and that the worktree copy includes every present file, not just `spec.md`; detection/enumeration take no `spec_kit_auto_scan` parameter and are exercised without touching that setting at all, confirming awareness is unconditional (FR-012) (depends on T002, T003, T004, T005)

**Checkpoint**: Detection, presence-enumeration, and worktree copy all work in isolation — user story implementation can now begin.

---

## Phase 3: User Story 1 - Start Autopilot directly from a Spec Kit spec (Priority: P1) 🎯 MVP

**Goal**: `heph autopilot start` uses `spec.md`/`plan.md` (and Spec Kit's Phase 1 siblings) directly when exactly one Spec Kit feature exists and no `design.md` conflict.

**Independent Test**: Point Autopilot at a project with only `specs/<NNN>-<name>/{spec.md,plan.md}` and confirm the pipeline starts using them (quickstart.md Scenario 1); confirm a `design.md`-only project is unaffected (Scenario 2).

### Tests for User Story 1

- [ ] T007 [P] [US1] Integration test: single Spec Kit feature auto-selected, `product_requirements` instructed to read `spec.md` and `architecture_design` instructed to read `plan.md` (plus `data-model.md`/`contracts/`/`research.md` when present) from the worktree; includes the case where `spec.md` still has unresolved `[NEEDS CLARIFICATION: ...]` markers — the run proceeds anyway, unblocked (FR-007) — in `tests/test_spec_kit_input.py`
- [ ] T008 [P] [US1] Regression test: a project with only `design.md` (no `specs/`) behaves identically to before this feature, in `tests/test_spec_kit_input.py`

### Implementation for User Story 1

- [ ] T009 [US1] Wire `spec_kit.py`'s single-feature selection into `src/autopilot/service.py`'s input resolution at `start()` (depends on T006)
- [ ] T010 [US1] Update `phase_inputs`/`build_input_manifest` in `src/autopilot/spec.py` so `product_requirements`'s manifest names `spec.md` and `architecture_design`'s manifest names `plan.md`, `data-model.md`, `contracts/`, `research.md` (whichever are present) — the same "here's what's present, read it" shape those functions already produce for `requirements.md`/`architecture.md`/etc. (depends on T009)
- [ ] T011 [US1] Error path in `src/autopilot/service.py`: both `design.md` and a `specs/` directory present with no explicit `--feature`/`--design-doc` → refuse and require an explicit choice (FR-010) (depends on T009)

**Checkpoint**: User Story 1 fully functional and independently testable.

---

## Phase 4: User Story 2 - Select among multiple Spec Kit features (Priority: P2)

**Goal**: An unambiguous, explicit way to pick a feature — CLI flag and dashboard picker — when more than one exists.

**Independent Test**: quickstart.md Scenario 3 (CLI errors and lists candidates without `--feature`; `--feature` selects correctly); dashboard picker renders and selects the same list.

### Tests for User Story 2

- [ ] T012 [P] [US2] CLI test: multiple feature directories, no `--feature` → non-zero exit, error lists all candidates, in `tests/test_spec_kit_input.py`
- [ ] T013 [P] [US2] CLI test: `--feature <NNN-name>` selects that specific feature, in `tests/test_spec_kit_input.py`
- [ ] T014 [P] [US2] Component test for the feature picker in `frontend/src/components/autopilot/SpecKitFeaturePicker.test.tsx`

### Implementation for User Story 2

- [ ] T015 [US2] Add `--feature <NNN-name>` and `--design-doc <path>` flags to `heph autopilot start` in `src/cli/commands/autopilot.py`, implementing the resolution order in contracts/cli.md (depends on T006, T011)
- [ ] T016 [P] [US2] New `GET /projects/{project_id}/spec-kit-features` endpoint returning `List[SpecKitFeatureItem]` in `src/mcp/autopilot/design_file_routes.py` (depends on T006)
- [ ] T017 [P] [US2] New `SpecKitFeaturePicker.tsx` component in `frontend/src/components/autopilot/` consuming that endpoint (depends on T016)
- [ ] T018 [US2] Wire the picker into the Autopilot launch flow in the dashboard, adding the API call to `frontend/src/services/api.ts` (depends on T017)

**Checkpoint**: User Stories 1 and 2 both work independently.

---

## Phase 5: User Story 3 - Carry an existing task breakdown into development (Priority: P3)

**Goal**: `tasks.md`, when present, reaches the `development` phase as context.

**Independent Test**: Same project as US1 with `tasks.md` added — `development`'s task description includes it; without it, behavior is unchanged.

### Tests for User Story 3

- [ ] T019 [P] [US3] Test: `tasks.md` content appears in the `development` phase's task description when present, in `tests/test_spec_kit_input.py`
- [ ] T020 [P] [US3] Test: no `tasks.md` → `development` proceeds exactly as before (no error), in `tests/test_spec_kit_input.py`

### Implementation for User Story 3

- [ ] T021 [US3] Extend the `development`-phase manifest entry in `src/autopilot/spec.py` to name `tasks.md` as supplementary context when present (depends on T010)

**Checkpoint**: User Stories 1–3 all work independently.

---

## Phase 6: User Story 4 - Voluntary readiness check before starting (Priority: P4)

**Goal**: A read-only check a user can run before committing to a build; never gates `start`.

**Independent Test**: quickstart.md Scenario 4 — check reports issues, `start` still runs regardless of whether the check was ever run.

### Tests for User Story 4

- [ ] T022 [P] [US4] Test: `heph autopilot check` reports unresolved `[NEEDS CLARIFICATION: ...]` markers and missing `plan.md`, in `tests/test_spec_kit_input.py`
- [ ] T023 [P] [US4] Test: running (or not running) the check has zero effect on a subsequent `heph autopilot start`, in `tests/test_spec_kit_input.py`

### Implementation for User Story 4

- [ ] T024 [US4] Implement `ReadinessCheckResult` computation in `src/autopilot/spec_kit.py` (depends on T006)
- [ ] T025 [US4] New `heph autopilot check --feature <NNN-name>` subcommand in `src/cli/commands/autopilot.py`, read-only, per contracts/cli.md (depends on T024)

**Checkpoint**: User Stories 1–4 all work independently.

---

## Phase 7: User Story 5 - Every Autopilot agent understands Spec Kit's conventions (Priority: P5)

**Goal**: `product_requirements`/`architecture_design`/`feature_architect` prompts adopt Spec Kit's conventions and read its full structure, regardless of input source.

**Independent Test**: quickstart.md Scenario 6 — a hand-written-`design.md` run now produces prioritized, independently-testable stories and measurable success criteria; all workflows' prompts agree on the same bounded `NEEDS CLARIFICATION` convention; `feature_architect`'s decomposition is traceable to `plan.md`/`tasks.md`, not just `spec.md`.

### Tests for User Story 5

- [ ] T026 [P] [US5] Prompt-content test asserting P1/P2/P3 story-priority and bounded `NEEDS CLARIFICATION` (max 3, scope > security > UX > technical) conventions in `config/workflows/bugfix/product_requirements.yaml`, in a new `tests/test_product_requirements_speckit_conventions.py` (same pattern as `tests/test_qa_coverage_gate_is_diff_scoped.py`)
- [ ] T027 [P] [US5] Same assertions for `config/workflows/autopilot/product_requirements.yaml`, in `tests/test_product_requirements_speckit_conventions.py`
- [ ] T028 [P] [US5] Same assertions for both workflows' `architecture_design.yaml`, plus an assertion that its prompt names `data-model.md`/`contracts/`/`research.md` as files to read when present (FR-003), in `tests/test_product_requirements_speckit_conventions.py`
- [ ] T029 [P] [US5] Test: `config/workflows/feature_architect/01_feature_architect.yaml`'s prompt content references Spec Kit's story-priority/`FR-NNN`/`plan.md`/`tasks.md` conventions and instructs reading all three together (FR-017/FR-018), in `tests/test_product_requirements_speckit_conventions.py`

### Implementation for User Story 5

- [ ] T030 [P] [US5] Edit `additional_notes` in `config/workflows/bugfix/product_requirements.yaml` to instruct prioritized, independently-testable stories; measurable, technology-agnostic success criteria; and the bounded `NEEDS CLARIFICATION` convention
- [ ] T031 [P] [US5] Same edit in `config/workflows/autopilot/product_requirements.yaml`
- [ ] T032 [P] [US5] Same convention added to `config/workflows/bugfix/architecture_design.yaml`, plus an instruction to read `data-model.md`/`contracts/`/`research.md` when present (FR-003)
- [ ] T033 [P] [US5] Same to `config/workflows/autopilot/architecture_design.yaml`
- [ ] T034 [US5] Edit `config/workflows/feature_architect/01_feature_architect.yaml`'s prompt to read Spec Kit-formatted input — `spec.md`+`plan.md`+`tasks.md` together — as its actual structure instead of undifferentiated prose (FR-017/FR-018) (depends on T004)
- [ ] T035 [US5] Run `validate_single_workflow` (config validator) against all three workflows (`bugfix`, `autopilot`, `feature_architect`) to confirm every edit parses cleanly (depends on T030, T031, T032, T033, T034)

**Checkpoint**: User Stories 1–5 all work independently.

---

## Phase 8: User Story 6 - Automatically build features Spec Kit already queued (Priority: P6)

**Goal**: A project-level, default-off setting extends the existing design-queue scan to `specs/`.

**Independent Test**: quickstart.md Scenario 5 — enabled: a new feature auto-builds within one scan interval; disabled: it never does, regardless of dwell time.

### Tests for User Story 6

- [ ] T036 [P] [US6] Test: `AutopilotProject.spec_kit_auto_scan` defaults to `False`; migration adds the column correctly, in `tests/test_spec_kit_input.py`
- [ ] T037 [P] [US6] Test: with the setting enabled, `scan_design_queue` picks up a new `specs/<NNN>-<name>/` as a `DesignEntry`, in `tests/test_design_queue_spec_kit_scan.py`
- [ ] T038 [P] [US6] Test: a feature with an existing build is not re-queued (self-heal parity with `.hephaestus/designs/` entries), in `tests/test_design_queue_spec_kit_scan.py`
- [ ] T039 [P] [US6] Test: with the setting disabled, a new feature is never auto-built no matter how many scans pass, in `tests/test_design_queue_spec_kit_scan.py`
- [ ] T040 [P] [US6] Component test for the settings toggle in `frontend/src/components/ProjectSettingsModal.test.tsx`

### Implementation for User Story 6

- [ ] T041 [US6] Add `spec_kit_auto_scan: Boolean, default=False` to `AutopilotProject` in `src/core/database.py:1141` (depends on T006)
- [ ] T042 [US6] Migration script `scripts/add_spec_kit_auto_scan_column.py`, matching the existing `add_*_column.py` convention (depends on T041)
- [ ] T043 [US6] Extend `scan_design_queue` in `src/autopilot/orchestrator/queue.py` to pass detected `specs/` feature directories via its existing `extra_dirs` parameter when the owning project's `spec_kit_auto_scan` is `True` (depends on T041, T006)
- [ ] T044 [US6] Add `spec_kit_auto_scan` to `ProjectUpdate`/`ProjectItem` and wire it into `PUT /projects/{project_id}` in `src/mcp/autopilot/project_routes.py` (depends on T041)
- [ ] T045 [US6] Add the automatic-scanning toggle to `frontend/src/components/ProjectSettingsModal.tsx` (depends on T044)

**Checkpoint**: All six user stories independently functional.

---

## Phase 9: Polish & Cross-Cutting Concerns

- [ ] T046 [P] Run all six quickstart.md scenarios manually against a real dev instance
- [ ] T047 [P] Add a "Spec Kit input" note to `website/docs/configuration/reference.md` (the Configuration Reference page) and to the README's Configuration section; also update the README's `## 🤖 Autopilot` section to state Spec Kit input support as a real, shipped capability (`specs/<NNN>-<name>/` accepted alongside `design.md`) — only once T001–T046 actually land, never claimed ahead of the implementation
- [ ] T048 Run the targeted new/touched test files with `pytest` and `npx vitest run` — not the full suite, per this repo's own targeted-testing convention
- [ ] T049 Final `validate_single_workflow` pass on all three workflows after all edits land

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies.
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories, including the worktree-copy mechanism (T005) every story's Independent Test relies on even when the story's own tasks never touch `pipeline.py`.
- **User Stories (Phase 3–8)**: All depend on Foundational. US1 → US3 have a real ordering dependency (US3 extends US1's `build_input_manifest` wiring; US2 extends US1's selection/error-path logic); US4 and US5 are independent of US1–3's implementation; US6 depends on Phase 2 detection but not on US1–5's implementation.
- **Polish (Phase 9)**: Depends on whichever stories were actually implemented.

### User Story Dependencies

- **US1 (P1)**: Foundational only.
- **US2 (P2)**: Foundational + US1 (extends its selection/error-path logic rather than duplicating it).
- **US3 (P3)**: Foundational + US1 (extends its `build_input_manifest` wiring).
- **US4 (P4)**: Foundational only — independent of US1–3.
- **US5 (P5)**: Foundational only (needs `present_files`/detection for its tests) — independent of every other story, could ship first if desired.
- **US6 (P6)**: Foundational only — independent of US1–5's implementation, though it's only useful once US1 exists to build the auto-queued features.

### Parallel Opportunities

- T001/T002 (Setup) in parallel.
- Within each story's "Tests for User Story N" block, all `[P]` tasks run in parallel (different test functions/files).
- T030–T034 (the five prompt-YAML edits, US5) run fully in parallel — five different files.
- US4 and US5 can be built in parallel with US1–3 by a second contributor, since neither depends on the other's implementation (only on Phase 2).

---

## Parallel Example: User Story 5

```bash
# Five independent files:
Task: "Edit additional_notes in config/workflows/bugfix/product_requirements.yaml"
Task: "Edit additional_notes in config/workflows/autopilot/product_requirements.yaml"
Task: "Edit additional_notes + Phase-1-siblings instruction in config/workflows/bugfix/architecture_design.yaml"
Task: "Edit additional_notes + Phase-1-siblings instruction in config/workflows/autopilot/architecture_design.yaml"
Task: "Edit config/workflows/feature_architect/01_feature_architect.yaml for Spec Kit convention awareness"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 (Setup) → Phase 2 (Foundational) → Phase 3 (US1).
2. **STOP and VALIDATE**: quickstart.md Scenarios 1 and 2.
3. That alone delivers the feature's entire stated value proposition — a Spec Kit user can start Autopilot without writing a second document.

### Incremental Delivery

1. Setup + Foundational → detection, presence-enumeration, and worktree copy all work standalone.
2. US1 → MVP: single-feature auto-start works.
3. US2 → safe on multi-feature projects.
4. US3 → richer `development`-phase context.
5. US4 → optional pre-flight safety net.
6. US5 → quality improvement, ships independently at any point.
7. US6 → full automation, last because it's the most advanced and least essential capability.

## Notes

- `[P]` tasks touch different files with no unmet dependencies.
- `[Story]` labels trace every task back to spec.md's user stories.
- Tests were included as a deliberate judgment call (see header) rather than an explicit spec.md request — flagged here per this repo's own "surface assumptions out loud" convention.
- Revised twice after initial generation: once to fold in `feature_architect` awareness, and again when the underlying mechanism changed from per-phase typed-content injection to a single whole-folder worktree copy (spec.md's FR-002a) — the worktree-copy task moved from User Story 5 into Foundational (Phase 2), since every story's Independent Test depends on it, not just the prompt-quality story. This file reflects the current, correct numbering (T001–T049).
- Commit after each task or logical group; stop at any checkpoint to validate a story independently before continuing.
