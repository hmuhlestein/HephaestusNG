---
type: adversarial_review_result
feature_id: des-91c8-cost-collectors
verdict: PASS
blocker_count: 0
warning_count: 0
nit_count: 0
---

# Adversarial Review: CLI Cost Collectors (Pi + Claude Code) — Verification Pass

This run verifies the fixes for the findings from the prior adversarial
review run, per instructions (verify-only, not a from-scratch re-review).
Reviewed `git show 578cf9b -- scripts/install.sh`, the development phase's
fix commit responding to those findings.

## Prior BLOCKER 1 — build failures silently reported as success (pipe exit-status bug)

**Was:** `if (cd "$EXT_DEST_DIR" && npm install --silent 2>&1 | tail -3 && npm run build 2>&1 | tail -3); then` —
without `set -o pipefail`, this always tested `tail`'s exit status (always 0),
so npm failures were never detected.

**Now** (`scripts/install.sh:793`):
```bash
if EXT_BUILD_OUTPUT=$(cd "$EXT_DEST_DIR" && npm install --silent 2>&1 && npm run build 2>&1); then
    ok "Cost tracker extension installed"
else
    warn "Cost tracker extension build failed — real-time cost tracking disabled"
    warn "Cost data will still be collected via task-completion fallback"
    echo "$EXT_BUILD_OUTPUT" | tail -6
fi
```
No pipe inside the tested condition — the `if` now evaluates the exit status
of the `&&`-chained command substitution directly, which is the exit status
of whichever of `npm install`/`npm run build` actually ran last/failed.

**Verified by direct repro**, not just inspection:
```
$ bash -c 'set -e; if EXT_BUILD_OUTPUT=$(true && false); then echo TRUE; else echo FALSE; fi'
FALSE
```
Confirmed fixed.

## Prior BLOCKER 2 — unguarded `mkdir -p`/`cp -r` abort the whole script under `set -e` on permission/disk failure

**Was:** bare `mkdir -p "$EXT_DEST_DIR"` and `cp -r "$EXT_SRC_DIR"/* "$EXT_DEST_DIR/"` statements, unguarded, killed by `set -e` on any failure (verified in the prior run with a real read-only-filesystem repro).

**Now** (`scripts/install.sh:789-792`):
```bash
if ! rm -rf "$EXT_DEST_DIR" 2>/dev/null || ! mkdir -p "$EXT_DEST_DIR" 2>/dev/null || ! cp -r "$EXT_SRC_DIR"/* "$EXT_DEST_DIR/" 2>/dev/null; then
    warn "Could not write to $EXT_DEST_DIR — skipping cost tracker extension"
    warn "Cost data will still be collected via task-completion fallback"
else
    ...
fi
```
All three filesystem operations are now part of an `if` condition's `||`
chain — commands used as part of `if`/`&&`/`||` test lists are exempt from
`set -e` by shell semantics, so a failure anywhere in the chain now warns
and continues instead of aborting.

**Verified by direct repro:**
```
$ bash -c 'set -e; echo before; if ! mkdir -p /nonexistent_root_test_xyz/sub 2>/dev/null; then echo WARN; else echo OK; fi; echo after'
before
WARN
after
```
Confirmed fixed — script continues past the failure.

## Prior WARNING — stale files surviving `--update` (cp -r never deletes)

**Was:** `cp -r` only added/overwrote files, so a file removed from
`extensions/hephaestus-cost-tracker/src/` upstream would linger in
`$EXT_DEST_DIR` forever and still get compiled by `tsc` (whose `tsconfig.json`
includes `src/**/*`).

**Now:** `rm -rf "$EXT_DEST_DIR"` runs before `mkdir -p`/`cp -r` in the same
guarded chain above, so the destination is fully wiped and rebuilt from
current source on every install/`--update` run. Confirmed fixed.

## Independent checks

- `bash -n scripts/install.sh` — syntax OK.
- `python -m pytest tests/test_cost_collection_service.py -q` — 20 passed,
  0 failed (unrelated `datetime.utcnow()` deprecation warnings only, no
  regressions; this feature touches zero Python).
- `git status --short` — clean tree, nothing uncommitted.
- No new issues found in the fix diff itself: the guard chain is a plain
  `||` short-circuit (correct precedence, no dangling `&&`/`||`), `EXT_BUILD_OUTPUT`
  is scoped to this block only, and `rm -rf "$EXT_DEST_DIR"` operates on a
  hardcoded, non-empty, feature-owned path (`$HOME/.pi/agent/extensions/hephaestus-cost-tracker`) — no risk of an empty-variable `rm -rf` widening its blast radius.

## Verdict

All 2 BLOCKERs and 1 WARNING from the prior run are fixed and independently
re-verified via direct shell repro (not just code reading). No new findings.
**PASS.**
