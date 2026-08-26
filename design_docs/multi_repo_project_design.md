# Multi-Repo Project Support

## Problem

Today one `AutopilotProject` == exactly one git repository. `base_dir` is a
single, `unique=True` path, `WorktreeManager` opens exactly one `git.Repo`,
and commit resolution (`_resolve_repo_path_for_commit` in `tickets_api.py`)
walks a bare `commit_sha` straight to one repo path with no disambiguation.

Goal: let one Hephaestus project span multiple git repos (e.g. `backend/`
and `frontend/` as siblings), with agents doing writes/commits scoped to a
single repo per task, while still being able to read across repos for
cross-stack context (e.g. reading the API contract in `backend/` while
implementing the consumer in `frontend/`).

## Confirmed single-repo assumptions (current code)

- **`AutopilotProject`** (`src/core/database.py:1076`) — `base_dir: Text,
  unique=True`. No repo-url/git-remote field, no multi-directory concept.
- **`WorktreeManager`** (`src/core/worktree_manager.py:~101`) — opens exactly
  one `git.Repo(project_root)` at init; `reload(new_path)` swaps the whole
  manager to a *different single* repo, never holds two.
- **Git call sites**, all single-`cwd`/single-`git.Repo`: `policy.py:248-281`
  (recovery: status/merge --abort/checkout/clean/reset), `worktree_integration.py`
  (multiple `git.Repo(...)` calls), `queue.py:132`, `pipeline.py:1216,1232`,
  `terminator.py:366`, `tickets_api.py:1341,1368,1396`.
- **`TicketCommit`** (`database.py:1009`) — `commit_sha`, `ticket_id`,
  `agent_id`. No repo field — a bare SHA is assumed globally resolvable to
  one repo per project.
- **Task dispatch** (`launch_pipeline.py:192-216`, `manager.py:242-246`) —
  `_resolve_project_base_dir` returns one `Path`; `WorktreeManager.reload()`
  is called with it. No `task.repo_id` or equivalent.
- **Design storage** — `AutopilotDesign.file_path`/`_create_designs_folder`
  build paths as `Path(project.base_dir)/.hephaestus/designs/...`. The
  `docs/` destination feature shipped earlier this session is the same
  pattern: `Path(base_dir)/docs`.
- **Pipeline concurrency is per-project, not per-repo** — `pick_next_design`
  picks *one* next design per project; its own docstring states "two
  concurrent `run_continuous_pipeline` loops (one per project...)". A
  project's design queue is processed serially, one design/workflow at a
  time.
- **Phase model already has a per-phase hook**: `Phase.working_directory`
  (`database.py:502`) — "Default working directory for agents in this
  phase," currently unused for multi-repo purposes but a natural extension
  point.
- Related prior art: `design_docs/unified_project_system.md` established
  `AutopilotProject.base_dir` as the single source of truth for "the
  project's repo," and documented that `WorktreeManager` caches its `Repo`
  object at init and needs explicit `reload()` to point elsewhere — the same
  constraint this design has to work around, just now for N repos instead
  of a hot-swappable 1.

## Proposed design

### Data model

Add `ProjectRepo`:

```python
class ProjectRepo(Base):
    __tablename__ = "project_repos"
    id = Column(String, primary_key=True)
    project_id = Column(String, ForeignKey("autopilot_projects.id"), nullable=False)
    label = Column(String, nullable=False)       # "backend", "frontend"
    path = Column(Text, nullable=False)           # absolute; not required to be under base_dir
    is_primary = Column(Boolean, default=False, nullable=False)
    __table_args__ = (UniqueConstraint("project_id", "path"), UniqueConstraint("project_id", "label"))
```

`path` is absolute rather than relative-to-`base_dir` — a child repo does not
have to live inside the project's workspace folder (e.g. an existing clone
elsewhere on disk).

Add `repo_id` (FK to `project_repos.id`, nullable) to: `Task`, `Ticket` (or
wherever tickets currently derive their repo via `Workflow.project_id`),
`TicketCommit`, `AgentWorktree`/`AgentBranch`. Nullable so existing rows for
single-repo projects don't need backfilling beyond the migration below.

### Migration (non-destructive)

For every existing `AutopilotProject`, create one `ProjectRepo` row with
`path = project.base_dir`, `is_primary = True`. `base_dir` itself is
untouched — for a single-repo project it continues to mean exactly what it
means today (the repo root). Nothing existing reads `repo_id`, so no
backfill of historical `Task`/`TicketCommit` rows is required; unresolved
`repo_id` falls back to the project's primary repo everywhere the old
single-repo code path used to run.

