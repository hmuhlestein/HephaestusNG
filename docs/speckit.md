# Spec Kit Support

Autopilot can build directly from a [GitHub Spec Kit](https://github.com/github/spec-kit)
`specs/<NNN>-<name>/` feature directory instead of a hand-written design
`.md` file. Spec Kit's own CLI (`specify init`, `/speckit.specify`,
`/speckit.plan`, `/speckit.tasks`, etc.) authors those directories inside
your project; **Hephaestus never creates, edits, or runs Spec Kit itself —
it only reads what's already on disk.** If a project has no `specs/`
directory, Spec Kit support is simply inactive for it; nothing about the
normal design-queue workflow (`heph autopilot add`) changes.

This exists so a team already using Spec Kit for spec/plan/task authoring
doesn't have to hand-copy that work into a separate Hephaestus design
document — Autopilot detects the directory Spec Kit already wrote and
builds from it directly.

---

## Directory layout Hephaestus expects

```
<repo>/specs/
├── 001-checkout-flow/
│   ├── spec.md            ← required — everything else is optional
│   ├── plan.md
│   ├── tasks.md
│   ├── data-model.md
│   ├── research.md
│   ├── quickstart.md
│   ├── contracts/
│   └── checklists/
└── 002-user-profile/
    └── spec.md
```

A feature directory must match `^(\d+)-(.+)$` (e.g. `001-checkout-flow`)
and contain a `spec.md` — that's the only hard requirement
(`src/autopilot/orchestrator/speckit.py::_scan_one_repo`). `plan.md` and
`tasks.md` are tracked individually because readiness checks and the
auto-scan gate (below) key off their presence; `data-model.md`,
`research.md`, `quickstart.md`, `contracts/`, `checklists/` are recorded as
"extra files" but never required.

**Security note:** a top-level entry under `specs/` that is itself a
symlink, or whose resolved real path escapes `specs/`, is skipped and
logged — not treated as a candidate feature. This exists because an
earlier version would enumerate `specs/999-x -> /etc` as a real feature and
copy it wholesale into an agent's worktree (`_scan_one_repo`, ticket
`84a86e68`).

---

## Detection and resolution (`src/autopilot/orchestrator/speckit.py`)

This is the module the CLI, the `/speckit/*` API routes, and `POST /start`
all go through — one implementation, several callers.

`discover_speckit_features(db, project_id, project_base_dir)` scans:

1. `specs/` under every registered `ProjectRepo` for the project (see
   [multi-repo-projects.md](multi-repo-projects.md)), and
2. `project_base_dir/specs/` as well, **but only if `project_base_dir`
   isn't already one of those registered repo paths** — this covers a
   `specify init` run at a multi-repo workspace root rather than inside one
   child repo, without double-counting a single-repo project's own `specs/`.

`discover_speckit_features_unregistered(project_base_dir)` is the fallback
for a project with no `AutopilotProject`/`ProjectRepo` rows yet — it scans
only `project_base_dir/specs/`. Both never raise on a missing/unreadable
`specs/` dir; they return `[]` for that location and keep going.

Results are sorted by `(repo_label, number)`.

### Selecting one feature — `resolve_feature_selection`

Given the detected list plus optional `feature_arg` (`--feature`) and
`repo_arg` (`--repo`):

| Situation | Result |
|---|---|
| `feature_arg` given, matches nothing | `SpecKitSelectionError("NOT_FOUND", ...)` |
| `feature_arg` matches features in >1 repo, no `--repo` | `SpecKitSelectionError("AMBIGUOUS_REPO", ...)` |
| `feature_arg` + `--repo`, nothing matches that repo | `SpecKitSelectionError("NOT_FOUND", ...)` |
| `feature_arg` given, unique match (optionally narrowed by `--repo`) | that feature |
| No `feature_arg`, zero features found | `SpecKitSelectionError("NOT_FOUND", ...)` |
| No `feature_arg`, ≥2 features found | `SpecKitSelectionError("MULTIPLE_FEATURES", ...)` (candidates list, plus `spec.md` as a candidate if one also exists) |
| No `feature_arg`, exactly 1 feature found, but a plain `spec.md` design also exists | `SpecKitSelectionError("BOTH_INPUTS_PRESENT", ...)` |
| No `feature_arg`, exactly 1 feature found, no competing `spec.md` | that feature |

