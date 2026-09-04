# Autopilot Pipeline

A fully automated multi-agent workflow engine that takes design documents,
decomposes them into features, and drives each feature through a 14-phase
pipeline to produce validated, committed, shipped software.

## Overview

```
DB queue                   Phase 0 (once)        per-feature pipeline      project/
  ├── auth-system.md  ──► [Feature Architect] ──► auth    (parallel) ──►    ├── src/
  │   (anywhere)            │                 ──► session (parallel) ──►    ├── tests/
  ├── dashboard.md          ▼                ──► admin   (sequential) ──►   └── .hephaestus/
  └── api-v2.md        features.json              (phases 1–14 each)             ├── designs/
                                                                                 │   └── 20260612_auth_system_fb36c8e3/
                                                                                 │       ├── design_report.html
                                                                                 │       ├── design_metrics.json
                                                                                 │       ├── features.json
                                                                                 │       └── features/<slug>/scope.md
                                                                                 └── features/          ← sibling, not nested
                                                                                     └── 20260612_auth_system/
                                                                                         ├── feature_report.html
                                                                                         ├── docs/       ← the phase reports
                                                                                         └── tmux/
```

---

## Feature Model

A **Design** is a single `.md` file describing a product change. It may be
simple (one self-contained capability) or complex (many interdependent
capabilities). The pipeline handles both by decomposing a design into
**Features** before any code is written.

### What is a Feature?

A Feature is a vertically-scoped, independently shippable slice of a design.
Each Feature:

- Has a clear name and scope (e.g. "JWT authentication", "user dashboard", "admin API")
- Runs the full 14-phase pipeline in its own git worktree
- Produces its own set of phase artifacts (requirements, architecture, code, reports)
- Has an independent pass/fail status and iteration count
- Is committed and merged to main independently

A simple design (e.g. "add a calculator") produces a single Feature. A complex
design (e.g. "add auth, dashboard, and admin panel") produces multiple Features.

### Execution Order

The **Feature Architect** (Phase 0) determines how features execute:

| `execution` value | Behavior |
|-------------------|----------|
| `parallel`        | Feature runs concurrently with other parallel-marked features |
| `sequential`      | Feature waits for all preceding features (parallel or sequential) to complete before starting |

The architect sets `execution` per feature based on dependency analysis. A
feature that depends on shared infrastructure written by another feature must
be `sequential`. Independent features should be `parallel` to minimize wall-clock
time.

### Feature Lifecycle

```
Design: pending → decomposing → active → completed
                                         failed
                                         skipped

Feature: pending → active → completed
                             failed
                             skipped
```

**Design states:**
- `pending`: design picked up from queue, Feature Architect not yet run
- `decomposing`: Feature Architect is running, `features.json` not yet written
- `active`: `features.json` written; per-feature pipelines running
- `paused`: pipeline paused while this design still has active/paused workflows
- `completed`: every feature reached completed or skipped
- `failed`: one or more features failed beyond max iterations
- `skipped`: design removed from queue before completion

**Feature states:**
- `pending`: created from `features.json`; waiting for dependencies or parallel slot
- `active`: pipeline phases running
- `paused`: this feature's workflow was paused; the pause cascades to the
  feature so the UI does not keep showing it as active with nothing running
- `completed`: the workflow ran to the end and the feature was merged to main
- `failed`: exceeded max iterations or hard error
- `skipped`: a dependency feature failed; this feature cannot run

### features.json Schema

The Feature Architect writes `features.json` to `.hephaestus/features.json`
in the design's worktree. The orchestrator reads this file to create Feature
records and spawn per-feature workflows.

```json
{
  "design_name": "auth-system",
  "features": [
    {
      "id": "auth",
      "name": "JWT Authentication",
      "scope": "JWT token issuance, validation, refresh. Endpoints: POST /auth/login, POST /auth/refresh, POST /auth/logout.",
      "files": ["src/auth/", "tests/test_auth.py"],
      "depends_on": [],
      "execution": "parallel"
    },
    {
      "id": "session",
      "name": "Session Management",
      "scope": "Redis-backed session store. Middleware to attach session to request context.",
      "files": ["src/session/", "tests/test_session.py"],
      "depends_on": [],
      "execution": "parallel"
    },
    {
      "id": "admin",
      "name": "Admin Panel",
      "scope": "CRUD endpoints for user management. Requires auth and session features.",
      "files": ["src/admin/", "tests/test_admin.py"],
      "depends_on": ["auth", "session"],
      "execution": "sequential"
    }
  ]
}
```

Field rules:
- `id`: short slug, unique within the design, used for folder naming
- `name`: human-readable feature name
- `scope`: paragraph describing exactly what this feature covers and what it excludes
- `files`: list of source paths this feature owns (non-overlapping across features)
- `depends_on`: list of feature `id`s that must complete before this one starts
- `execution`: `"parallel"` or `"sequential"` — set by the architect, never overridden by the orchestrator

---

## Quick Start

```bash
# 1. Register a design document (can live anywhere)
heph autopilot add ~/my-designs/auth-system.md --project-path ~/my-project

# 2. Start the pipeline
heph autopilot start --project-path ~/my-project

# 3. Add more designs anytime — pipeline picks them up from the queue
heph autopilot add ~/my-designs/dashboard.md --project-path ~/my-project
```

Design documents can live anywhere on the filesystem. `heph autopilot add`
creates an `AutopilotDesign` DB record pointing to the file path and adds it
to the processing queue. The pipeline processes queued designs in
priority/creation order and produces an HTML design report aggregating all
feature outcomes in the `designs/` directory.

---

## Projects & Concurrent Execution

