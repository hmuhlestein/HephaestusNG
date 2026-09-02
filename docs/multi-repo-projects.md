# Multi-Repo Projects

An `AutopilotProject` isn't limited to a single directory. It can register
additional **child repos** — separately labeled, independently pathed git
repositories — alongside its primary one, and a `Feature` produced by the
Feature Architect can be bound to one specific child repo. That feature's
entire pipeline (worktree, branch, commit, merge) then runs against that
repo, not the project's primary.

This exists for workspaces that are genuinely more than one repository —
e.g. a separate frontend and backend repo checked out as siblings — where a
feature scoped to one of them shouldn't need its own separately-registered
`AutopilotProject`.

---

## Data model

```
AutopilotProject
  base_dir            ← the project's own registered directory
  │
  └── ProjectRepo (0..N per project)
        id             "repo-<uuid>"
        project_id     FK → autopilot_projects.id (CASCADE)
        label          e.g. "backend", "frontend" — unique per project
        path           absolute filesystem path — not required to live
                        under base_dir
        is_primary     bool, default False
        created_at

AutopilotDesign.repo_id   → ProjectRepo.id, nullable
Feature.repo_id           → ProjectRepo.id, nullable
Task.repo_id              → ProjectRepo.id, nullable
[worktree table].repo_id  → ProjectRepo.id, nullable
[commit table].repo_id    → ProjectRepo.id, nullable
```

(`src/core/database.py`, `class ProjectRepo`, line 1194; `Feature.repo_id`
at line 1280.) Two `UniqueConstraint`s: `(project_id, path)` and
`(project_id, label)` — a repo's path and label are each unique within a
project, not globally. A partial unique index
(`uq_project_repos_one_primary`, applied by
`schema_migrations.migrate_project_repos_table`) enforces at most one
`is_primary=True` row per project at the database level, closing the race
that an in-process `asyncio.Lock` (`add_project_repo`, see below) only
narrows.

`repo_id = None` on any of the above means "the project's primary repo (or
`base_dir`, for a project with zero `ProjectRepo` rows at all)" — it is
never a wildcard meaning "any repo."

---

## Resolution — `src/core/repo_resolution.py`

This module is the single choke point every call site uses instead of its
own copy of "given a project and maybe a `repo_id`, what path" logic.

### `resolve_repo_path(db, project_id, repo_id) -> Path`

```
repo_id given   → that ProjectRepo's path
                  (raises RepoNotFoundError if it doesn't belong to
                   project_id — NEVER silently falls back to primary here,
                   because substituting a different repo's path would
                   point git operations at the wrong tree with no visible
                   error)

repo_id is None → the project's primary ProjectRepo (is_primary=True)
                  → if no primary row exists AND no ProjectRepo rows exist
                    at all (pre-migration edge case), falls back to
                    AutopilotProject.base_dir directly, logged at WARNING

project_id doesn't resolve → raises ValueError
```

### `get_project_repos(db, project_id) -> List[ProjectRepo]`

All of a project's repos, **primary first**, then alphabetical by label.
Returns `[]` for a malformed/missing `project_id` rather than raising — it
is the defensive, display-oriented variant (used for UI listing, prompt
injection); `resolve_repo_path` is the strict variant for anything that is
about to perform a filesystem/git write.

### `repo_id_for_path(db, project_id, file_path) -> Optional[str]`

Which registered repo (if any) a given absolute path falls under —
**longest-prefix match** against every `ProjectRepo.path` for the project.
Tie-break when more than one repo's path matches (only possible with
overlapping/nested repo paths): longest path wins, then primary wins, then
alphabetical by label. Returns `None` if no repo's path is a prefix of
`file_path`.

---

## `Feature.repo_id` — how a feature binds to a repo

Set once, at feature-record creation (`_create_feature_records`,
`src/autopilot/orchestrator/features.py`), from the Feature Architect's
`features.json` output for a multi-repo project:

1. If the feature's JSON entry carries an explicit `repo_label`, that wins
   — resolved directly to the matching `ProjectRepo.id`.
2. Otherwise, every path in the feature's `files` list is resolved to an
   absolute path and matched against `repo_id_for_path`. If every matched
   file lands in the same repo, that repo's id is used.
3. If the feature's `files` genuinely span more than one repo with no
   `repo_label` to disambiguate, `Feature.repo_id` is left `None` and a
   loud warning is logged — the code deliberately does **not**
   majority-vote a repo and silently drop the other repo's files from the
   feature's scope (that would only surface later as a confusing
   task-creation error once a task's working directory landed in the
   dropped repo). The Feature Architect's own prompt already forbids one
   feature spanning more than one repo; this is the enforcement backstop.
4. `repo_id` stays `None` for a single-repo project, or when repo
   inference is inconclusive — in every downstream use this falls back to
   the project's primary repo via `resolve_repo_path`.