`--feature` matches by full name (`001-checkout-flow`) first; if nothing
matches the full name it falls back to an **exact** zero-padded numeric
match on `001` — never a partial/prefix match, so `--feature 001` cannot
accidentally match `0012-something` (`_match_feature_arg`).

Every ambiguous/not-found case raises — selection never silently guesses.
`SpecKitSelectionError.code` is one of `NOT_FOUND` / `MULTIPLE_FEATURES` /
`AMBIGUOUS_REPO` / `BOTH_INPUTS_PRESENT`; `.candidates` is a list of labeled
options for the caller to print or return.

### Readiness (voluntary, non-blocking)

`check_feature_readiness(feature)` parses `spec.md` for
`[NEEDS CLARIFICATION: ...]` markers and checks whether `plan.md`/`tasks.md`
exist. It never blocks anything — a feature with missing files or open
clarification markers can still be started; this is purely informational.

---

## CLI (`src/cli/commands/autopilot.py`)

```bash
# Pin one Spec Kit feature for the next pipeline run
heph autopilot start --project-path ~/my-project --feature 001-checkout-flow

# --feature also accepts the bare number
heph autopilot start --project-path ~/my-project --feature 001

# Disambiguate when the same number/name exists in more than one repo
heph autopilot start --project-path ~/my-project --feature 001 --repo backend

# Check readiness (plan.md present? any [NEEDS CLARIFICATION] left?) without
# starting anything. Omit --feature to check every detected feature.
heph autopilot check --project-path ~/my-project --feature 001-checkout-flow
```