### Write/read scoping (per your steer: one repo per agent for writes, siblings readable)

- `WorktreeManager` stays single-repo-per-instance in shape — just
  parameterized by `ProjectRepo` instead of hardcoded to `project.base_dir`.
  One instance per `(project, repo)` pair that has active work, not one
  singleton per project.
- An agent's worktree/branch/commit machinery is created against exactly
  its task's `repo_id`. No multi-worktree-per-agent machinery needed.
- Sibling repos are **not** worktree'd for that agent — they're visible at
  their canonical `ProjectRepo.path` (the main clone, not a worktree), which
  the task's launch context/prompt exposes as a documented read path.
- Enforcement: soft. The task prompt states which paths are read-only.
  Commit-time validation (wherever `TicketCommit` rows get created) checks
  the commit's changed files fall under the task's `repo_id` path —
  reusing existing commit-linking code, adding one path-prefix check.
  Hard filesystem enforcement (read-only bind mounts/chmod) was considered
  and rejected for v1: this is a local, single-user, no-auth tool per your
  earlier call, and read-only mounts complicate any tooling that wants to
  open the sibling repo normally. Revisit only if soft enforcement proves
  insufficient in practice.

### Design/doc storage — resolved contradiction

Initial framing ("keep `.hephaestus/designs/` and `docs/` at the project
level, outside any child repo") doesn't hold for `docs/`: it's git-tracked
by design (that's the whole point of the feature just shipped), and a
multi-repo project's `base_dir`/workspace root is not necessarily a git repo
itself — writing there wouldn't be tracked by anything.

Resolution: `docs/` uploads resolve to the **primary** `ProjectRepo`'s path
(`ProjectRepo.is_primary`), not the workspace root. `.hephaestus/designs/`
(never git-tracked, purely a staging area) can stay at the workspace-root
level (`base_dir`) since it has no git-tracking requirement to satisfy.

### Commit resolution

`_resolve_repo_path_for_commit` (`tickets_api.py:37`) takes `repo_id`
instead of walking straight to `base_dir`, since a bare `commit_sha` is no
longer guaranteed unique across a project's repos (two independent repos
can produce colliding short SHAs). Every `git show <sha>`/`git diff` call
site in `tickets_api.py` needs the resolved repo path, not just the resolved
project path.

### Recovery/cleanup

`policy.py`'s recovery commands, `worktree_integration.py`'s cleanup, and
`terminator.py` all currently resolve a single `cwd`/`git.Repo()` from one
project path. Each needs `repo_id` threaded through so recovery targets the
correct child repo's worktree — recovering "the project" is no longer
well-defined once there's more than one repo.

### Existing cross-feature dependency system (corrects earlier framing in
### this doc — there IS one, already built and running)

`Feature` (`database.py:1162`) already has `depends_on` (JSON list of
`feature_key` strings) and `execution` (`parallel`/`sequential`).
`run_feature_pipelines` (`pipeline.py:2254`) resolves these into execution
groups via Kahn's-algorithm topological sort (`_resolve_execution_order`,
`features.py:591`), cycle-checks the graph first (`has_cycle`), and — this
is the important part — genuinely runs every feature within a group
**concurrently**, via a real `ThreadPoolExecutor` (`pipeline.py:2280`), not
just a sequential loop with a "parallel" label. A runtime gate
(`pipeline.py:1949-1960`) also re-checks each feature's `depends_on` at
launch time and skips it if a dependency hasn't reached `completed`/`active`
yet.

This means: two `Feature`s with no `depends_on` edge between them, under the
**same design**, already execute in parallel today — including, with this
design's changes, two features bound to different repos. A backend feature
and a frontend feature that depends on it are expressed exactly as you'd
expect: the frontend feature's `depends_on: ["backend-feature-key"]`. No new
dependency/concurrency mechanism needs to be built — `depends_on`/`execution`
already do this job.

Crucially, `WorktreeManager` instances are already created fresh per
operation (e.g. `pipeline.py:577`, `:751` — each call does
`WorktreeManager(db_manager=...)` then `.reload(project_path)`), not held as
one long-lived singleton shared across concurrently-running features. That
means parameterizing those call sites' `project_path` by the feature's
`repo_id` → `ProjectRepo.path` (instead of always `project.base_dir`) is a
drop-in fit with the existing concurrency model — no restructuring of
`run_feature_pipelines` itself is needed for multi-repo features to run
safely in parallel.