From then on, `_run_one_feature` (`src/autopilot/orchestrator/pipeline.py`)
resolves `project_path = resolve_repo_path(db, project_id, feature.repo_id)`
**before** creating or reconnecting to that feature's worktree — so every
phase of that feature's pipeline (worktree creation, branching, the
Phase 13 git commit/PR/merge) operates against the resolved repo's path,
not the project's primary. If the feature is explicitly bound to a repo
(`repo_id` set) and resolution fails (`RepoNotFoundError`/`ValueError` —
e.g. the repo was removed), the feature is failed outright rather than
silently running against the wrong tree; if `repo_id` was never set,
resolution failure just logs a warning and falls through to the default.

The same `resolve_repo_path(db, design.project_id, feature.repo_id)` call
is used again by the stuck-workflow worktree-recovery path
(`worktree_integration.py`) so a recreated worktree lands in the same repo
the feature was originally bound to.

`AutopilotDesign.repo_id` (nullable, same FK) plays the equivalent role one
level up — it's set when a Spec Kit feature is selected from a specific
child repo (see [speckit.md](speckit.md)) or inferred via
`repo_id_for_path`, and is `None` for a single-repo project's ordinary
queued designs or a design that was never repo-scoped to begin with.

---

## API — `src/mcp/autopilot/project_repo_routes.py`

Only add and list exist in v1 — no update or delete route.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/autopilot/projects/{project_id}/repos` | Lists the project's `ProjectRepo` rows (`get_project_repos` — primary first). Requires `X-Agent-ID` auth (repo paths are filesystem-sensitive). |
| `POST` | `/api/autopilot/projects/{project_id}/repos` | Body: `{label, path}`. Adds one child repo. |

`POST` validates before writing anything:

- `path` must resolve to an existing directory containing `.git` (via
  `repo_resolution.git_repo_error`, the strict repo check — a child repo
  cannot use the multi-repo exemption or workspace-root allowance that
  `git_repo_error` grants a *project's* directory, since a child repo IS
  the git repository itself).
- The resolved path must be readable and writable
  (`os.access(..., os.R_OK | os.W_OK)`), else `403`.
- The first repo added to a project is automatically `is_primary=True`;
  every subsequent one is not. This check-then-insert is serialized by an
  in-process `asyncio.Lock` per project (`_get_project_lock`) to avoid a
  spurious `409` between two concurrent requests on the same process — the
  actual cross-process guarantee is the database's own
  `uq_project_repos_one_primary` partial unique index.
- A duplicate `(project_id, path)` or `(project_id, label)` returns `409`
  with a message naming which constraint collided.

---

## Worktree isolation — the no-nested-worktrees invariant

> **If `project_path` contains `.worktrees/`, use it directly — never
> create a worktree inside an existing worktree.**
> (`CLAUDE.md`'s `no-nested-worktrees` invariant.)

Enforced literally as `if ".worktrees/" in str(project_path):` at every
site that would otherwise create a new worktree from a project/design
path — `src/autopilot/orchestrator/pipeline.py` (design worktree
creation), `worktree_integration.py`, `feature_routes.py`,
`launch_pipeline.py`, `repair_service.py`, `design_file_routes.py`. When
the check hits, the existing worktree path is used directly instead:

```python
# FIX: If project_path is already a worktree (contains .worktrees/),
# use it directly as the design worktree. Don't create a nested
# worktree inside it — that would be destroyed when the parent
# worktree is cleaned up.
if ".worktrees/" in str(project_path):
    design_worktree_path = str(project_path)
```

This matters for multi-repo projects specifically because a child repo's
own path can itself already be a worktree of some other checkout — the
guard is what keeps that case from producing a worktree nested inside a
worktree, which would vanish the moment the outer one is cleaned up.

`WorktreeManager` (`src/core/worktree_manager.py`) itself takes an already-
resolved `repo_path` at construction — it has no `repo_id` parameter of its
own. Every caller resolves `repo_id` → filesystem path via
`resolve_repo_path` first, then hands `WorktreeManager` that concrete path;
multi-repo awareness lives entirely in the resolution layer described
above, not inside worktree creation itself.

---

## Fallback behavior summary

| Condition | Behavior |
|---|---|
| Project has zero `ProjectRepo` rows (pre-migration, or never registered) | `resolve_repo_path` falls back to `AutopilotProject.base_dir`, logged at WARNING |
| `repo_id=None` on a `Feature`/`AutopilotDesign`/etc. | Resolves to the project's primary `ProjectRepo` |
| `repo_id` set but doesn't belong to `project_id` | `RepoNotFoundError` — never silently substituted |
| A feature's `files` span >1 repo, no `repo_label` given | `Feature.repo_id` left `None`; loudly logged, not guessed |
| `project_path` already contains `.worktrees/` | Used directly; no nested worktree is created |

---

## Related

- [Autopilot Pipeline](autopilot.md) — worktree lifecycle, feature
  pipeline, and where `Feature`/`AutopilotDesign` fit in the broader data
  model.
- [Spec Kit Support](speckit.md) — how `--repo`/`repo_label` disambiguate a
  Spec Kit feature detected in one specific child repo's own `specs/`
  directory.
