# Feature Specification: Spec Kit-Aware Autopilot Input

**Feature Branch**: `001-speckit-autopilot-input`

**Created**: 2026-08-26

**Status**: Draft

**Input**: User description: "Add spec-kit-aware input to Hephaestus Autopilot. Today `heph autopilot start` requires a raw design.md as its only input. Spec Kit users author specs through a structured process instead: spec.md (prioritized, independently-testable user stories with measurable, technology-agnostic success criteria), plan.md (technical implementation plan), and tasks.md (dependency-ordered task breakdown), living under specs/<NNN>-<feature-name>/ in a Spec Kit-initialized project. We want Autopilot to accept a Spec Kit project as an alternative starting input to a raw design.md ... Separately, audit and update Hephaestus's own phase prompts to be Spec Kit-aware: recognize when spec-kit-formatted input is present and read it directly, and adopt useful quality conventions from Spec Kit's own process even for hand-written design.md input."

## Clarifications

### Session 2026-08-26

- Q: When more than one Spec Kit feature directory exists under `specs/`, how is the target feature selected? → A: Require an explicit `--feature <NNN-name>` argument to `heph autopilot start`; also expose Spec Kit feature awareness in the dashboard UI (not CLI-only).
- Q: When `spec.md` still has unresolved `[NEEDS CLARIFICATION: ...]` markers, does Autopilot refuse to start? → A: No — proceed exactly as an ambiguous hand-written `design.md` would today. Add a separate, voluntary readiness-check command a user can run first if they want; `heph autopilot start` itself never blocks on it.
- Q: When both `design.md` and a Spec Kit `specs/` directory are present, which wins? → A: Neither wins implicitly. An explicit, specific input (a `--design-doc` path or a `--feature <NNN-name>` Spec Kit selector) is always required in that situation — no silent default.

## Awareness Model

Two things are easy to conflate and must stay distinct throughout this feature:

- **Detection ("awareness") is unconditional.** Whenever Hephaestus looks at a project — the dashboard rendering it, the CLI validating a `heph autopilot start` invocation — it recognizes whether a Spec Kit `specs/<NNN>-<name>/` structure exists, with no setting required to turn this on. This is what makes User Story 2's dashboard picker and the CLI's error-with-a-list behavior (FR-006) possible at all times.
- **Acting on it (starting a build) stays opt-in by default.** Recognizing a Spec Kit feature exists never, by itself, starts an Autopilot run. A build only starts when a user explicitly invokes `heph autopilot start` (optionally with `--feature`), **or** the project has explicitly enabled the automatic-scanning setting described in User Story 6 below. Awareness and automation are two separate levers — a project can have full awareness (dashboard shows every detected feature) while automation stays off forever.

## User Scenarios & Testing *(mandatory)*

<!--
  IMPORTANT: User stories should be PRIORITIZED as user journeys ordered by importance.
  Each user story/journey must be INDEPENDENTLY TESTABLE - meaning if you implement just ONE of them,
  you should still have a viable MVP (Minimum Viable Product) that delivers value.
-->

### User Story 1 - Start Autopilot directly from a Spec Kit spec (Priority: P1)

A user who already authored `spec.md` and `plan.md` for a feature via Spec Kit's own `/speckit.specify` and `/speckit.plan` points `heph autopilot start` at that project. Autopilot recognizes the existing Spec Kit feature directory and begins its pipeline using that spec and plan directly, without asking for a separate hand-written `design.md`.

**Why this priority**: This is the entire value proposition of the feature. Without it, nothing else here matters — a user who already has a spec and plan should never be told to go write a third document.

**Independent Test**: Can be fully tested by pointing Autopilot at a project containing only `specs/<NNN>-<name>/{spec.md,plan.md}` (no `design.md` anywhere) and confirming the pipeline starts and the `product_requirements`/`architecture_design` phases produce output traceable to that spec and plan.

**Acceptance Scenarios**:

1. **Given** a project with exactly one Spec Kit feature directory, `specs/003-checkout-flow/{spec.md,plan.md}`, and no `design.md`, **When** `heph autopilot start` is invoked against that project with no explicit design document path, **Then** Autopilot detects the Spec Kit structure, uses `spec.md` as the `product_requirements` phase's input and `plan.md` as the `architecture_design` phase's input, and proceeds through the rest of the pipeline (development, adversarial_review, security_review, qa_validation, git_expert, deploy) unchanged.
2. **Given** a project with only `design.md` and no `specs/` directory, **When** `heph autopilot start` is invoked, **Then** Autopilot behaves exactly as it does today — no regression for existing users.
3. **Given** a project with both a `design.md` and a Spec Kit `specs/` directory, **When** `heph autopilot start` is invoked without specifying which input to use, **Then** Autopilot refuses to guess and errors, requiring the caller to pass either an explicit design document path or an explicit `--feature <NNN-name>` selector.

---

### User Story 2 - Select among multiple Spec Kit features (Priority: P2)

A project has more than one Spec Kit feature directory under `specs/` (e.g. an in-progress feature and a completed one). The user tells Autopilot exactly which one to build — via a CLI flag, or by picking it in the dashboard — rather than Autopilot silently guessing.

**Why this priority**: Directly required for User Story 1 to be safe to ship the moment a project has more than one Spec Kit feature — an ambiguous auto-pick would be a correctness bug, not a convenience gap. Independently testable and deliverable right after Story 1's single-feature case.

**Independent Test**: Can be tested independently by creating a project with two Spec Kit feature directories and confirming both the CLI and the dashboard require an explicit choice and route to the correct one.

**Acceptance Scenarios**:

1. **Given** a project with two Spec Kit feature directories under `specs/`, **When** `heph autopilot start --feature 003-checkout-flow` is invoked, **Then** Autopilot uses that specific feature's `spec.md`/`plan.md`/`tasks.md`, ignoring the other directory.
2. **Given** a project with two Spec Kit feature directories under `specs/` and no `--feature` argument, **When** `heph autopilot start` is invoked, **Then** it errors, listing the available feature directories and instructing the caller to pick one.
3. **Given** a project with one or more Spec Kit feature directories, **When** a user views that project in the Autopilot dashboard, **Then** the dashboard shows the detected Spec Kit feature(s) and, when more than one exists, lets the user select which one to launch — the same choice the CLI's `--feature` flag makes, surfaced in the UI instead of requiring a terminal.

---

### User Story 3 - Carry an existing task breakdown into development (Priority: P3)

A user's Spec Kit feature directory also has a `tasks.md` (produced by `/speckit.tasks`). Autopilot's `development` phase is given that breakdown as supplementary context, so the user's prioritized, dependency-ordered task list isn't discarded in favor of the agent inventing its own from scratch.

**Why this priority**: Real value-add for Spec Kit users who go further than just spec+plan, but the pipeline is fully functional and useful without it — `development` already knows how to decompose work on its own when `tasks.md` is absent.

**Independent Test**: Can be tested independently by running the same project from User Story 1 with a `tasks.md` added, and confirming the `development` phase's task description references it, versus an otherwise-identical run without it.

**Acceptance Scenarios**:

1. **Given** a Spec Kit feature directory with `spec.md`, `plan.md`, and `tasks.md`, **When** the `development` phase is dispatched, **Then** its task description includes `tasks.md`'s content as reference context.
2. **Given** a Spec Kit feature directory with `spec.md` and `plan.md` but no `tasks.md`, **When** the `development` phase is dispatched, **Then** it proceeds exactly as it does today (no error, no missing-file complaint).

---

### User Story 4 - Voluntary readiness check before starting (Priority: P4)

Before committing to a full Autopilot run, a user can optionally check whether a Spec Kit feature is actually ready — e.g. it still has unresolved `[NEEDS CLARIFICATION: ...]` markers, or is missing `plan.md`. Running this check is entirely the user's choice; `heph autopilot start` itself never blocks or requires it.

**Why this priority**: A convenience that saves a user from starting a run against an obviously-incomplete spec, but strictly optional — Autopilot's existing phases (`scope_review`, `product_requirements`) already handle an ambiguous or incomplete input today, exactly as they would for a rough `design.md`. Nothing else depends on this shipping.

**Independent Test**: Can be tested independently by running the readiness check against a Spec Kit feature with known issues (e.g. an unresolved `NEEDS CLARIFICATION` marker) and confirming it reports them, then confirming `heph autopilot start` still runs successfully against that same feature without the check having been run at all.