## Gaps found in this pass (open questions / follow-on scope)

1. **Parallelism is free at the feature level, not at the design level.**
   Corrected from an earlier pass of this doc, which incorrectly claimed no
   cross-feature dependency/concurrency mechanism exists — see the section
   above. What's still genuinely serialized is *designs*, not features:
   `pick_next_design` picks one active design per project, so two separate,
   unrelated **design docs** each targeting a different repo would still run
   one after another. This matters much less now that the feature architect
   keeps a single design's frontend+backend work as two features (with
   `depends_on` between them as needed) rather than two designs — the
   common case (one feature request spanning both repos) already gets real
   parallelism for free. Cross-*design* parallelism remains future work (see
   below), scoped narrowly now that the bigger piece isn't needed.
2. **Frontend has no repo concept anywhere.** `GitDiffModal`/commit views,
   project creation/settings UI, and `LoadDesignModal`'s "stored in `docs/`"
   label all assume one repo. Per your steer: no bespoke new UI surface for
   this — a multi-repo project should just be detected (`ProjectRepo` count
   > 1) and handled by the existing views using data the backend already
   returns, the same way the dashboard already handles multiple concurrent
   *projects* without a dedicated "multi-project mode." Concretely:
   `GitDiffModal` shows the repo a commit belongs to because `TicketCommit`/
   the commit-resolution endpoint now returns `repo_id`/label, not because a
   new picker component was built. The one place that unavoidably needs new
   UI is adding/labeling child repos on a project (there's no way to detect
   that from existing data) — keep that minimal, a small addition to
   existing project-settings UI, not a new page. Task/ticket-level repo
   *assignment* should not be a user-facing picker at all — see the prompt
   work below; the feature architect assigns it as part of decomposition.
3. **`Phase.working_directory` overlap.** Phases already have a per-phase
   working-directory override, unused today. Decide whether phase-level repo
   assignment (a "backend implementation" phase defaults its tasks to the
   backend `ProjectRepo`) reuses/extends this field or `repo_id` is added
   separately — reusing it avoids a second mechanism doing the same job.
4. **`AutopilotProject.is_active`/`max_concurrent_projects` invariant is
   unaffected** — that gate is per-project, not per-repo; multi-repo doesn't
   change its semantics. Verified, no action needed.
5. **`WorktreeManager` branch/dir collisions are not a real risk** — each
   instance is scoped to its own repo's `.worktrees/`, and `agent_id` is
   already globally unique, so N per-repo managers can't collide. Verified,
   no action needed.
6. **`queue_routes.py:768`'s repair-flow prompt string** (already a known,
   deliberately-deferred limitation from the earlier `docs/`-destination
   change) still hardcodes `DESIGN_CONTEXT_SUBDIR`/single-path assumptions —
   will need the same `repo_id`-awareness eventually; still out of scope for
   a first cut.

## Agent prompt awareness

The feature architect and implementation-phase agents currently have no
concept of "more than one repo" at all — their prompts assume the codebase
they're reading and writing is the one thing at `{project_context}`. This is
not optional polish: without it, the feature architect (the agent that
decomposes a design into `Feature`/`Task` rows in the first place) has no
way to assign a task to the correct repo, and an implementation agent has no
way to know a sibling repo is present-but-read-only rather than just absent.

Found the natural single injection point: `AgentManager.get_project_context()`
(`src/agents/manager.py:710`) builds the `{project_context}` string used by
every phase prompt, including `feature_architect_system_prompt`
(`config/prompts/system_prompts.yaml:88`) and the per-phase implementation
prompts under `config/workflows/autopilot/` (e.g. `development.yaml`). One
change here reaches every agent, rather than editing N phase prompt files
individually.

For a multi-repo project, `get_project_context()` should include:

- The list of `ProjectRepo`s (label + path) — so the feature architect knows
  what's available to assign work to.
