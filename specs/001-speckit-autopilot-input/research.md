# Phase 0 Research: Spec Kit-Aware Autopilot Input

No unresolved `NEEDS CLARIFICATION` markers remain in the Technical Context — all were settled during `/speckit-specify` (see spec.md's Clarifications log) or by reading the actual codebase during planning. This document records the resulting technical decisions and why each was made, so `/speckit-tasks` has a concrete design to break down rather than open questions to re-litigate.

## Decision: Copy the whole Spec Kit feature folder into the worktree — no per-phase typed content injection

**Decision**: When a Spec Kit feature is selected, its entire `specs/<NNN>-<name>/` directory (`spec.md`, `plan.md`, `tasks.md`, `data-model.md`, `contracts/`, `research.md`, `quickstart.md`, `checklists/` — whatever is actually present) is copied into the worktree once, at the same point `design.md` is copied in today (the 3 sites in `src/autopilot/orchestrator/pipeline.py`). Each phase's own prompt then names which files within it are relevant to that phase and reads them directly, the same way phases already read `requirements.md`/`architecture.md`/`adversarial.md` etc. via the existing `phase_inputs`/`build_input_manifest` "here's what's present, read what you need" pattern — rather than Hephaestus extracting and injecting one typed content string per phase.

**Rationale**: This single mechanism replaces two things that would otherwise have needed separate special-casing: `feature_architect` needing the *full* bundle up front (FR-017/FR-018 below), and `architecture_design` needing more than just `plan.md`'s raw text — Spec Kit's own Phase 1 outputs (`data-model.md`, `contracts/`) sit right alongside `plan.md` and carry exactly the technical detail an architecture phase wants (entities, interface contracts), and a design that only forwarded `plan.md`'s content would have silently dropped them. Copying the whole folder once, and trusting each phase's prompt to read what it needs (exactly how every other Hephaestus phase input already works), avoids Hephaestus's orchestrator needing to know, in code, exactly which Spec Kit file goes to which phase — that mapping lives in the phase prompts themselves, where it's easy to see and change.

**Alternatives considered**: Separate typed fields (`spec_content`, `plan_content`, `tasks_content`) extracted and injected per phase, each phase getting only its "own" file — this was the original design and is what surfaced the gap above: `architecture_design` would never have seen `data-model.md`/`contracts/` under that model, and every time Spec Kit adds a new artifact type Hephaestus would need a code change to notice it. The whole-folder copy needs no such change — a phase's prompt just needs a line telling it to look.

## Decision: Parse spec.md/plan.md by structural section, not a strict schema

**Decision**: `spec_kit.py` extracts content by locating the known section headings Spec Kit's own template always emits (`## User Scenarios & Testing`, `## Requirements`, `## Success Criteria` in `spec.md`; `## Technical Context`, `## Summary` in `plan.md`) and passing each section's raw markdown through to the corresponding Autopilot phase input, rather than attempting to fully parse Spec Kit's grammar (user story priority tags, `FR-NNN` numbering, etc.) into a typed model.

**Rationale**: Autopilot's own `product_requirements`/`architecture_design` phases are themselves LLM agents that already read and reason over unstructured markdown (that's literally what `design.md` is today) — handing them Spec Kit's sections as well-labeled markdown is sufficient and consistent with how every other phase input already works (`build_input_manifest` in `src/autopilot/spec.py` already injects raw file content, not parsed structures). A strict schema parser would be brittle against the exact edge case spec.md's own Assumptions section already calls out: hand-edited files that drift from the template.

**Alternatives considered**: A full typed parser (reject anything that doesn't match Spec Kit's template exactly) — rejected as too brittle and unnecessary extra engineering for content that's ultimately fed to an LLM either way, not machine-processed.

## Decision: Multi-feature selection is CLI-flag + dashboard-picker, no auto-guessing

**Decision**: `heph autopilot start --feature <NNN-name>` (new flag on the existing `src/cli/commands/autopilot.py`); when omitted and more than one `specs/<NNN>-<name>/` directory exists, the command errors and lists them. The dashboard picker (`SpecKitFeaturePicker.tsx`) surfaces the same list via the existing project-detail API surface.

**Rationale**: Directly resolves the FR-006/FR-010 clarification: never silently guess which feature to build. Reuses Spec Kit's own convention of numbered feature directories (`<NNN>-<name>`) as the selector value, so a user copies a name they already recognize from `specs/`.

**Alternatives considered**: Auto-select the most-recently-modified feature directory — rejected explicitly during clarification (Q1) as too easy to get silently wrong; auto-select via `.specify/feature.json`'s "active feature" pointer — rejected because that file is Spec Kit's own per-checkout convenience state (see `.specify/.gitignore`), not something Autopilot should take a hard dependency on for a decision this consequential.

## Decision: Automatic scanning extends the existing design-queue scanner, not a new poller

**Decision**: The new `AutopilotProject.spec_kit_auto_scan` setting (boolean, default `False`, same shape as the existing `review_mode` column) gates whether `scan_design_queue` (`src/autopilot/orchestrator/queue.py:164`) also treats `specs/<NNN>-<name>/` directories as queue entries, reusing its existing `extra_dirs` parameter and existing already-processed/self-heal tracking. No second background loop, no new scan interval.