Every `--project-path` the pipeline runs against is backed by an
`AutopilotProject` row, keyed by its resolved absolute `base_dir`. `heph
autopilot start --project-path ~/my-project` auto-creates this row on first
use and activates it (`_get_or_create_project_id`) — most single-project
users never need to touch project activation directly. Multi-project setups
(running autopilot against more than one repo from the same backend) need to
understand two things: how many projects can run at once, and what "active"
actually means.

### `is_active` vs. workflow status — two different things named "active"

`AutopilotProject.is_active` and a `Workflow`'s own `status` field answer
different questions, and confusing them is a real source of stuck-pipeline
confusion:

- **`AutopilotProject.is_active`** (boolean) controls whether this project's
  work is picked up **at all**. The phase-advancement sweep and the dispatch
  loop only ever touch phases and workflows belonging to an active project.
- **`Workflow.status`** (e.g. `active`, `completed`, `paused`, `failed`) is
  the state of one specific pipeline run, independent of whether its
  project is currently active.

Deactivating a project does **not** touch the `status` of its in-flight
workflows — a `Workflow` can sit at `status="active"` indefinitely once its
project is deactivated, because nothing is polling it anymore. There is no
error, no `paused_by` reason set, nothing in the UI beyond the project
itself showing as inactive — the workflow just silently stops making
progress. If a feature looks stuck with no obvious error, check whether its
project is still active before looking anywhere else.

### The concurrency cap

`max_concurrent_projects` (`hephaestus_config.yaml`'s `autopilot:` section,
default `2`) caps how many `AutopilotProject` rows can have `is_active=True`
at once. `heph autopilot start` and `POST /projects/{id}/activate` both
enforce it:

- `heph autopilot start` reserves a slot before launching; a genuinely new
  project that would exceed the cap is refused outright. Restarting a
  project that already occupies a slot is always allowed — it isn't
  claiming a new one.
- `POST /projects/{id}/activate` returns `409` naming every currently-active
  project if the cap is already full:
  ```
  Max concurrent projects (2) reached: backend-api, frontend-app.
  Stop one before starting another.
  ```

Activating a project never evicts another active one to make room — you
must explicitly deactivate something first.

### Selecting a project

```bash
heph project list                       # every registered project, active/default flags
heph project create <name> <path>       # register a new project
heph project activate <name-or-id>      # bring it into the active rotation (subject to the cap)
heph project deactivate <name-or-id>    # stop the sweep/dispatch loop from touching it
heph project current                    # list every currently-active project
```

