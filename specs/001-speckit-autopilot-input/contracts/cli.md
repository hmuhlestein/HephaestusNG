# CLI Contract: Spec Kit-Aware Autopilot Input

Both commands live in the existing `src/cli/commands/autopilot.py`.

## `heph autopilot start` (existing command, new flag)

```text
heph autopilot start --project-path <path> [--feature <NNN-name>] [--design-doc <path>]
```

| Flag | Required | Behavior |
|---|---|---|
| `--feature <NNN-name>` | Conditional | Selects a specific `specs/<NNN>-<name>/` directory as the build's Spec Kit input. **Required** when more than one Spec Kit feature directory exists (FR-006), or when both `design.md` and a `specs/` directory exist and Spec Kit input is the intended source (FR-010). |
| `--design-doc <path>` | Conditional | Explicit path to a hand-written design document. **Required** instead of `--feature` when both `design.md` and a `specs/` directory exist and the raw design doc is the intended source (FR-010). Existing behavior when no `specs/` directory exists at all — unchanged (FR-005). |

**Resolution** (evaluated in this order):

1. Neither `specs/` nor `design.md` present → existing "no design input" error, unchanged.
2. Only `design.md` present (no `specs/`) → existing behavior, unchanged (FR-005).
3. Only `specs/` present, exactly one feature directory, no `--feature`/`--design-doc` given → that feature is auto-selected (FR-002).
4. Only `specs/` present, more than one feature directory, no `--feature` → **error**, lists the available `<NNN>-<name>` directories (FR-006).
5. Both `design.md` and `specs/` present → **error** unless exactly one of `--feature`/`--design-doc` is given; that explicit choice wins (FR-010).

## `heph autopilot check` (new command)

```text
heph autopilot check --project-path <path> --feature <NNN-name>
```

Read-only. Reports a `ReadinessCheckResult` (see data-model.md) to stdout: any unresolved `[NEEDS CLARIFICATION: ...]` markers in `spec.md`, and whether `plan.md` is present. Exit code reflects `ready` (0 if ready, non-zero if not) for scriptability, but **never** affects `heph autopilot start` — running or not running this command changes nothing about whether `start` will work (FR-011).
