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

## Gaps found in this pass (open questions / follow-on scope)

1. **No free parallelism.** Multi-repo support as designed here does *not*
   give you concurrent backend+frontend execution — the pipeline loop is
   architected one-per-project, serially draining one design queue
   (`pick_next_design`). A "backend" design and a "frontend" design under
   the same project would still run one after another, not in parallel,
   unless the pipeline loop itself becomes repo-aware and is explicitly
   allowed to run N concurrent sub-loops within one active project. **Recommend
   scoping v1 to sequential multi-repo (correct repo-scoped commits/worktrees/
   reads) and treating parallel cross-repo execution as an explicit, separate
   follow-on** — don't let the feature's name imply parallelism it doesn't have.
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
  structure. Per "Future work" below, today those two features run
  independently/sequentially through the same project queue with no
  built-in ordering guarantee — the architect should note the dependency
  (e.g. "frontend feature consumes the backend feature's new endpoint") in
  each feature's description so a human or the design queue's ordering can
  account for it, since there's no automatic cross-feature dependency
  mechanism yet.

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

## Future work: parallel cross-repo execution

Explicitly out of scope for v1 (see gap #1 above), noted here so it isn't
lost. Sequential multi-repo (this doc's main design) gets correct
repo-scoped commits/worktrees/reads, but a backend design and a frontend
design under the same project still run one after another through the same
project-level queue. Making them run concurrently is a materially bigger
change, isolated to the pipeline/orchestrator layer — the data model above
(`ProjectRepo`, `repo_id` on `Task`/`TicketCommit`/etc.) should not need to
change to support it later.

What it would take:

- **Queue picking becomes repo-aware.** `pick_next_design` currently returns
  one next design per project. It would need to return (or be called once
  per) the next design *per repo that has no in-flight work*, so a backend
  design and a frontend design can both be "active" simultaneously under one
  project.
- **`run_continuous_pipeline` goes from one loop per project to one loop per
  (project, repo)** with in-flight work — mirroring the existing "one loop
  per project" pattern (`AutopilotServiceRegistry`) one level down. The
  project-level `is_active`/`max_concurrent_projects` gate stays as-is; a new
  gate would be needed to bound concurrent repo-loops within one active
  project (analogous cap, e.g. `max_concurrent_repos_per_project`).
- **Phase-level active-agent check stays correct almost for free.** The
  existing gate is already scoped by `Task.phase_id`, not by project — two
  phases in two different repo-scoped workflows won't block each other. The
  part that needs work is upstream of that: getting two workflows to be
  "active" under one project at the same time in the first place.
- **Cross-repo coordination points need explicit handling.** If a feature
  genuinely spans both repos (e.g. an API contract change), something has to
  decide whether the two repo-scoped workflows run fully independently
  (simplest, but no ordering guarantee — frontend work could start before
  the matching backend endpoint exists) or whether one can declare a
  dependency on the other's completion. Recommend starting independent-only
  and letting the user manually sequence dependent work via design-queue
  ordering, rather than building a cross-repo dependency graph up front.
- **UI**: same "detect and operate normally" principle as v1 — no dedicated
  new surface. The project view already needs to represent N concurrent
  *projects*; representing N concurrent repo-loops within one project is the
  same pattern one level down, reusing whatever component already renders
  concurrent pipeline state rather than a bespoke multi-pipeline view.

This should be scoped as its own design pass once sequential multi-repo is
live and in real use — the actual pain points (how often do designs
genuinely need cross-repo ordering vs. run fine independently) will be much
clearer from real usage than from speculation now.