The equivalent API:

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/autopilot/projects` | List every registered project. |
| `POST` | `/api/autopilot/projects/{id}/activate` | Activate; `409` if the cap is full. |
| `POST` | `/api/autopilot/projects/{id}/deactivate` | Deactivate — in-flight workflows are left exactly as they are. |
| `GET` | `/api/autopilot/projects/active` | Every currently-active project (0 to `max_concurrent_projects`), not just one — this used to return a single project via `.first()` before multi-project concurrency existed. |

---

## Pipeline Phases

### Phase 0: Feature Architect (design-scoped, runs once per design)

**Agent:** Feature Architect

Runs **once per design**, before any feature pipelines start. Reads the full
design document and the project context, then decomposes the design into
features with explicit execution ordering.

- Reads `AGENTS.md` for repository guidelines
- Searches `designs/` for previously shipped work that may inform scope boundaries
- Queries vector DB for prior decomposition decisions and dependency patterns
- Reads the full design document (`.hephaestus/spec.md`)

Produces two outputs:

1. `.hephaestus/features.json` — machine-readable decomposition for the orchestrator
2. `.hephaestus/features/<feature-id>/scope.md` **per feature** — a prose document
   written specifically for that feature's pipeline agents. Each `scope.md` contains:
   - What this feature covers (expanded from the `scope` field in `features.json`)
   - What it explicitly excludes (the boundaries with sibling features)
   - Interfaces with other features (APIs, shared data models, events)
   - Constraints and non-functional requirements specific to this feature
   - Which features this one depends on and what it expects from them

The orchestrator reads `features.json`, creates one `Feature` record per entry,
and launches per-feature workflows. Each feature's agents read their `scope.md`
as their primary source of truth — they do not need to read the full design doc.

**Simple designs** (one self-contained capability) should produce a single-entry
`features.json` with `execution: parallel` and a single `scope.md` that is
essentially the full design. This is functionally identical to the old
single-workflow model — no overhead beyond one fast agent run.

**Phase 0 is a blocking gate.** No feature pipeline starts until `features.json`
and all `scope.md` files are written and validated by the orchestrator.

---

### Per-Feature Pipeline (Phases 1–14)

Each Feature runs its own independent instance of the following 14 phases (1–14), in its
own git worktree.

---

### Phase 1: Product Requirements

**Agent:** Product Requirements Analyst

Primary input is this feature's `scope.md` — not the full design document.

- Reads `.hephaestus/features/<feature-id>/scope.md` as the source of truth
- Reads `AGENTS.md` for repository guidelines
- Searches existing `designs/` for previously completed work
- Queries the vector database via `search_memory` for prior decisions
- Greps other design docs for cross-references

Produces: `requirements.md` scoped strictly to this feature —
functional/non-functional requirements, component dependencies, technology
constraints, and integration points with other features as defined in `scope.md`.

### Phase 2: Scope Review

**Agent:** Scope Reviewer

Gate between product requirements and architecture. Verifies that
`requirements.md` is a faithful, complete extraction of this feature's
scope — nothing added, nothing dropped.

- Reads `.hephaestus/features/<feature-id>/scope.md` — the feature's stated scope
- Reads `.hephaestus/spec.md` — the original full design doc, to verify `scope.md`
  correctly represents the original intent for this feature's slice
- Reads `requirements.md` — what Phase 1 produced
- Traces every requirement back to a line in `scope.md` and ultimately to `spec.md`

Produces: `scope.md` — a YAML frontmatter block (OKF format:
`type` first, then verdict PASS or FAIL and supporting fields) followed by
the narrative report. On FAIL, returns to Phase 1 with specific correction
instructions. This is a binary gate — no architecture starts until scope is
clean.

### Phase 3: Architecture & Design

**Agent:** Software Architect

Primary input is this feature's `scope.md` and `requirements.md`.

- Reads `.hephaestus/features/<feature-id>/scope.md` for boundary constraints
- Uses `requirements.md` as the detailed requirements input
- Creates system architecture scoped to this feature's file ownership
- Data models and API contracts within this feature's boundary
- Implementation plan

Produces: `architecture.md` — technical design for this feature only.

### Phase 4: Design Review

**Agent:** Architecture Challenger

An adversarial, pre-development review of `architecture.md` itself — before
any code exists to review instead. Assumes the architecture is wrong and
tries to prove it, while fixing a finding still only costs a rewrite of a
document, not a rewrite of working code:
- Requirements coverage (every REQ/NFR traced to an owning component and
  vice versa)
- Concurrency/data-consistency gaps designed into the proposed model
- Interface and error-propagation gaps in the hook-together map
- Composition and hierarchy: high-level components leaking low-level
  internals, missed polymorphism/strategy-pattern opportunities, complex
  logic that isn't pushed down to the right level
- Over- and under-engineering relative to the feature's actual scope
- Secrets/security handling in the proposed design
- Task breakdown sanity (valid dependency order, verifiable acceptance criteria)

Classifies findings as BLOCKER, WARNING, or NIT. Unlike the later
Architectural Review and Adversarial Code Review (where only a BLOCKER sends
work back), **any** finding here returns to Phase 3 — development hasn't
started yet, so there's no cheaper phase to defer a WARNING to.

Produces: `challenge.md` — a YAML frontmatter block (OKF format,
`blocker_count`/`warning_count`/`nit_count`) followed by the narrative
report. Reports only — does **not** edit `architecture.md` directly.

### Phase 5: Development

**Agent:** Software Developer

Implements components according to this feature's `architecture.md`:
- Follows `AGENTS.md` coding conventions
- Works only within the file paths listed in this feature's `files` entry
- Writes unit and integration tests
- Verifies tests pass
- Does not modify files owned by other features

Produces: Working source code in `<project-path>/`.

### Phase 6: Adversarial Code Review

**Agent:** Adversarial Code Reviewer

Runs immediately after development, before Architectural Review — there is
no design-compliance pass yet for this phase to defer to. Reviews all code
produced by this feature with a critical perspective, reasoning backward
from production failure rather than validating the happy path:
- Correctness (logic errors, edge cases)
- Exception propagation, concurrency/shared-state races, resource leaks
- Silent data corruption, incorrect defaults, cascade/ordering invariants
- Code composition (high-level classes leaking low-level details, missed
  polymorphism, complex logic that isn't pushed down)
- Security (injection, XSS, auth bypass)

Classifies findings as BLOCKER/WARNING/NIT. Reports findings — does **not**
edit production code directly; the developer fixes based on this report.

Produces: `adversarial.md` — a YAML frontmatter block (OKF format,
`blocker_count`/`warning_count`/`nit_count`) followed by the narrative
report.

### Phase 7: Architectural Review

**Agent:** Software Architect (re-invoked)

The architect is re-invoked after development (and the adversarial code
review) completes, with warm context about the design decisions,
trade-offs, and invariants from Phase 3. Reviews the implementation for
architecture compliance, design violations, and over-engineering — a
different lens than Phase 6's failure-mode reasoning, so some overlap in
findings between the two is expected and fine:
- Classifies findings as BLOCKER (architecture violated), FIX (design
  deviation), or DEFER (nice to have)
- Reports findings — does **not** edit production code directly

Produces: `review.md` — a YAML frontmatter block (OKF
format, `blocker_count`/`fix_count`/`defer_count` etc.) followed by the
narrative report. The developer fixes issues based on this report; capped
at a max number of review runs (`workflow.yaml`'s `max_review_runs`) before
the phase reports unresolved findings and moves on.

### Phase 8: Security Review

**Agent:** Security Reviewer

Focused security assessment:
- Authentication/authorization mechanisms
- Input validation across all endpoints
- Data handling and secret management
- Dependency vulnerability audit
- OWASP Top 10 checks

**Fixes** critical and high vulnerabilities directly in the code.

Produces: `security.md` with findings and fixes applied.

### Phase 9: QA Validation

**Agent:** QA Engineer

Comprehensive testing:
- Discovers test locations (doesn't assume `tests/unit/`)
- Runs existing tests or creates smoke tests
- Validates requirements compliance with a matrix
- Verifies security fixes are working
- Runs end-to-end smoke tests

Produces: `qa.md` with pass/fail status and recommendation.

### Phase 10: Product Validation

**Agent:** Product Validator

Final spec compliance check for this feature. Reads both sources:
- `.hephaestus/features/<feature-id>/scope.md` — the feature's stated scope and boundaries
- `.hephaestus/spec.md` — the original full design doc, to verify the feature's scope was
  correctly extracted and nothing was silently dropped or added

Compares implementation against every requirement in `scope.md`, validates
non-functional requirements, checks integration with other features already
merged, and verifies the feature's scope faithfully represents the original design intent.

Produces: `validation.md` with PASS/NEEDS_WORK verdict.

### Phase 11: Documentation Review

**Agent:** Documentation Reviewer

Reviews all documentation against the actual implementation:
- Requirements doc accuracy vs. actual code
- Architecture doc accuracy vs. file structure
- README/setup instructions correctness
- API documentation vs. actual endpoints
- Docstrings and inline comments accuracy
- Cross-document consistency

**Fixes** documentation inaccuracies, gaps, and stale content directly.

Produces: `docs.md` with findings and fixes applied.

### Phase 12: Forensics Analysis

**Agent:** Forensics Analyst

Pipeline self-improvement for this feature run. Skipped entirely on a clean
run — the orchestrator only dispatches it when the run had tmux errors — so its
presence in a run's history is itself a signal.

- Reads `run_health.json` for GOTO counts, the orchestrator's own gate
  decisions, and which phases produced errors
- Reads `phase_prompts/` for the actual agent prompt text
- Compares prompts against outcomes
- Identifies patterns in issues found across phases
- **Files prompt rewrites as proposals** for human review (see
  [Reviewing prompt changes](#reviewing-prompt-changes-improvements-tab))
- Saves feature-scoped learnings to memory for future runs

Both `run_health.json` and `phase_prompts/` are staged into the worktree's
`.hephaestus/` by the orchestrator when it dispatches this phase. There is no
`pipeline_metrics.json` to read at this point: that file is written when the
feature record is assembled, which happens after the whole workflow finishes —
after this phase. Timing and iteration data comes from `run_health.json` and the
tmux logs instead.

Produces: `forensics.md` with evidence-based improvement recommendations.

### Phase 13: Git Commit & Push

**Agent:** Git Operator

Version control workflow for this feature:
1. Pulls latest from main
2. Creates feature branch (`feature/<design-name>-<feature-id>`)
3. Commits all changes within this feature's file scope
4. Pushes feature branch
5. Creates pull request (`gh pr create`)
6. Merges PR (`gh pr merge --merge --delete-branch`)
7. Checks out main and pulls
8. Saves commit hash and PR URL to memory

### Phase 14: Deploy

**Agent:** Deployer

Conditional on `DEPLOY.md` existing in the project root. If it doesn't, the
orchestrator skips this phase entirely — no agent is launched — and fires a
synthetic completion straight through to the design aggregate step. If
`DEPLOY.md` exists, the agent reads it and follows its deployment steps
exactly (no improvising steps it doesn't specify).

Also in `optional_phases`: even when `DEPLOY.md` exists and the phase does
run, a failure here does not fail the feature (see Stop Conditions below) —
the pipeline reports the failure and moves on rather than blocking on
infrastructure it doesn't control.

---

### Design Aggregate (orchestrator-level, no agent)

After **all features** in a design complete, the orchestrator runs a final
aggregation step:
- Merges per-feature `pipeline_metrics.json` into a design-level summary
- Generates `design_report.html` in the design folder root
- Writes `design_metrics.json` with totals (time, cost, iterations across all features)
- Marks the `AutopilotDesign` as `completed` in the database

---

## Reviewing prompt changes (Improvements tab)

Forensics proposes prompt rewrites; it does not make them. Proposals land in
**Autopilot → Improvements**, where you approve or reject each one against a
real before/after diff. Nothing reaches a prompt file without that approval.

### The loop

```
forensics_analysis                 files a proposal via heph_propose_prompt_change
        ↓