**Acceptance Scenarios**:

1. **Given** a Spec Kit feature whose `spec.md` has unresolved `[NEEDS CLARIFICATION: ...]` markers, **When** the user voluntarily runs the readiness check against it, **Then** it reports each unresolved marker and any missing expected files (e.g. `plan.md`).
2. **Given** the same feature, **When** the user runs `heph autopilot start` directly without ever running the readiness check, **Then** the pipeline starts anyway — the check is advisory only, never a gate on `start`.

---

### User Story 5 - Every Autopilot agent understands Spec Kit's conventions (Priority: P5)

Regardless of which input path was used to start it, Autopilot's `product_requirements` and `architecture_design` phases write output that follows Spec Kit's own quality conventions: user stories prioritized P1/P2/P3 and independently testable, success criteria that are measurable and technology-agnostic, and ambiguous requirements flagged with a bounded, prioritized `NEEDS CLARIFICATION` marker instead of being silently guessed at or left to block the pipeline indefinitely. Separately — and distinctly — when `feature_architect` actually receives Spec Kit-formatted input (FR-017), it reads it *as* Spec Kit's structure (recognizing story priorities, `FR-NNN` numbering, `plan.md`'s Technical Context sections, `tasks.md`'s dependency breakdown) rather than treating it as undifferentiated prose the way it reads a hand-written `design.md` today.

**Why this priority**: An enhancement to output quality that benefits every Autopilot run, including ones that never touch Spec Kit — valuable, but the pipeline already functions without it, and it depends on nothing from the other stories.

**Independent Test**: Two independent halves. First: running Autopilot against a project with only a hand-written `design.md` (no Spec Kit involved at all) and confirming `requirements.md` now contains prioritized, independently-testable stories and measurable success criteria in the Spec Kit style. Second: running `feature_architect` against genuine Spec Kit input and confirming its decomposition reasoning (e.g. `features.json`'s rationale, or the agent's own commentary) references the source spec's actual story priorities and `plan.md`'s technical breakdown, not a generic re-summary.

**Acceptance Scenarios**:

1. **Given** a hand-written `design.md` with no explicit story priorities, **When** the `product_requirements` phase runs, **Then** the resulting `requirements.md` organizes requirements as prioritized (P1/P2/P3), independently-testable user stories with measurable, technology-agnostic success criteria.
2. **Given** a requirement in the source input that is genuinely ambiguous in a way with no reasonable default, **When** the `product_requirements` phase encounters it, **Then** it is flagged with a `NEEDS CLARIFICATION` marker, bounded to at most 3 such markers total and prioritized scope > security/privacy > user experience > technical detail — never silently guessed away, and never left unbounded.
3. **Given** a Spec Kit feature with `spec.md`, `plan.md`, and `tasks.md` all present, **When** `feature_architect` decomposes it, **Then** its feature boundaries and `depends_on` relationships are traceable to `plan.md`'s technical breakdown and `tasks.md`'s existing dependency ordering, not invented from scratch as if only `spec.md` existed.

---

### User Story 6 - Automatically build features Spec Kit already queued (Priority: P6)

Hephaestus already auto-builds designs dropped into a project's design queue (`.hephaestus/designs/`, with `docs/spec-queue` as a conventional fallback) — a background scan picks up new files there and starts a workflow with no manual invocation. A project can opt in, via a project-level setting, to extend that same automatic behavior to Spec Kit's own canonical `specs/` folder: once enabled, a new or updated `specs/<NNN>-<name>/` feature directory is picked up by the same background scan and built automatically, exactly as a dropped-in design.md already is today — no separate automation system, no new polling loop.

**Why this priority**: The most advanced, most optional capability in this feature — it depends on User Stories 1 and 2 (single- and multi-feature build) already working, and a project must deliberately opt in. Nothing else here depends on it.

**Independent Test**: Can be tested independently by enabling the setting on a project, adding a new `specs/<NNN>-<name>/spec.md` to it, and confirming a build starts within one scan interval with no manual `heph autopilot start` call — then disabling the setting and confirming a newly-added feature is never auto-built, only detectable (per the Awareness Model above).

