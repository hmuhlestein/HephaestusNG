# Autopilot Pipeline

A fully automated multi-agent workflow engine that takes design documents,
decomposes them into features, and drives each feature through a 14-phase
pipeline to produce validated, committed, shipped software.

## Overview

```
DB queue                   Phase 0 (once)        per-feature pipeline      project/
  ├── auth-system.md  ──► [Feature Architect] ──► auth    (parallel) ──►    ├── src/
  │   (anywhere)            │                 ──► session (parallel) ──►    ├── tests/
  ├── dashboard.md          ▼                ──► admin   (sequential) ──►   └── designs/
  └── api-v2.md        features.json              (phases 1–14 each)             └── 20260612_auth_system_fb36c8e3/
                                                                                     ├── design_report.html
                                                                                     ├── features.json
                                                                                     └── features/
                                                                                         ├── auth/
                                                                                         │   └── docs/...
                                                                                         ├── session/
                                                                                         │   └── docs/...
                                                                                         └── admin/
                                                                                             └── docs/...
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
- `completed`: all features passed product validation and merged
- `failed`: one or more features failed beyond max iterations
- `skipped`: design removed from queue before completion

**Feature states:**
- `pending`: created from `features.json`; waiting for dependencies or parallel slot
- `active`: pipeline phases running
- `completed`: product validation passed; feature merged to main
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

## Pipeline Phases

### Phase 0: Feature Architect (design-scoped, runs once per design)

**Agent:** Feature Architect

Runs **once per design**, before any feature pipelines start. Reads the full
design document and the project context, then decomposes the design into
features with explicit execution ordering.

- Reads `AGENTS.md` for repository guidelines
- Searches `designs/` for previously shipped work that may inform scope boundaries
- Queries vector DB for prior decomposition decisions and dependency patterns
- Reads the full design document (`.hephaestus/design.md`)

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

Produces: `requirements_analysis.md` scoped strictly to this feature —
functional/non-functional requirements, component dependencies, technology
constraints, and integration points with other features as defined in `scope.md`.

### Phase 2: Scope Review

**Agent:** Scope Reviewer

Gate between product requirements and architecture. Verifies that
`requirements_analysis.md` is a faithful, complete extraction of this feature's
scope — nothing added, nothing dropped.

- Reads `.hephaestus/features/<feature-id>/scope.md` — the feature's stated scope
- Reads `.hephaestus/design.md` — the original full design doc, to verify `scope.md`
  correctly represents the original intent for this feature's slice
- Reads `requirements_analysis.md` — what Phase 1 produced
- Traces every requirement back to a line in `scope.md` and ultimately to `design.md`

Produces: `scope_review_result.md` — a YAML frontmatter block (OKF format:
`type` first, then verdict PASS or FAIL and supporting fields) followed by
the narrative report. On FAIL, returns to Phase 1 with specific correction
instructions. This is a binary gate — no architecture starts until scope is
clean.

### Phase 3: Architecture & Design

**Agent:** Software Architect

Primary input is this feature's `scope.md` and `requirements_analysis.md`.

- Reads `.hephaestus/features/<feature-id>/scope.md` for boundary constraints
- Uses `requirements_analysis.md` as the detailed requirements input
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

Produces: `architectural_review_report.md` — a YAML frontmatter block (OKF
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

Produces: `security_report.md` with findings and fixes applied.

### Phase 9: QA Validation

**Agent:** QA Engineer

Comprehensive testing:
- Discovers test locations (doesn't assume `tests/unit/`)
- Runs existing tests or creates smoke tests
- Validates requirements compliance with a matrix
- Verifies security fixes are working
- Runs end-to-end smoke tests

Produces: `qa_report.md` with pass/fail status and recommendation.

### Phase 10: Product Validation

**Agent:** Product Validator

Final spec compliance check for this feature. Reads both sources:
- `.hephaestus/features/<feature-id>/scope.md` — the feature's stated scope and boundaries
- `.hephaestus/design.md` — the original full design doc, to verify the feature's scope was
  correctly extracted and nothing was silently dropped or added

Compares implementation against every requirement in `scope.md`, validates
non-functional requirements, checks integration with other features already
merged, and verifies the feature's scope faithfully represents the original design intent.

Produces: `product_validation.md` with PASS/NEEDS_WORK verdict.

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

Produces: `doc_review_report.md` with findings and fixes applied.

### Phase 12: Forensics Analysis

**Agent:** Forensics Analyst

Pipeline self-improvement for this feature run:
- Reads `pipeline_metrics.json` for real timing/iteration data
- Reads `phase_prompts/` for actual agent prompt text
- Compares prompts against outcomes
- Identifies patterns in issues found across phases
- Proposes specific prompt rewrites with before/after text
- Saves feature-scoped learnings to memory for future runs

Produces: `forensics_report.md` with evidence-based improvement recommendations.

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

Executes this project's configured deployment steps for the feature once it
has been committed and merged. Optional phase — failure here does not fail
the feature (see Stop Conditions below); the pipeline reports the failure
and moves on rather than blocking on infrastructure it doesn't control.

---

### Design Aggregate (orchestrator-level, no agent)

After **all features** in a design complete, the orchestrator runs a final
aggregation step:
- Merges per-feature `pipeline_metrics.json` into a design-level summary
- Generates `design_report.html` in the design folder root
- Writes `design_metrics.json` with totals (time, cost, iterations across all features)
- Marks the `AutopilotDesign` as `completed` in the database

---

## Iteration Loop

Iteration is scoped **per feature**, not per design.

```
Feature pipeline → Validation FAIL → Feature pipeline again → Validation PASS → feature done
```

- Maximum iterations configurable via `--max-iterations` (default: 3, per feature)
- If a feature fails beyond max iterations it is marked `failed`
- Sequential features whose `depends_on` list contains a failed feature are `skipped`
- Parallel features continue regardless of other parallel feature outcomes

### Stop Conditions

| Condition | Scope | Action |
|-----------|-------|--------|
| Product validation passes | Feature | Mark feature completed; start next pending feature |
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
│       ├── design.md                  ← original design doc
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
│   ├── .hephaestus/
│   │   ├── features.json              ← copied from Phase 0 worktree at creation
│   │   └── features/
│   │       └── auth/
│   │           └── scope.md           ← copied from Phase 0 worktree at creation
│   └── docs/
│       ├── requirements_analysis.md
│       ├── architecture.md
│       └── ...
│
├── <design-id>-session/               ← session feature worktree (Phases 1–14)
│   └── ...
│
└── <design-id>-admin/                 ← admin feature worktree (Phases 1–14)
    └── ...
```