Improvements tab                   before/after diff, rationale, evidence
        ↓
  approve ──────────────────────►  phase YAML written + committed, SHA recorded
  reject  ──────────────────────►  recorded with your note, file untouched
        ↓
  revert (on an applied one) ────► restores the value captured at apply time
```

### What a proposal may change, and what it may not

Only three prose fields are editable: `description`, `done_definitions`, and
`additional_notes`. Everything that wires a phase into the orchestrator is
refused at the API — `spec_gate`, `outputs`, `id`, `name`, and all of
`workflow.yaml` (evaluation points, thresholds, `required_output`,
`phase_inputs`).

This is not a UI convenience. An approved proposal that could drop
`spec_gate: true` or lower a continue threshold would silently disable a
pipeline gate while appearing in the review queue as a routine improvement —
and a disabled gate is invisible precisely because everything keeps passing. If
forensics believes a threshold is wrong it says so in `forensics.md`, for a
human to act on directly.

A phase also cannot rewrite its own prompt, which is why `forensics_analysis`
cannot propose against `forensics_analysis.yaml`. Without that, the loop has no
fixed point outside itself.

### When an approved change takes effect

**Workflows started afterwards — not runs already in flight.**

There are three copies of any phase prompt:

| Copy | Written when | Read by |
|---|---|---|
| `config/workflows/<def>/<phase>.yaml` | edited by hand, or by an approved proposal | workflow creation, once |
| The `Phase` DB row | snapshotted from the template when a workflow is created | the agent, at every dispatch |
| `PhasePromptVersion` rows | the phase-prompt draft/publish UI | overwrites that workflow's `Phase` row |

A proposal edits the **template**. A running workflow already has its snapshot,
so approving changes nothing about it. That is deliberate — forensics exists to
improve future runs, and rewriting a prompt out from under a running agent would
be worse than useless — but it means "approve" will look like it did nothing if
you are watching the current run. To change a prompt for a run in progress, use
that phase's prompt versions instead. The two mechanisms are complementary: per
definition here, per running workflow there.

### Reading the diff

The "before" side is read live from the file at the moment you open the tab, not
echoed from what the agent quoted when it filed the proposal. If the file has
changed since, the proposal is flagged **stale** — approving replaces what is
there *now*, not what forensics originally read.

Resolved proposals in History show `previous value → proposed value`, which is
what the change actually did when it landed.

### Operational notes

- Approvals are committed one file at a time, so an approval never sweeps up
  unrelated working-tree changes.
- Revert restores the value recorded at apply time rather than `git revert`-ing
  the commit, so it does not fight anything else that touched the file since.
- A proposal that fails to apply is kept and marked `failed` with the reason,
  rather than disappearing into an error response.
- Applies and reverts are serialized, so two approvals landing together cannot
  interleave and silently lose one.

---

## Full Autopilot vs. Review Mode

`AutopilotProject.review_mode` (boolean, per-project, default off) controls
whether a feature merges to main unattended or waits for a human to approve
it. Toggle it with:

```
PATCH /api/autopilot/projects/{project_id}/review-mode
Body: {"review_mode": true}
```

(`ReviewModeToggle.tsx` on the dashboard's Autopilot page calls the same
endpoint.)

### Full Autopilot (`review_mode=false`, the default)

Phase 13 (Git Commit & Push) merges the feature branch into main directly
and pushes both. The completion hard floor
(`verify_git_expert_merged_and_pushed`,
`src/services/task_completion/verification.py`) rejects the task as "done"
unless the worktree is clean, the branch is actually merged into `main`,
and `main` is pushed to the remote — a prompt instruction alone isn't
trusted.

### Review Mode (`review_mode=true`)

Phase 13 stops short of merging: it only needs the worktree clean and the
feature branch pushed to the remote, then creates or updates a pull request
(`git_expert.yaml`'s prompt is instructed not to open a second PR on a
retry — push a follow-up commit to the same branch/PR instead). The hard
floor's `review_mode` branch then checks the **real** PR status via `gh pr
view` (`get_pr_status`, `src/services/github_pr_status.py`) before letting
the phase finish:

| PR state | Result |
|---|---|
| CI failing, or `reviewDecision == CHANGES_REQUESTED` | Task rejected with the concrete reason — a real retry: the agent pushes a follow-up commit to the **same** PR/branch. |
| CI still running, no unresolved review | Task is left `in_progress`, untouched — nothing for this turn's agent to do. `Feature.pr_url` is recorded so this state is identifiable later. |
| CI passing, no unresolved review | "Done" stands; the phase completes normally. |

The "CI still running" case doesn't sit forever: a periodic sweep
(`_resolve_pending_pr_status`, `src/autopilot/orchestrator/pr_resolution.py`,
run on every phase-advancement sweep tick from
`src/mcp/server/background_loops.py`) re-checks `gh pr view` and either
marks the task `done` (advancing the phase exactly as if the agent itself
had just finished) or `failed` with the real CI/review reason — without
spinning up a fresh agent every tick just to ask "done yet?".

Once the feature's entire pipeline (through Phase 14) reaches `completed`,
the orchestrator pauses that feature's `Workflow` with `paused_by="review"`
and waits (polling every 30s) for a human decision:

```
POST /api/autopilot/features/{feature_id}/review
Body: {"action": "approve"}
Body: {"action": "request_changes", "feedback": "..."}
```

- **`approve`** clears the pause and merges the PR (`gh pr merge --merge
  --auto`, confirmed via a follow-up `gh pr view`). If that fails and
  `git.allow_local_merge_fallback` is enabled (default off), it falls back
  to a local `git merge --no-ff` into main, aborting cleanly on a real
  conflict rather than auto-resolving; local main is synced with the
  remote afterward either way.
- **`request_changes`** (feedback required) records the feedback and
  restarts or creates a task on the development phase carrying it, leaving
  the workflow paused for another review round once the fix is in.

`review_mode` fails safe to `True` on a DB read error — a check gating a
risky autonomous action (merging unattended) must not silently skip the
gate just because it couldn't be read.

---

## Iteration Loop

Iteration is scoped **per feature**, not per design.

```
Feature pipeline → gate FAIL → back to development/architecture → gate PASS
                 → remaining phases → git commit → feature done