- For an implementation-phase agent (task already has `repo_id` from
  feature-architect assignment): which repo is **writable** (its own,
  worktree'd as today) vs. which are **read-only reference** (sibling repos,
  canonical clone paths, not worktree'd for this agent) — stated plainly,
  since this is a behavioral instruction to the model, not just data.
- For the feature architect specifically (no `repo_id` yet — it's the one
  deciding assignment): a **hard rule**, not a preference — every `Feature`
  it creates must be bound to exactly one repo. A design that needs both an
  API change and its UI consumer is not one feature spanning two repos; it's
  two features, one bound to the backend repo and one to the frontend repo,
  same as the write-scoping rule established earlier in this doc (one repo
  per agent/task for writes). The architect infers the right repo per
  feature from the design doc's implied file paths and each `ProjectRepo`'s
  structure. Ordering between them uses the **existing** `Feature.depends_on`/
  `execution` mechanism (see "Existing cross-feature dependency system"
  above) — e.g. the frontend feature declares
  `depends_on: ["backend-feature-key"]` — not an ad-hoc note in a
  description. This is not new engineering for the architect's prompt to
  invoke; it just needs to be told the mechanism exists and used correctly
  across repo boundaries the same way it's presumably already used within a
  single repo today.

For a single-repo project (the common case, and every existing project via
the migration), this section of `get_project_context()` should emit nothing
extra — no behavior change, no new text in the prompt, since there's only
one repo and the existing single-repo instructions already cover it.

## Suggested implementation order

1. `ProjectRepo` model + migration (auto-populate from existing `base_dir`s).
2. `WorktreeManager` parameterization (per-repo instances instead of a
   project-wide singleton).
3. `repo_id` threading through `Task`/`TicketCommit`/`AgentWorktree`/
   `AgentBranch` + commit resolution in `tickets_api.py`.
4. `AgentManager.get_project_context()` + `feature_architect_system_prompt`:
   surface the repo list, writable-vs-read-only distinction, and per-repo
   task-assignment instructions (see "Agent prompt awareness" above);
   commit-time path-prefix validation.
5. `docs/`-destination fix: resolve to primary `ProjectRepo`, not workspace
   root.
6. Frontend: minimal project-settings addition to add/label child repos;
   everything else (`GitDiffModal` repo labeling, etc.) rides on data the
   backend now returns rather than new UI surfaces.

Each step should land with its own red→green regression tests before the
next starts, per the usual discipline — this list is a sequencing proposal,
not a commitment to build all of it in one pass.

## Requirements

Grouped by feature area, mirroring the sections above. IDs are stable
references for implementation/tracking, not a commit-by-commit checklist —
one commit may satisfy several REQs in a group, per the implementation
order above.

### Feature: Data Model

- **REQ-01**: Add a `ProjectRepo` model (`id`, `project_id` FK, `label`,
  `path`, `is_primary`) with unique constraints on `(project_id, path)` and
  `(project_id, label)`.
- **REQ-02**: Add a nullable `repo_id` FK (→ `project_repos.id`) to
  `Task`, `Ticket`, `TicketCommit`, and `AgentWorktree`/`AgentBranch`.
- **REQ-03**: `ProjectRepo.path` is stored absolute — a child repo is not
  required to live under `AutopilotProject.base_dir`.

### Feature: Migration

- **REQ-04**: On upgrade, auto-create one `ProjectRepo` per existing
  `AutopilotProject`, with `path = base_dir` and `is_primary = True`.
- **REQ-05**: Migration must not modify `AutopilotProject.base_dir` or
  require backfilling `repo_id` on historical `Task`/`TicketCommit` rows.
- **REQ-06**: Any code path resolving `repo_id` that finds it unset falls
  back to the project's primary `ProjectRepo` (preserves existing
  single-repo behavior unchanged).

### Feature: Write/Read Scoping

- **REQ-07**: `WorktreeManager` is instantiable per `(project, repo)`
  pair, parameterized by a `ProjectRepo` path instead of hardcoded to
  `project.base_dir`.
- **REQ-08**: A task's worktree/branch/commit machinery is created
  against exactly its assigned `repo_id`; an agent never holds more than one
  worktree.
- **REQ-09**: Sibling repos are exposed to an agent as read-only paths at
  their canonical `ProjectRepo.path` (not worktree'd), documented in the
  task's launch context/prompt (see Feature Architect & Agent Prompts
  below).
- **REQ-10**: Commit-linking validates that a task's committed files fall
  under its assigned `repo_id`'s path before recording a `TicketCommit`
  (soft/code-level enforcement).
- **REQ-11** *(explicitly out of scope for v1)*: Hard filesystem
  enforcement (read-only bind mounts/chmod) of sibling-repo read access —
  revisit only if soft enforcement (REQ-10 + prompt instruction) proves
  insufficient in practice.

### Feature: Design/Doc Storage

- **REQ-12**: `docs/`-destination design uploads resolve to the primary
  `ProjectRepo`'s path, not the project's workspace root, so the feature
  stays git-tracked in a multi-repo project.