`heph autopilot start`'s `--feature`/`--repo` are only consulted when
`--feature` is actually passed — an existing project with its own already-
queued `.md` designs keeps working exactly as before if you never touch
these flags (`start_pipeline`'s own docstring, `control_routes.py`).

When `--feature` resolves to exactly one feature, the backend
(`_resolve_and_enqueue_speckit_feature`) creates or updates an
`AutopilotDesign` row for it — `source_dir` set to the feature's directory,
`repo_id` set to its resolving `ProjectRepo` (or `None` for a single-repo
project) — and gives it the top ordinal so the pipeline's normal
`pick_next_design()` polling picks it up on its very next cycle. It reuses
the existing continuous design queue; there is no separate speckit-specific
launch path.

A selection error surfaces as HTTP 422 with `{code, message, candidates}`;
the CLI (`_print_speckit_selection_error`) renders it as:

```
Error: <message>
Available options:
  - 001-checkout-flow
  - 002-user-profile
```

---

## API routes

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/autopilot/speckit/features?project_path=` | Path-based; works even for an unregistered project (falls back to `discover_speckit_features_unregistered`). |
| `GET` | `/api/autopilot/speckit/check?project_path=&feature=&repo=` | Same fallback. `feature` optional (omit to check all); response includes `multi_repo_scan: bool` — `false` means the project isn't registered yet, so only `project_path/specs/` was scanned. |
| `GET` | `/api/autopilot/projects/{project_id}/speckit/features` | Project-id-scoped variant for the dashboard (which has an id, not a raw path). |
| `GET` | `/api/autopilot/projects/{project_id}/speckit/check?number=&repo_label=` | Project-id-scoped variant. `number` optional. |
| `POST` | `/api/autopilot/start?project_path=&feature=&repo=` | Same start endpoint the CLI's `heph autopilot start` calls; `feature`/`repo` are the same selectors. |

**`repoLabel: null` means "the project's primary repo," not "any repo."**
Both project-id-scoped routes null out `repoLabel` for whichever repo is
`is_primary=True` — even once a project has multiple registered repos and
every feature technically has a real `repo_label` — because the frontend's
file-browse endpoints only ever reach the primary repo, so `null` is what
the picker actually needs to mean "selectable without `--repo`." A caller
passing that `null` back into `.../speckit/check` must be resolved the same
way (`f.repo_id == primary_repo_id`), not matched against the real,
unmasked `repo_label` — an earlier version of this route did the latter and
404'd every primary-repo feature the moment any child repo was registered
(see `_discover_features_or_404`'s docstring, commit `2edde877`).

The two path-based routes (`/speckit/features`, `/speckit/check`) return
unmasked `repo_label` values — the masking is specific to the project-id
routes' primary-repo semantics described above.

Response fields for a feature entry: `number`, `slug`, `repoLabel` (or
`repo_label` on the path-based routes), `hasPlan`/`hasTasks` (list route) or
`needs_clarification`/`missing_files` (check route).

---

## Frontend picker

`frontend/src/components/autopilot/SpecKitFeaturePicker.tsx`, used from
`LoadDesignModal.tsx`. Renders nothing if the project has zero detected
Spec Kit features. Otherwise:

- A `<select>` listing every detected feature as `NNN-slug`, suffixed with
  `(no plan.md)` when `hasPlan` is false. Grouped into `<optgroup>`s by repo
  label when the project has features in more than one repo (or a single
  non-null repo label); otherwise a flat list.
- Selecting an option calls the `onSelect(feature)` prop.
- A **"Check readiness"** button per selection, fetched only on demand
  (never on list load) via the project-id-scoped `/speckit/check` route.
  Shows each `missingFiles` entry and each `needsClarification` entry
  inline; a failed request shows "Failed to check readiness."

---

## Automatic scanning (independent of `--feature`)

Separate from the manual `--feature`/`--repo` CLI/API path above, a project
can opt into **automatic** Spec Kit enqueueing:

```
PATCH /api/autopilot/projects/{project_id}/speckit-auto-scan
Body: {"speckit_auto_scan_enabled": true}
```

(`AutopilotProject.speckit_auto_scan_enabled`, a `Boolean` column, default
`False`; toggled in the UI via `SpeckitAutoScanToggle.tsx` on the Autopilot
page.)

When enabled, `_sync_speckit_designs` (`src/autopilot/orchestrator/queue.py`)
runs on every design-queue scan and automatically creates/updates an
`AutopilotDesign` row for every detected feature that **already has a
`plan.md`** (`has_plan=True` — a feature with no plan is not considered
ready to build, and is skipped every scan until it gets one). Dedup key is
`(project_id, spec_key)`, where `spec_key` is derived from
`directory_spec_key(f"{number}-{slug}", repo_label)` — not a filename, since
a Spec Kit feature has none. An existing `pending` row's content hash is
refreshed if `spec.md` changed since it was last queued; a row already past
`pending` is left alone. Detection failures for one feature are logged and
skipped, never allowed to abort the scan or (per an adversarial-review fix)
bypass the per-project budget gate for the rest of the pass.

**Note:** this auto-scan path calls `find_speckit_features` from
`src/core/speckit_detection.py` — a second, independently-maintained
detection implementation from the `discover_speckit_features` used by the
manual `--feature` CLI/API path documented above. The two use very similar
scanning logic (same directory convention, same per-location error
isolation, same symlink-safety posture) but are not the same code path.
This is a real duplication in the codebase as of this writing, not a
documentation error — worth a maintainer's attention, since a future change
to detection semantics (e.g. which optional files count, dedup rules) would
need to land in both modules to stay consistent.

---

## Precedence and coexistence with hand-written designs

A project can have both a `specs/` directory and manually-queued `.md`
designs (`heph autopilot add`) at the same time — nothing here disables the
normal queue. `resolve_feature_selection`'s `BOTH_INPUTS_PRESENT` case only
fires for the *zero-argument* selection path (no `--feature` given, exactly
one Spec Kit feature detected, and a competing `spec.md` also present) — it
forces an explicit choice rather than silently preferring one. Passing
`--feature` explicitly, or having more than one Spec Kit feature, sidesteps
that specific ambiguity check entirely.

---

## Related

- [Autopilot Pipeline](autopilot.md) — the 14-phase pipeline a selected
  Spec Kit feature (or hand-written design) runs through once queued.
- [Multi-Repo Projects](multi-repo-projects.md) — how Spec Kit detection
  scans every child repo's own `specs/` directory, and how `--repo`/
  `repo_label` map onto `ProjectRepo` rows.