```

A feature is marked `completed` when its **workflow** reaches `completed` — that
is, the pipeline ran to the end. Passing product validation (Phase 10) is not
the finish line: doc review, forensics, git commit and deploy all still run
after it, and `workflow.yaml`'s own `result_criteria` is "Feature validated
**and committed to git**". Feature status is written from the workflow's final
status, never from a single phase's gate result.

- `--max-iterations` (default: 3) is a **design-level** retry concept. It does
  NOT cap a feature's goto budget — that comes from `workflow.yaml`'s
  `max_total_gotos` (30 for the autopilot definition), with per-gate limits from
  each evaluation point's `max_retries`. Passing `--max-iterations` through to
  feature workflows was a real bug: it capped every feature pipeline at 3 gotos
  across all 13 phases, and `_run_one_feature` now deliberately does not forward
  it. Raising it will not give a feature more retries
- If a feature fails beyond max iterations it is marked `failed`
- Sequential features whose `depends_on` list contains a failed feature are `skipped`
- Parallel features continue regardless of other parallel feature outcomes

### Stop Conditions

| Condition | Scope | Action |
|-----------|-------|--------|
| Pipeline runs to the end (git commit) | Feature | Mark feature completed; start next pending feature |
| Hard error (crashed agent) | Feature | Stop this feature's pipeline |
| Impasse (stuck agents) | Feature | Request human input |
| API credits exhausted | Design | Request human input |
| Max iterations reached | Feature | Mark feature failed; skip dependents |
| All features complete | Design | Run aggregate report; move to next design |
| Queue empty | Pipeline | Pause, wait for new designs |
| Ctrl+C | Pipeline | Graceful shutdown |

---

## Output Structure

### Worktrees (pipeline working areas)

Worktrees are ephemeral — created per design/feature, removed after the feature
merges to main.

```
worktrees/
├── <design-id>/                       ← Phase 0 design worktree (temporary)
│   └── .hephaestus/
│       ├── spec.md                  ← original design doc
│       ├── features.json              ← written by Phase 0
│       └── features/
│           ├── auth/
│           │   └── scope.md           ← written by Phase 0
│           ├── session/
│           │   └── scope.md
│           └── admin/
│               └── scope.md
│
├── <design-id>-auth/                  ← auth feature worktree (Phases 1–14)
│   └── .hephaestus/                   ← ALL phase reports land here, never in docs/
│       ├── features.json              ← copied from Phase 0 worktree at creation
│       ├── features/
│       │   └── auth/
│       │       └── scope.md           ← copied from Phase 0 worktree at creation
│       ├── spec.md                  ← copy of the design doc, seeded at creation
│       ├── requirements.md            ← product_requirements writes flat
│       ├── architecture_design/
│       │   └── architecture.md        ← every gated phase writes to its own
│       ├── qa_validation/             ←   .hephaestus/<phase_name>/ subdirectory
│       │   └── qa.md
│       └── ...
│
├── <design-id>-session/               ← session feature worktree (Phases 1–14)
│   └── ...
│
└── <design-id>-admin/                 ← admin feature worktree (Phases 1–14)
    └── ...