- **REQ-13**: `.hephaestus/designs/` staging continues to resolve at the
  workspace-root (`base_dir`) level, unaffected by repo count.

### Feature: Commit Resolution

- **REQ-14**: `_resolve_repo_path_for_commit` (`tickets_api.py:37`)
  accepts/resolves `repo_id` and returns the correct child repo's path
  instead of assuming one project-wide path.
- **REQ-15**: Every `git show`/`git diff` call site in
  `tickets_api.py` (`:1341,1368,1396`) operates against the resolved repo
  path for the commit in question.

### Feature: Recovery/Cleanup

- **REQ-16**: `policy.py`'s recovery commands, `worktree_integration.py`'s
  cleanup, and `terminator.py` accept/resolve `repo_id` to target the
  correct child repo's worktree, instead of assuming one project path.

### Feature: Feature Architect & Agent Prompts

- **REQ-17**: `AgentManager.get_project_context()` includes the
  project's repo list (label + path) whenever a project has more than one
  `ProjectRepo`.
- **REQ-18**: For an implementation-phase agent, injected context
  states plainly which repo is writable (its own) vs. read-only reference
  (siblings).
- **REQ-19**: `feature_architect_system_prompt` instructs the
  architect, as a hard rule, that every `Feature` it creates must be bound
  to exactly one repo — e.g. an API change and its UI consumer are two
  features (one per repo), never one feature spanning both.
- **REQ-20**: `feature_architect_system_prompt` instructs the
  architect to express cross-repo ordering via the existing
  `Feature.depends_on`/`execution` mechanism, not a free-text note in a
  feature's description.
- **REQ-21**: For single-repo projects, `get_project_context()` emits
  no additional text — no prompt/behavior change for the existing common
  case.

### Feature: Frontend

- **REQ-22**: Multi-repo projects (`ProjectRepo` count > 1) are detected
  and handled by existing views using data the backend already returns — no
  bespoke new "multi-repo mode" UI.
- **REQ-23**: `GitDiffModal`/commit views display which repo a commit
  belongs to, sourced from `repo_id`/label already returned by the resolved
  commit endpoint (REQ-14).
- **REQ-24**: Project-settings UI gets a minimal addition to add/label
  child repos on a project — the one new UI surface that can't be inferred
  from existing data.
- **REQ-25**: Task/ticket repo assignment is not exposed as a user-facing
  picker — assignment is the feature architect's responsibility
  (REQ-19).

### Feature: Cross-Design Parallelism *(deferred, out of scope for v1)*

- **REQ-26**: `pick_next_design` returns the next design per repo
  with no in-flight work, instead of one next design per project.
- **REQ-27**: `run_continuous_pipeline` runs one loop per
  `(project, repo)` with in-flight work, instead of one per project, bounded
  by a new concurrency cap alongside `max_concurrent_projects`.
- Both deferred per "Future work" below — revisit only if real usage shows
  people uploading independent per-repo design docs often enough to justify
  it; the feature-level mechanism (REQ-20) already covers the common
  case of one design spanning both repos.

## Future work: cross-design parallelism

Narrowed significantly from an earlier pass of this doc, which assumed no
feature-level parallelism existed and so put the whole "run backend and
frontend work concurrently" problem here. It doesn't belong here anymore —
see "Existing cross-feature dependency system" above: within one design,
independent (or `depends_on`-ordered) features across repos already run
concurrently via `run_feature_pipelines`'s `ThreadPoolExecutor`, using
existing machinery, as part of v1.

What's left as genuinely future/out-of-scope is narrower: `pick_next_design`
still picks one active design per project, so two separate, unrelated
**design docs** (not features within one design) each targeting a different
repo still run one after another. This only matters when someone
deliberately uploads two independent design docs rather than letting the
feature architect decompose one design into repo-scoped features — a much
smaller and less common case than what this section originally described.
If it turns out to matter in practice: `pick_next_design` would need to
return the next design *per repo with no in-flight work* instead of one
next design per project, and `run_continuous_pipeline` would need one loop
per (project, repo) with in-flight work instead of one per project, bounded
by a new cap alongside `max_concurrent_projects`. No UI work — same
"detect and operate normally" principle as the rest of this doc, reusing
whatever already renders concurrent pipeline state.

Revisit only if real usage shows people uploading independent per-repo
design docs often enough to justify it — the feature-level mechanism
already covers the common case.
