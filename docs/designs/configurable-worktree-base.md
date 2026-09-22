# Design: Configurable Worktree Base (Current Branch vs. Main)

## Status
Proposed — awaiting review.

## Problem

New agent worktrees always branch from the configured `git.base_branch` (default
`main`), resolved by `WorktreeManager._resolve_base_commit()` (commit `9bbc499f`,
which closed a bug where worktrees leaked the repo's arbitrary current HEAD).
"Always main" is right for full autopilot, but too rigid for a developer running a
single design by hand while on an in-progress feature branch — they want the agent to
build on *that* branch, not main.

## Scope (deliberately minimal)

After iterating, the feature reduces to **one binary choice on the Design Spec
modal**:

- **Base branch** (default): branch new work from a freshly-fetched
  `git.base_branch` (`main`). Today's behavior.
- **Current branch**: branch new work from the managed repo's primary checkout's
  currently-checked-out branch (resolved live at launch).

Everything else considered during design was cut:
- **Always fetch** — no fetch/no-fetch toggle. New work always branches from
  freshly-fetched code.
- **No project-level setting** — with fetch always on and the branch always `main`,
  a project default would have exactly one possible value. If a project genuinely
  needs autopilot to run off a different branch, the right lever is the global
  `git.base_branch` config (which also moves the merge target with it, keeping the
  two consistent) — not a per-project override that would desync base from merge
  target. No `AutopilotProject` columns, no settings-UI change.
- **No branch-name field** — "current branch" covers the only coherent
  base-on-a-specific-branch case (foreground manual run); an arbitrary base branch
  differing from the merge target is a footgun by construction.

## Decisions (locked)

- **Always fetch** before resolving the base.
- **Current branch is manual-only.** Only the Design Spec modal can request it. Full
  autopilot (the continuous queue loop + filesystem auto-scanned designs) always uses
  base branch + fetch. Enforced structurally (below), not by convention.
- **Merge target is always `main`** (`git.base_branch`) regardless of base. Base
  selection changes only where work *starts*, never where it *lands*. `merge_to_main`
  is unchanged. A `current`-based manual run still merges into main — the developer's
  explicit foreground choice.
- **"Current branch" = the primary checkout's current branch resolved at launch
  time** (not captured at enqueue). Simple boolean; if the developer moves the
  checkout between enqueue and pickup, "current" follows the checkout. Acceptable
  because this is a foreground, run-it-now action where that gap is near-zero.
- **Current resolves to a live branch ref, never a detached sha** — no detached HEAD,
  no frozen snapshot that drifts behind merged code.

## Data model

A single nullable boolean on `AutopilotDesign`:

```
# src/core/database.py :: AutopilotDesign
git_base_use_current = Column(Boolean, nullable=True)  # NULL/False => base branch + fetch; True => current checkout branch
```

- `NULL`/`False` → base branch (`git.base_branch`) + fetch. Existing rows read `NULL`,
  so no backfill and no behavior change.
- `True` → resolve the primary checkout's current branch at launch.

Added via `schema_migrations.py` (ALTER TABLE ADD COLUMN, the idempotent pattern
already used for `cli_model`/fallback columns).

**No `AutopilotProject` columns. No `git_base_branch` column. No `system_settings`
key.**

### Why manual-only is structural

Four sites create `AutopilotDesign` rows; only `add_project_design` (the modal) may
set `git_base_use_current=True`:

- `add_project_design` (design_file_routes.py) — the modal. **Only** setter of `True`.
- `_sync_project_designs` (project_routes.py) — filesystem auto-scan.
- `queue_routes.py` upload.
- `control_routes.py` Spec Kit selection.

The three non-modal sites leave the column unset (`NULL` → base+fetch). So a design
that full autopilot auto-discovers can never be `current`; only a human explicitly
enqueuing via the modal can. A test asserts each non-modal site leaves it `NULL`.

## Resolver changes

`_resolve_base_commit` gains one keyword param, defaulted so every existing caller is
unchanged:

```python
def _resolve_base_commit(self, *, use_current_branch: bool = False) -> str:
    if use_current_branch:
        # The primary checkout's current branch as a LIVE ref, not a
        # detached sha: resolve its name to a commit. If the repo is
        # itself already detached, fall back to base+fetch rather than
        # propagate detachment into new work.
        try:
            current_branch = self.main_repo.active_branch.name
        except TypeError:
            return self._resolve_base_commit(use_current_branch=False)
        return self.main_repo.git.rev_parse("--verify", f"{current_branch}^{{commit}}")
    # ---- unchanged base-branch path (always fetch) ----
    #   fetch origin/<base_branch> (bounded, offline-safe), prefer remote
    #   ref, fall back to local, raise if unresolvable (never HEAD).
```

- `use_current_branch` defaults to `False`, so the three `create_agent_worktree`
  internal sites and `_get_parent_commit` need **no change** — they get base+fetch.
- `current` is the only new path; it resolves a live branch ref and falls back to
  base+fetch on a detached repo. The RuntimeError guard on the base path stays.
- The `fetch`/`no_fetch` distinction from earlier drafts is gone — the base path
  always fetches.

The design-execution worktree creators that must call this with the design's flag are
`_setup_shared_design_worktree` and `_create_integration_worktree` (see below). The
latter also gains a start-point it currently lacks (closing a leftover current-HEAD
bug), regardless of the flag's value.

### Which worktree-creation site actually carries the work

There are **two** worktree creators in the design-execution path, and it matters
which one the base flag reaches:

- `_setup_shared_design_worktree` (pipeline.py) creates the `feature/<design>`
  branch used for the shared design worktree.
- `_create_integration_worktree` (worktree_integration.py) creates the **per-feature
  branch** (`feature/<design_id[:8]>/<feature_key>`, and
  `feature_architect/<design_id>` for Phase 0) that the workflow's phases actually
  commit to. Called from **both** `run_phase0` (pipeline.py ~1257) and
  `_run_one_feature` (~2202).

Both must resolve the base through `_resolve_base_commit(use_current_branch=...)`.
Today **both branch with no start-point** (`git.branch(branch)`), i.e. off the repo's
current HEAD — `_create_integration_worktree` is a *third* instance of the same
current-HEAD bug that commit `9bbc499f` fixed in `create_agent_worktree` and
`_setup_shared_design_worktree` but did NOT cover here. This design fixes it too:

```python
# _create_integration_worktree — currently `git.branch(branch)` (no start-point)
use_current = _design_uses_current_branch(design_id)   # already queries AutopilotDesign here (review_mode)
base_commit = wt_mgr._resolve_base_commit(use_current_branch=use_current)
wt_mgr.main_repo.git.branch(branch, base_commit)   # start-point added
```

`_create_integration_worktree` **already** queries `AutopilotDesign` by `design_id`
(for `review_mode`), so reading the base flag there is free — no launch_params
threading needed for this site at all; it reads the DB directly.

`_setup_shared_design_worktree` similarly resolves via the flag; it can read
launch_params (populated below) or re-query by `db_id` for symmetry with
`_create_integration_worktree`. Re-query is preferred so there's one loading path.

#### Loading the flag — read by `db_id`, not via `DesignEntry`

`DesignEntry` (state.py) is a fixed dataclass; new `AutopilotDesign` columns don't
appear on it. So the flag is read by `db_id`:
- `_create_integration_worktree` already has `design_id` and an open session — extend
  its existing `AutopilotDesign` query.
- `_setup_shared_design_worktree` / the launch_params sites resolve it via a tiny
  `_design_uses_current_branch(db_id) -> bool` helper (`run_single_design` already
  re-queries `AutopilotDesign` by `db_id` for `workflow_type`, same pattern).

One short-lived DB read per creation; negligible.

**Bugfix designs need no extra site.** `run_bugfix_single_feature` skips Phase 0 and
creates no worktree itself; a bugfix design flows through `_run_one_feature` →
`_create_integration_worktree` (dispatched to the "bugfix" workflow definition). The
flag is read by `db_id` there, independent of workflow type — so the same change
covers both the Design Spec (feature) and Bug Spec (bugfix) modals.

## API changes

Extend the `add_project_design` payload (`design_file_routes.py`, and
`apiService.addAutopilotProjectDesign` in `frontend/src/services/api.ts`) with an
optional `git_base_use_current: bool`, persisted onto the new column. This is the
only endpoint that accepts it. No project-route (`ProjectUpdate`/`ProjectItem`)
changes.

## UI changes

### Spec modals (`LoadDesignModal.tsx`) — the only surface
"Design Spec" (feature) and "Bug Spec" (bugfix) are the **same** `LoadDesignModal`
component rendered with `workflowType='feature'` vs `'bugfix'`. Add a single
toggle/radio — **Base branch (main)** [default] vs **Current branch** — rendered for
**both** workflow types (i.e. whenever `workflowType` is set), included in the
`addMutation` payload as `git_base_use_current`. A short helper line clarifies
"Current branch" bases work on the repo's checked-out branch (for building on
in-progress work) and is intended for manual runs. Because the control lives in the
shared component gated on `workflowType`, the feature and bugfix spec modals both get
it from one change; the plain "Load Design" entry point (`workflowType` unset) does
not render it and continues to enqueue base+fetch designs.