```

### Permanent record

Two SIBLING trees under the project's `.hephaestus/`, not one nested tree. The
design folder is keyed by design; the feature records are keyed by timestamp and
carry the phase reports.

```
<project>/.hephaestus/
├── designs/
│   └── 20260612_auth_system_fb36c8e3/   ← <timestamp>_<design-name>_<design-id>
│       ├── auth-system.md               ← copy of the original design doc
│       ├── features.json                ← copy of the feature decomposition
│       ├── design_metrics.json          ← design_name, total_time_seconds, status,
│       │                                    features{}, completed_at (NO cost field)
│       ├── design_report.html           ← aggregated design-level report
│       └── features/
│           ├── auth/                    ← keyed by the feature SLUG from features.json
│           │   └── scope.md             ← scope.md ONLY; no phase reports here
│           ├── session/
│           │   └── scope.md
│           └── admin/
│               └── scope.md
│
└── features/                            ← the feature records — a SIBLING of designs/
    └── 20260612_auth_system/            ← <timestamp>_<design-name>, one per workflow
        ├── feature_report.html          ←   run, so sibling features of one design
        ├── docs/                        ←   differ only by timestamp
        │   ├── requirements.md          ← swept from the worktree's .hephaestus/
        │   ├── scope.md                 ←   after the pipeline finishes
        │   ├── architecture.md
        │   ├── challenge.md
        │   ├── adversarial.md
        │   ├── review.md
        │   ├── security.md
        │   ├── qa.md
        │   ├── validation.md
        │   ├── docs.md
        │   ├── summary.md
        │   ├── forensics.md             ← only when the run was not clean
        │   ├── deploy.md                ← only when DEPLOY.md exists
        │   └── pipeline_metrics.json    ← design_name, workflow_id, project_path,
        │                                    docs_dir, feature_folder, completed_at,
        │                                    stop_reason, qa_passed, product_validated
        └── tmux/                        ← per-agent session logs
```

The two are built by different code and at different times:
`_create_designs_folder` makes the design folder when the design is picked up
(and `run_phase0` copies each feature's `scope.md` into it), while
`_populate_feature_folder` makes a feature record when that feature's workflow
finishes. Nothing nests one inside the other — a `docs/` directory holding phase
reports exists only under `features/<timestamp>_<name>/`.

Neither metrics file carries cost. The feature-record API reads
`Workflow.cost_total_usd` from the DB instead; see Cost Tracking.

---

## Data Model

```
AutopilotDesign
  │  id, name, status, feature_folder, design_file_path
  │
  ├── DesignWorkflow (one per design — runs Phase 0)
  │     id, design_id, status
  │     └── Phase 0 task → Feature Architect agent → features.json
  │
  └── Feature (one per entry in features.json)
        id, design_id, name, scope, files, depends_on, execution
        status: pending | active | completed | failed | skipped
        │
        └── Workflow (one per Feature — runs Phases 1–14)
              id, feature_id, status, paused_by, cost_total_usd
              │
              └── Phase (one per pipeline phase, 1–14)
                    │
                    └── Task (one per phase execution)
                          │
                          └── Agent