**Rationale**: `scan_design_queue` already runs on a fixed interval (`DESIGN_QUEUE_SCAN_INTERVAL = 60`, `src/autopilot/orchestrator/pipeline.py:100`), already scans more than one directory (it already falls back to `docs/spec-queue` as a sibling of the primary queue dir), and already has hardened self-heal logic for "marked processed but nothing actually built" states. Building a parallel scanner would duplicate all of that and risk the two mechanisms disagreeing about what's already been built.

**Alternatives considered**: A dedicated Spec-Kit-only background task with its own interval — rejected as unnecessary duplication of an already-working, already-tested mechanism, and a second source of truth for "is this thing already queued."

## Decision: The per-project setting lives on `AutopilotProject`, exposed via the general project-update route

**Decision**: `spec_kit_auto_scan: Boolean` column on `AutopilotProject` (`src/core/database.py:1141`), added via a migration script matching the existing `add_*_column.py` convention (e.g. `add_phase_cli_columns.py`), read/written through `PUT /projects/{project_id}` (`src/mcp/autopilot/project_routes.py:526`).

**Rationale**: `review_mode` is the exact precedent for "a per-project boolean behavioral toggle" on this same model, and most plain settings already go through the general project-update route — `review_mode` getting its own dedicated `PATCH /projects/{project_id}/review-mode` in `feature_review_routes.py` is specific to that feature's own needs (it has side effects tied to feature-review state), not a pattern this setting needs to copy.

**Alternatives considered**: A global `hephaestus_config.yaml` setting instead of per-project — rejected; the spec (FR-013) explicitly calls for a project-level setting, not a global default, since whether a given project wants automatic building is a per-project decision.

## Decision: The voluntary readiness check (FR-011) is a new CLI subcommand, not a phase

**Decision**: `heph autopilot check --feature <NNN-name>` (new subcommand in `src/cli/commands/autopilot.py`) statically inspects a Spec Kit feature directory — scans `spec.md` for unresolved `[NEEDS CLARIFICATION: ...]` markers, checks for the presence of `plan.md` — and reports findings. It does not touch the database, does not create a task or workflow, and has no effect on `heph autopilot start`.

**Rationale**: The spec is explicit that this must be "entirely optional" and have "no effect on whether `heph autopilot start` itself will run" (FR-011) — a CLI-only, side-effect-free static check is the simplest shape that satisfies that, and it mirrors Spec Kit's own `/speckit.checklist`/`/speckit.analyze` pattern of a read-only validation pass distinct from the build itself.

**Alternatives considered**: Folding the check into `scope_review` or `product_requirements` as an early phase step — rejected because those phases already run inside a dispatched agent and worktree; the whole point of this story is a check a user can run before committing to any of that.

## Decision: `feature_architect` reads spec.md+plan.md+tasks.md — the same folder copy every other phase gets, read more completely

**Decision**: `feature_architect` (the Phase 0 multi-feature decomposition workflow, `config/workflows/feature_architect/`, which runs *before* any per-feature pipeline exists) is not a special case of the copy mechanism above — it receives the same whole-folder copy every phase does. What's specific to it is its *prompt*: since it decides feature boundaries before anything else runs, its instructions tell it to read `spec.md`, `plan.md`, and `tasks.md` together (not just `spec.md`, the way `product_requirements`'s own prompt is scoped), because decomposition quality depends on seeing the full picture up front.

**Rationale**: `feature_architect`'s whole job is producing feature boundaries and `depends_on` relationships from one design document read in full (confirmed by reading `01_feature_architect.yaml`: it reads `./.hephaestus/design.md` as a single complete document, then explores the codebase, before writing `features.json`). `plan.md`'s technical breakdown and `tasks.md`'s already-dependency-ordered task list are exactly the signal most useful for deciding feature boundaries and dependencies — reading only `spec.md` here (mirroring `product_requirements`'s narrower scope) would throw that away. This was raised directly during planning review, not inferred.

**Alternatives considered**: Only ever hand `feature_architect` `spec.md`'s content, matching `product_requirements`'s narrower per-phase scope, and let it request `plan.md`/`tasks.md` itself if it wants more context — rejected as strictly worse than just telling it to read them up front (the same "pull vs. push" tradeoff already resolved in `src/agents/prompt_builder.py` for open bug tickets: a pull-based instruction is compliance-dependent). Note this alternative was never about *access* — the whole folder is always present in the worktree either way — only about whether the prompt tells the agent to read the extra files.

## Decision: Prompt-quality audit is a direct content edit to the 4 existing phase YAMLs, not a new mechanism

**Decision**: `product_requirements.yaml` and `architecture_design.yaml`, for both the `bugfix` and `autopilot` workflows, get their `additional_notes` edited in place to instruct prioritized (P1/P2/P3) independently-testable stories, measurable/technology-agnostic success criteria, and the bounded `NEEDS CLARIFICATION` convention (max 3, scope > security > UX > technical) — the same in-place-edit approach already used this session to generalize `qa_validation.yaml`'s coverage instructions away from a Python-only assumption.

**Rationale**: These are prompt templates read fresh at dispatch time (not baked into already-created `Phase` DB rows for in-flight workflows, confirmed earlier this session) — an in-place edit is the established, low-risk way to change agent-facing instructions in this codebase, and keeps this change consistent with how the rest of the phase YAML content is already written and tested (`tests/test_qa_coverage_gate_is_diff_scoped.py`'s existing pattern of asserting on prompt content is the direct precedent for testing this).

**Alternatives considered**: A new shared prompt-fragment file included by reference — rejected as more machinery than four files need, and inconsistent with how the rest of this codebase's phase YAMLs are already self-contained rather than composed from fragments.