### Permanent record

Design folders live under `designs/`. Each folder is named
`<timestamp>_<design-name>_<design-id>` so it can be looked up from the DB
record by ID and is human-readable by name.

```
designs/
└── 20260612_auth_system_fb36c8e3/     ← design folder (timestamp_name_id)
    ├── design.md                      ← copy of original design doc
    ├── design_report.html             ← aggregated design-level report
    ├── design_metrics.json            ← totals: time, cost, iterations
    ├── features.json                  ← copy of feature decomposition
    └── features/
        ├── auth/                      ← per-feature folder
        │   ├── scope.md               ← copy of feature scope doc
        │   ├── feature_report.html
        │   └── docs/
        │       ├── requirements_analysis.md
        │       ├── architecture.md
        │       ├── adversarial.md
        │       ├── doc_review_report.md
        │       ├── security_report.md
        │       ├── qa_report.md
        │       ├── product_validation.md
        │       ├── forensics_report.md
        │       ├── pipeline_metrics.json
        │       └── phase_prompts/
        ├── session/
        │   └── ...
        └── admin/
            └── ...
```

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

---

## Worktree Strategy

### Phase 0 worktree (design-scoped)

Phase 0 runs in a single design-level worktree. It reads `design.md`, writes
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
- `.hephaestus/design.md` → `designs/<timestamp>_<design>_<design-id>/design.md`
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
- The HTML feature report (Cost Tracking section)
- `pipeline_metrics.json` (`cost_total` field, per feature)
- `design_metrics.json` (`cost_total` field, summed across all features including Phase 0)
- LiteLLM dashboard (grouped by `user` field)

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
| 0  | Design  | design.md, AGENTS.md, designs/, vector DB | features.json, features/\<id\>/scope.md (per feature) |
| 1  | Feature | scope.md, AGENTS.md, features/, vector DB | requirements_analysis.md |
| 2  | Feature | scope.md, design.md, requirements_analysis.md | scope_review_result.md |
| 3  | Feature | scope.md, requirements_analysis.md | architecture.md |
| 4  | Feature | scope.md, requirements_analysis.md, architecture.md | challenge.md |
| 5  | Feature | architecture.md, challenge.md, AGENTS.md | Source code, tests |
| 6  | Feature | requirements_analysis.md, architecture.md, source code | adversarial.md |
| 7  | Feature | architecture.md, requirements_analysis.md, adversarial.md | architectural_review_report.md |
| 8  | Feature | requirements_analysis.md, architecture.md, adversarial.md | security_report.md, code fixes |
| 9  | Feature | requirements_analysis.md, architecture.md, all review reports | qa_report.md |
| 10 | Feature | scope.md, design.md, requirements_analysis.md, architecture.md, qa_report.md | product_validation.md |
| 11 | Feature | All reports, source code | doc_review_report.md, doc fixes |
| 12 | Feature | All docs, pipeline_metrics.json, phase_prompts/ | forensics_report.md, memory entries |
| 13 | Feature | Committed source, forensics_report.md | Git commit, PR, merge |
| 14 | Feature | Merged code, deployment config | Deployment output/logs |
| —  | Design  | All feature outputs | design_report.html, design_metrics.json |