```

Key points:
- `Feature.execution` is `"parallel"` or `"sequential"` — set by the Feature Architect, never overridden by the orchestrator
- `Feature.depends_on` is a list of feature IDs; the orchestrator enforces ordering at Workflow launch time
- Phase 0 runs in a `DesignWorkflow` scoped to the design, separate from any Feature's workflow
- Single-feature designs have one `Feature` record and one `Workflow` — functionally equivalent to the old model
- `PromptProposal` sits deliberately **outside** this hierarchy. It is keyed on
  `workflow_definition` + `phase_name`, not on a Workflow, because it targets the
  phase *template* that future workflows are created from rather than any one
  run — see [Reviewing prompt changes](#reviewing-prompt-changes-improvements-tab).
  (`PhasePromptVersion`, by contrast, is keyed on a `Phase` row and so belongs to
  exactly one Workflow.)

---

## Worktree Strategy

### Phase 0 worktree (design-scoped)

Phase 0 runs in a single design-level worktree. It reads `spec.md`, writes
`features.json`, and writes one `scope.md` per feature. This worktree is
discarded after Phase 0 completes.

### Feature worktrees (feature-scoped, shared across all phases)

Each Feature is assigned **one dedicated git worktree** for its entire pipeline
run. All phases (1–14) of that feature operate in the same worktree. No phase
creates its own worktree.

**Naming:** `worktrees/<design-id>-<feature-id>/`

**Creation:** The orchestrator creates the feature worktree immediately after
reading `features.json`, before launching Phase 1. At creation time the
orchestrator copies:
- `.hephaestus/features.json` → `<worktree>/.hephaestus/features.json`
- `.hephaestus/features/<feature-id>/scope.md` → `<worktree>/.hephaestus/features/<feature-id>/scope.md`

From that point on, every phase agent reads `scope.md` from the worktree-local
path `.hephaestus/features/<feature-id>/scope.md`. No agent needs to reach
outside its own worktree.

**Branch:** Each feature worktree is checked out on its own feature branch
(`feature/<design-name>-<feature-id>`). Phase 13 (Git Commit & Push) commits
and merges this branch to main, then the orchestrator removes the worktree.

**Isolation:** Parallel features run in separate worktrees and cannot see each
other's uncommitted changes. Sequential features that depend on a prior feature
pull from main at the start of Phase 1, picking up whatever the prior feature
merged.

### Permanent storage of features.json and scope.md

`.hephaestus/` is ephemeral — not committed to git, removed with the worktree.
`features.json` and `scope.md` must be persisted to the feature record folder
before the worktrees are discarded.

**After Phase 0 completes**, the orchestrator immediately copies from the design
worktree to the feature record folder:
- `.hephaestus/spec.md` → `designs/<timestamp>_<design>_<design-id>/spec.md`
- `.hephaestus/features.json` → `designs/<timestamp>_<design>_<design-id>/features.json`
- `.hephaestus/features/<id>/scope.md` → `designs/<timestamp>_<design>_<design-id>/features/<id>/scope.md`
  (one copy per feature)

The design worktree is then discarded. These copies in the feature record folder
are the permanent record. Feature worktrees read `scope.md` from their own
`.hephaestus/` (copied in at creation) — not from the feature record folder.

---

## HTML Reports

### Feature Report

Each feature produces `feature_report.html` at:
```
designs/<timestamp>_<design>_<design-id>/features/<feature-id>/feature_report.html
```

Includes: pipeline metrics, QA and validation status, all phase summaries,
cost breakdown, forensics recommendations, files created.

### Design Report

After all features complete, `design_report.html` is written at:
```
designs/<timestamp>_<design>_<design-id>/design_report.html
```

Includes: per-feature status table, aggregate cost and time, list of PRs merged,
outstanding issues across features.

---

## Cost Tracking

When LiteLLM proxy is configured, LLM calls include a `user` field:
- Phase 0: `<design-name>/feature-architect`
- Phases 1–14: `<design-name>/<feature-id>`

```bash
export LITELLM_PROXY_URL=http://deneb-server:4000
export LITELLM_API_KEY=sk-virtual-key
export LITELLM_MASTER_KEY=sk-master-key
export LITELLM_COST_TRACKING=true