**Acceptance Scenarios**:

1. **Given** a project with the automatic-scanning setting disabled (the default), **When** a new `specs/<NNN>-<name>/spec.md` is added, **Then** no build starts on its own — it is only detected/visible (Awareness Model), same as before this setting existed.
2. **Given** a project with the automatic-scanning setting enabled, **When** a new `specs/<NNN>-<name>/spec.md` is added, **Then** the existing design-queue background scan (the same one already polling `.hephaestus/designs/` on its existing interval) also picks it up and starts an Autopilot build for it, without manual invocation.
3. **Given** a project with the setting enabled and a `specs/<NNN>-<name>/` feature that already has a build in progress or completed, **When** the next scan runs, **Then** that feature is not re-queued or double-built — same already-processed/self-heal behavior the existing design-queue scan applies to `.hephaestus/designs/` today.
4. **Given** a project with the setting enabled and two new Spec Kit features added between scans, **When** the scan runs, **Then** both are queued for build, respecting the same project concurrency limits (`max_concurrent_projects`) already applied to the regular design queue.

---

### Edge Cases

- What happens when a Spec Kit feature directory has `spec.md` but planning hasn't happened yet (no `plan.md`)? Not yet resolved — candidate follow-up for the readiness check (User Story 4).
- What happens when `spec.md` was hand-edited into a shape that no longer matches Spec Kit's template structure closely enough to parse? Covered by the Assumptions section below (best-effort, not guaranteed).
- What happens when automatic scanning (User Story 6) is enabled but a detected `specs/<NNN>-<name>/` feature has no `plan.md` yet — does the scan build it from `spec.md` alone (same as a manual start would, per User Story 1), or wait for planning to finish? Not yet resolved — candidate follow-up for a later clarification pass, since it depends on how User Story 4's readiness signal ends up being exposed.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST detect a Spec Kit project structure (a `specs/<NNN>-<name>/` directory containing `spec.md`) when `heph autopilot start` is invoked.
- **FR-002a**: When a Spec Kit feature is selected (per FR-002/FR-006/FR-010), system MUST copy that feature's entire directory into the worktree as a whole — not just whichever single file a given phase's prompt happens to name — the same way `design.md` is copied in today. Every file Spec Kit produced for that feature (`spec.md`, `plan.md`, `tasks.md`, `data-model.md`, `contracts/`, `research.md`, `quickstart.md`, `checklists/`, whatever is actually present) MUST be available in the worktree to any phase that wants to read it; which files a given phase's prompt actually instructs it to read is a per-phase decision (FR-002/FR-003/FR-004/FR-017), not a Hephaestus-side filter on what gets copied.
- **FR-002**: When exactly one Spec Kit feature directory is detected and no `design.md` is explicitly provided, system MUST instruct the `product_requirements` phase to read that feature's `spec.md` as its primary input instead of requiring a hand-written `design.md`.
- **FR-003**: When a `plan.md` exists alongside the selected `spec.md`, system MUST instruct the `architecture_design` phase to read it as its primary input, along with `data-model.md`, `contracts/`, and `research.md` when Spec Kit produced them — `plan.md`'s own Phase 1 outputs carry entity and interface-contract detail an architecture phase needs, and FR-002a means they're already sitting in the worktree either way.
- **FR-004**: When a `tasks.md` exists in the selected feature directory, system MUST instruct the `development` phase to read its content as supplementary context.
- **FR-017**: When the selected Spec Kit feature is routed through `feature_architect` (multi-feature decomposition, run before any per-feature pipeline starts), its phase prompt MUST instruct it to read `spec.md`, `plan.md`, **and** `tasks.md` together — a wider scope than `product_requirements`'s own `spec.md`-only instruction (FR-002). FR-002a already guarantees all three are present in the worktree; this requirement is about what `feature_architect`'s own prompt tells it to actually read, not a separate data-transfer mechanism. Decomposition quality depends on seeing the full picture (`plan.md`'s technical breakdown and `tasks.md`'s existing dependency-ordered breakdown are direct signal for feature boundaries and `depends_on` relationships) before FR-002–004's per-phase routing applies to each decomposed feature's own subsequent pipeline.
- **FR-018**: The `feature_architect` phase prompt MUST be updated to understand Spec Kit's own conventions (prioritized P1/P2/P3 user stories, `FR-NNN` requirement numbering, `plan.md`'s Technical Context structure, `tasks.md`'s dependency-ordered breakdown) when its input is Spec Kit-formatted, rather than reading it as undifferentiated prose the way it reads a hand-written `design.md` today.
- **FR-005**: System MUST NOT change behavior for projects that provide a raw `design.md` and have no Spec Kit `specs/` directory — that path continues to work exactly as it does today.
- **FR-006**: When more than one feature directory exists under `specs/`, system MUST require an explicit `--feature <NNN-name>` argument to `heph autopilot start`; if omitted in that situation, system MUST error and list the available feature directories rather than guessing. This selection MUST also be exposed in the Autopilot dashboard UI as a feature picker, not only as a CLI flag.
- **FR-007**: When `spec.md` contains unresolved `[NEEDS CLARIFICATION: ...]` markers at the time an Autopilot run is started, system MUST proceed with the run — the same way an ambiguous hand-written `design.md` is handled today — and MUST NOT block `heph autopilot start` on their presence.
- **FR-008**: The `product_requirements.yaml` and `architecture_design.yaml` phase prompts, for both the `bugfix` and `autopilot` workflows, MUST instruct the agent to author or validate prioritized (P1/P2/P3), independently-testable user stories and measurable, technology-agnostic success criteria — Spec Kit's own quality bar — regardless of whether the source input was Spec Kit-formatted or a hand-written `design.md`.
- **FR-009**: Phase prompts MUST support a bounded `NEEDS CLARIFICATION` convention (maximum 3 markers per phase, prioritized scope > security/privacy > user experience > technical detail) for genuinely ambiguous requirements, whether they originate from a raw `design.md` or Spec Kit input.
- **FR-010**: When both a `design.md` and a Spec Kit `specs/` directory are present in the same project, system MUST require an explicit, specific selection (an explicit design document path, or an explicit `--feature <NNN-name>`) — it MUST NOT silently default to either input.
- **FR-011**: System MUST provide a separate, voluntary readiness-check capability that a user can run against a Spec Kit feature to surface unresolved `[NEEDS CLARIFICATION: ...]` markers and missing expected files (e.g. absent `plan.md`). Running it MUST be entirely optional and MUST have no effect on whether `heph autopilot start` itself will run.
- **FR-012**: Detection of a Spec Kit `specs/` structure (FR-001) MUST be unconditional — it MUST NOT depend on whether the automatic-scanning setting (FR-013) is enabled. A project with automatic scanning disabled MUST still show detected features wherever awareness is already surfaced (dashboard picker, CLI error listing) per the Awareness Model.
- **FR-013**: System MUST support a project-level setting, defaulting to disabled, that opts a project into automatic building of Spec Kit features. This is a per-project configuration value alongside the project's other existing settings, not a global default.
- **FR-014**: When the automatic-scanning setting is enabled for a project, system MUST extend its existing design-queue background scan (the one already polling that project's `.hephaestus/designs/` directory, with `docs/spec-queue` as a conventional fallback, on the existing scan interval) to also recognize new or updated `specs/<NNN>-<name>/` Spec Kit feature directories as queued designs — one new automation surface on the existing scan loop, not a second parallel poller.
- **FR-015**: A Spec Kit feature picked up by automatic scanning MUST NOT be re-queued or built again once it already has a build in progress or completed — the same already-processed/self-heal tracking the existing design-queue scan already applies to `.hephaestus/designs/` entries.
- **FR-016**: When the automatic-scanning setting is disabled (the default), system MUST take no automatic action on any Spec Kit feature regardless of how long it has existed under `specs/` — starting a build always requires explicit user action (`heph autopilot start`, per User Stories 1–2).

### Key Entities

- **Spec Kit Feature Directory**: A `specs/<NNN>-<name>/` directory produced by Spec Kit, containing `spec.md` (required to be recognized), and optionally `plan.md`, `tasks.md`, `data-model.md`, `contracts/`, `research.md`, `quickstart.md`, and `checklists/`. Copied into the worktree as a whole (FR-002a) — no file within it is held back from any phase.
- **Spec Document (`spec.md`)**: Prioritized user stories, functional requirements, and success criteria; the `product_requirements` phase's primary instructed input.
- **Plan Document (`plan.md`)** and its Phase 1 siblings (`data-model.md`, `contracts/`, `research.md`): the technical implementation plan and its supporting design artifacts; the `architecture_design` phase's primary instructed input.
- **Task Document (`tasks.md`)**: A dependency-ordered task breakdown; supplementary instructed context for the `development` phase when present.
- **Design Queue**: Hephaestus's existing background scan of a project's `.hephaestus/designs/` directory (with `docs/spec-queue` as a conventional fallback) that already auto-starts builds for dropped-in design documents; User Story 6 extends what this scan recognizes rather than introducing a second mechanism.
- **Automatic-Scanning Setting**: A per-project, opt-in, default-disabled configuration value that, when enabled, makes the Design Queue also recognize Spec Kit `specs/<NNN>-<name>/` feature directories.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A user with an existing Spec Kit project (`spec.md` and `plan.md` already written, exactly one feature directory) can start Autopilot on it without authoring any additional design document and without passing any extra flags.
- **SC-002**: A project using only a hand-written `design.md` (no Spec Kit involvement) completes the full Autopilot pipeline with the same phase sequence and gate behavior as before this feature shipped — zero regression, verified by the existing bugfix/autopilot workflow test suites passing unchanged.
- **SC-003**: `product_requirements` and `architecture_design` phase output, on a run started from Spec Kit input, is traceable back to every user story and technical decision stated in the source `spec.md`/`plan.md` — including entities and interface contracts from `data-model.md`/`contracts/` when present, not just `plan.md`'s own prose — no silent drops.
- **SC-004**: Both workflows' `product_requirements.yaml` and `architecture_design.yaml` prompts apply the same `NEEDS CLARIFICATION` bound (max 3, same priority order) and story-priority convention, verified by a direct comparison of the phase YAML files.
- **SC-005**: A project with two or more Spec Kit feature directories never has Autopilot silently pick one — every ambiguous case either errors (CLI) or presents a picker (dashboard).
- **SC-006**: A user can determine whether a Spec Kit feature is ready for Autopilot (no unresolved clarifications, required files present) without starting a run, and choosing not to run that check never prevents `heph autopilot start` from working.
- **SC-007**: On a project with automatic scanning enabled, a new Spec Kit feature directory results in a build starting within one existing design-queue scan interval, with no action beyond having enabled the setting once.
- **SC-008**: On a project with automatic scanning left at its default (disabled), no Spec Kit feature ever starts a build without explicit user action, no matter how long it has sat under `specs/` — detection alone (Awareness Model) never triggers a build.
- **SC-009**: `feature_architect`, given a Spec Kit feature with all three files present, decomposes it using signal from all three — not a decomposition indistinguishable from one that only ever saw `spec.md`.

## Assumptions

- The target project has already had `specify init` (or an equivalent Spec Kit-compatible process) run against it — Autopilot itself does not run `specify init` or generate `spec.md`/`plan.md` from scratch.
- `spec.md` and `plan.md` follow Spec Kit's own template structure closely enough to parse their standard sections (User Stories, Functional Requirements, Success Criteria for `spec.md`; Technical Context/Summary for `plan.md`); heavily hand-modified, non-standard files are handled best-effort, not guaranteed.
- `tasks.md`, when present, is advisory context for the `development` phase, not a literal replacement for Hephaestus's own task creation within phases.
- The dashboard feature picker (User Story 2) surfaces the same feature list the CLI's `--feature` flag would accept; it does not introduce a separate selection mechanism.

## Out of Scope for This Feature

- **A `/heph` Claude Code skill set mirroring the `heph` CLI** (analogous to Spec Kit's own `/speckit-*` skills), raised during clarification as a idea worth pursuing given how naturally it follows from adopting Spec Kit's own pattern. Genuinely valuable, but it is a separate, standalone capability (making Hephaestus itself agent-skill-driven) rather than part of "Autopilot accepting Spec Kit input" — recommended as its own follow-on `/speckit-specify` feature rather than folded in here.
