# CLI Contract: Spec Kit-Aware Autopilot Input

Both commands live in the existing `src/cli/commands/autopilot.py`.

## `heph autopilot start` (existing command, new flag)

```text
heph autopilot start --project-path <path> [--feature <NNN-name>|<NNN>] [--repo <label>] [--design-doc <path>]
```

| Flag | Required | Behavior |
|---|---|---|
| `--feature <NNN-name>` | Conditional | Selects a specific `specs/<NNN>-<name>/` directory as the build's Spec Kit input. Accepts the full directory name (`001-checkout-flow`) or the bare number alone (`001`) when unambiguous (FR-021). **Required** when more than one Spec Kit feature directory exists (FR-006), or when both `design.md` and a `specs/` directory exist and Spec Kit input is the intended source (FR-010). |
| `--repo <label>` | Conditional | In a multi-repo project, disambiguates which child repo's `specs/` to search when `--feature` alone would match a directory in more than one repo (FR-022/FR-023). `<label>` matches `ProjectRepo.label` (e.g. `backend`, `frontend`). Not needed in a single-repo project — there is only one repo to search. |
| `--design-doc <path>` | Conditional | Explicit path to a hand-written design document. **Required** instead of `--feature` when both `design.md` and a `specs/` directory exist and the raw design doc is the intended source (FR-010). Existing behavior when no `specs/` directory exists at all — unchanged (FR-005). |

**Resolution** (evaluated in this order):

1. Neither `specs/` nor `design.md` present, in any searched repo → existing "no design input" error, unchanged.
2. Only `design.md` present (no `specs/` anywhere) → existing behavior, unchanged (FR-005).
3. Only `specs/` present, exactly one feature directory across all searched repos, no `--feature`/`--design-doc` given → that feature is auto-selected (FR-002).
4. Only `specs/` present, more than one feature directory (whether within one repo or across repos), no `--feature` → **error**, lists the available `<NNN>-<name>` directories, annotated with repo label in a multi-repo project (FR-006).
5. `--feature <NNN-name>`/`<NNN>` given but it matches a directory in more than one repo and no `--repo` was given → **error**, lists the ambiguous matches by repo, requires `--repo` (FR-023).
6. Both `design.md` and `specs/` present → **error** unless exactly one of `--feature`/`--design-doc` is given; that explicit choice wins (FR-010).

In a single-repo project (the common case), `--repo` is never relevant — every rule above still applies, just scoped to the one repo there is.

## `heph autopilot check` (new command)

```text
heph autopilot check --project-path <path> --feature <NNN-name>|<NNN> [--repo <label>]
```

Read-only. Reports a `ReadinessCheckResult` (see data-model.md) to stdout: any unresolved `[NEEDS CLARIFICATION: ...]` markers in `spec.md`, and whether `plan.md` is present. Exit code reflects `ready` (0 if ready, non-zero if not) for scriptability, but **never** affects `heph autopilot start` — running or not running this command changes nothing about whether `start` will work (FR-011).