heph autopilot start --project-path ~/my-project
```

Costs appear in:
- The dashboard, from the `CostEntry` ledger rolled up to `cost_total_usd`
  (see Budget Enforcement below) — this is the authoritative source
- LiteLLM dashboard (grouped by `user` field)

They do **not** appear in `pipeline_metrics.json` or `design_metrics.json`.
Neither writer emits a cost field: `design_metrics.json` carries
design_name / design_document / project_path / designs_folder /
total_time_seconds / status / features / completed_at, and
`pipeline_metrics.json` carries design_name / workflow_id / project_path /
docs_dir / feature_folder / completed_at / stop_reason / qa_passed /
product_validated. Use the DB rollups for cost, not these files.

### Budget Enforcement

Independent of LiteLLM, every agent's task cost is recorded to a `CostEntry`
ledger via `POST /cost-entries` and rolled up into `cost_total_usd` on `Task`,
`Feature`, `Workflow`, `AutopilotDesign`, and `AutopilotProject` via
`src/core/cost_derivation.py`. Two collectors feed this ledger:
`ClaudeCodeCollector` (Claude Code sessions) and, for Pi, either the
real-time `hephaestus-cost-tracker` pi extension (installed/built
automatically by `scripts/install.sh` when `pi` is detected, posting costs
on `turn_end`) or, if the extension isn't built (e.g. `npm` unavailable), a
JSONL-tailing fallback in `src/services/cost_collection_service.py` that
picks up the same costs at task-completion time instead of in real time.

Set a spending cap per project with `cost_limit_usd` (project settings in the
UI, or `PUT /projects/{id}`). Once a project's `cost_total_usd` reaches its
`cost_limit_usd`:
- All of that project's active/running workflows (including Phase 0) are
  paused with `paused_by="budget"`
- The orchestrator's `pick_next_design()` and `_run_one_feature()` refuse to
  start new work for the project
- Self-heal/auto-resume logic (`_try_auto_resume_paused_workflow`,
  `_create_corrective_task`, stuck-workflow restart) skips `"budget"`-paused
  workflows — only raising or clearing `cost_limit_usd` resumes them
- The dashboard shows a "Paused: budget limit reached" badge on affected
  workflows and a budget indicator on the project/design screen

A `"user"`-initiated pause (stop button) is independent of budget pauses:
clicking play resumes `paused_by="user"` workflows but never
`paused_by="budget"` ones.

New projects inherit a system-wide default cap
(`get_default_cost_limit`, `src/services/system_settings.py`, stored as the
`settings:default_cost_limit_usd` key) if one is configured; `None` (the
factory default) means unlimited. Set or clear it dashboard-wide with:

```
PUT /api/autopilot/settings/default-budget
Body: {"default_cost_limit_usd": 25.0}
```

This only seeds newly-created projects — it does not retroactively change
an existing project's own `cost_limit_usd`.

Raise or clear a specific project's limit with `PUT /projects/{id}`:

```
PUT /api/autopilot/projects/{project_id}
Body: {"cost_limit_usd": 50.0}
Body: {"clear_cost_limit": true}
```

Either call automatically resumes (`force=True`) every one of that
project's workflows currently paused with `paused_by="budget"` — you don't
need a separate resume step after raising the cap. Sending
`cost_limit_usd: null` alone does **not** clear the limit; use
`clear_cost_limit: true` explicitly, or the update leaves the existing
limit untouched (this lets a partial `PUT` update other project fields
without accidentally wiping the budget).

---

## Vector Database Integration

The pipeline uses the Hephaestus vector database (Qdrant or TurboVec) for
cross-feature and cross-design learning.

### Writing
- Phase 0: feature decomposition decisions, execution ordering rationale
- Phase 1–14 (per feature): requirements, architecture, implementation notes,
  review findings, security findings, QA results, validation outcomes,
  commit references, improvement recommendations

### Reading
Phase 0 (Feature Architect) searches memory before decomposing:
```
search_memory("feature decomposition patterns scope boundaries")
search_memory("parallel sequential execution dependency patterns")
search_memory("completed features implemented components", memory_type="decision")
```

Phase 1 searches memory before extracting requirements:
```
search_memory("technology stack decisions framework language")
search_memory("architecture patterns system design components")
search_memory("constraints must not rules security requirements")
```

This ensures each new design benefits from all prior decompositions and
implementation decisions.

---

## Design Queue

The design queue is a list of `AutopilotDesign` records in the database. Each
record stores the absolute path to the design document — the file can live
anywhere on the filesystem. The pipeline processes queued designs in
priority/creation order.

- Design docs can live anywhere — a personal notes folder, a shared drive, a
  repo subdirectory, anywhere the pipeline process can read
- The DB record is the source of truth for queue state; the file is read at
  pipeline start time
- If the file has changed since it was registered, the pipeline reads the
  current content (re-registering is not required)

### Adding Designs

```bash
# Register any .md or .txt file by absolute or relative path
heph autopilot add ~/designs/auth-system.md --project-path ~/my-project
heph autopilot add ./specs/dashboard.md --project-path ~/my-project
```

### Queue Status

```bash
heph autopilot queue --project-path ~/my-project
```

### Alternative source: Spec Kit

A design doesn't have to be a hand-written `.md` file. If the project has a
[GitHub Spec Kit](https://github.com/github/spec-kit) `specs/<NNN>-<name>/`
directory, autopilot can build directly from it instead — either pinned
explicitly (`heph autopilot start --feature 001-checkout-flow`) or
automatically enqueued once a feature has a `plan.md`
(`speckit_auto_scan_enabled`, per project). Both paths feed the same design
queue described above. See [Spec Kit Support](speckit.md) for the full
detection, selection, and auto-scan behavior.

---

## LiteLLM Proxy Integration

All LLM calls can optionally route through a LiteLLM proxy for cost tracking.

### How It Works

1. `OpenRouterClient` checks for `litellm_proxy_url` in config
2. If set, requests are routed through the proxy instead of directly to OpenRouter
3. Each request includes `"user": "<design-name>/<feature-id>"` for per-feature tracking
4. The proxy returns cost in the `x-litellm-response-cost` response header
5. After each feature completes, the orchestrator queries LiteLLM spend endpoints

### Configuration

In `hephaestus_config.yaml`:
```yaml
llm:
  litellm_proxy:
    url: http://deneb-server:4000
    api_key_env: LITELLM_API_KEY
    cost_api_key_env: LITELLM_MASTER_KEY
    cost_tracking: true
```

Or via environment variables:
```bash
export LITELLM_PROXY_URL=http://deneb-server:4000
export LITELLM_API_KEY=sk-virtual-key
export LITELLM_MASTER_KEY=sk-master-key
export LITELLM_COST_TRACKING=true
```

### Cost Queries

The `CostTracker` module (`src/interfaces/cost_tracker.py`) queries:
- `/user/info?user_id=<design>/<feature>` — total spend per feature
- `/user/daily/activity` — daily breakdown by model
- `/global/spend/report?group_by=customer` — all features across all designs

---

## Context-Aware Design

| Phase | Scope | Reads From | Writes To |
|-------|-------|-----------|-----------|
| 0  | Design  | spec.md, AGENTS.md, designs/, vector DB | features.json, features/\<id\>/scope.md (per feature) |
| 1  | Feature | scope.md, AGENTS.md, features/, vector DB | requirements.md |
| 2  | Feature | scope.md, spec.md, requirements.md | scope.md |
| 3  | Feature | scope.md, requirements.md | architecture.md |
| 4  | Feature | scope.md, requirements.md, architecture.md | challenge.md |
| 5  | Feature | architecture.md, challenge.md, AGENTS.md | Source code, tests |
| 6  | Feature | requirements.md, architecture.md, source code | adversarial.md |
| 7  | Feature | architecture.md, requirements.md, adversarial.md | review.md |
| 8  | Feature | requirements.md, architecture.md, adversarial.md | security.md, code fixes |
| 9  | Feature | requirements.md, architecture.md, all review reports | qa.md |
| 10 | Feature | scope.md, spec.md, requirements.md, architecture.md, qa.md | validation.md |
| 11 | Feature | All reports, source code | docs.md, summary.md, feature_report.html, doc fixes |
| 12 | Feature | All docs, run_health.json, phase_prompts/ | forensics.md, prompt proposals, memory entries |
| 13 | Feature | Committed source, forensics.md | Git commit, PR, merge |
| 14 | Feature | Merged code, deployment config | deploy.md, deployment output/logs |
| —  | Design  | All feature outputs | design_report.html, design_metrics.json |

---

## Related

- [Spec Kit Support](speckit.md) — building directly from a Spec Kit
  `specs/<NNN>-<name>/` directory instead of a hand-written design.
- [Multi-Repo Projects](multi-repo-projects.md) — registering child repos
  on a project and binding a `Feature` to one of them.