### Project settings (Autopilot page) — no change
Deliberately nothing. Full autopilot always uses base+fetch; there is no per-project
knob.

## Backward compatibility

- One new nullable column, `NULL` = existing behavior. No backfill.
- `_resolve_base_commit`'s new param defaults to today's behavior; all existing
  callers unchanged.
- A design with no selection (every auto-discovered design, every pre-existing row)
  behaves exactly as main+fetch does now.

## Testing

- **Resolver** (`test_worktree_manager.py`): `use_current_branch=False` = base+fetch
  (existing); `True` resolves the checked-out branch as a live ref (on an unrelated
  branch → that branch's tip, not a detached sha); `True` on an already-detached repo
  falls back to base+fetch; unresolvable base still raises.
- **Both worktree creators**: `_setup_shared_design_worktree` AND
  `_create_integration_worktree` (Phase 0 branch + per-feature branch) resolve the
  base through `_resolve_base_commit`; a `current` design has BOTH created off the
  checked-out branch, a `base` design off main+fetch. Includes a regression test that
  `_create_integration_worktree` no longer branches off the repo's current HEAD when
  the repo is parked on an unrelated branch (the pre-existing bug this closes).
- **Manual-only enforcement**: each non-modal `AutopilotDesign` creation site
  (`_sync_project_designs`, queue upload, Spec Kit) leaves the column `NULL`.
- **Merge target**: a `current`-based worktree still merges into `git.base_branch`
  (unchanged `merge_to_main`).
- **Migration**: ADD COLUMN idempotent; pre-existing rows read `NULL`.
- **API**: design POST round-trips `git_base_use_current`.
- **Frontend**: `npx tsc --noEmit`; toggle defaults to base branch.

## Risks / notes

- **Pre-existing current-HEAD bug in `_create_integration_worktree`** (per-feature
  branch created with no start-point) is closed by this change. It means "always
  branch from main" was only partially true before: the per-feature integration
  branches were still HEAD-based even after commit `9bbc499f`.
- **Resume reuses the existing branch**, so the base (including `current`) is applied
  at first creation only; a resumed workflow keeps its original base regardless of a
  later flag change. Correct for a resume (continue the same line of work), noted so
  it isn't mistaken for a bug.
- **`current` is resolved live at launch** (decision B): if the developer moves the
  primary checkout between enqueue and pickup, `current` follows the checkout.
  Acceptable for a foreground run-it-now action.

## Out of scope

- Fetch/no-fetch choice (always fetch).
- Project-level base configuration (use global `git.base_branch`).
- Arbitrary base-branch selection (only current-vs-main).
- Changing the merge target based on base (always main).
- Capturing the branch at enqueue (resolved live at launch, per decision).

## Open questions

None.
