---
type: adversarial_review_result
feature_id: des-91c8-cost-collectors
verdict: BLOCKERS_FOUND
blocker_count: 2
warning_count: 1
nit_count: 1
---

# Adversarial Review: CLI Cost Collectors (Pi + Claude Code)

**Diff under review:** `git diff d570606 dc933f6 -- scripts/install.sh extensions/hephaestus-cost-tracker/README.md`
(the entire feature's code change: 24 lines added to `scripts/install.sh`, 2 lines changed in `README.md`)

Both the development phase's self-review and the architectural review phase
signed off on this diff as "no findings," on the strength of reading the code
and matching it against the architecture doc line-by-line. Neither actually
executed the failure paths the new code claims to handle. Both failure paths
below were verified by running the actual shell semantics, not by inspection.

## BLOCKER 1 — `npm install`/`npm run build` failures are never detected; the extension silently reports "installed" even when the build failed

**Location:** `scripts/install.sh:791`

```bash
if (cd "$EXT_DEST_DIR" && npm install --silent 2>&1 | tail -3 && npm run build 2>&1 | tail -3); then
    ok "Cost tracker extension installed"
else
    warn "Cost tracker extension build failed — real-time cost tracking disabled"
fi
```

The script has `set -e` (line 22) but **no `set -o pipefail`** (confirmed:
`grep -n pipefail scripts/install.sh` returns nothing). Without `pipefail`,
the exit status of `cmd | tail -3` is the exit status of `tail`, not `cmd`.
`tail` on a pipe essentially never fails. So this `if` is testing "did `tail`
exit 0," which is always true, regardless of whether `npm install` or
`npm run build` actually failed.

**Verified:**
```
$ bash -c 'set -e; if (false | tail -3); then echo TRUE; else echo FALSE; fi'
TRUE
```
`false` stands in for a failed `npm install`/`npm run build` here — the `if`
still takes the success branch.

**Failure sequence:** a machine with `npm` present but a broken/unreachable
npm registry, a `typescript` version conflict, or a `tsc` compile error (e.g.
someone lands a genuinely broken `src/index.ts` later) → `npm install` or
`npm run build` exits non-zero → `tail -3` still exits 0 → script prints
`ok "Cost tracker extension installed"` → no `dist/index.js` exists (or a
stale one from a previous run does) → pi silently fails to load the
extension or loads garbage → real-time cost tracking silently doesn't work,
and the operator was explicitly told it does. This is the exact "silent
failure" class FR-1's acceptance criterion ("reports success/failure") was
written to prevent, and it's broken by the one line that was supposed to
implement that criterion.

**Fix:** add `set -o pipefail` at the top of the script (verify this doesn't
change behavior of other pre-existing `cmd | tail -3` patterns in the file —
e.g. line 575, 577 — which have the *same* latent bug, just outside this
feature's diff scope), or avoid the pipe inside the condition, e.g.:
```bash
if npm_out=$(cd "$EXT_DEST_DIR" && npm install --silent 2>&1 && npm run build 2>&1); then
    ok "Cost tracker extension installed"
else
    warn "Cost tracker extension build failed — real-time cost tracking disabled"
    echo "$npm_out" | tail -6
fi
```

## BLOCKER 2 — `mkdir -p`/`cp -r` are not guarded; a failure here aborts the entire install script, not just this feature

**Location:** `scripts/install.sh:789-790`

```bash
mkdir -p "$EXT_DEST_DIR"
cp -r "$EXT_SRC_DIR"/* "$EXT_DEST_DIR/"
```

The script runs under `set -e`. Every other filesystem write in this same
`if command -v pi` block redirects stderr and/or is itself inside a
conditional so a failure doesn't kill the script (e.g. line 595
`cp "$PREFIX/agents/pi"/*.md "$PI_AGENTS_DIR" 2>/dev/null`, line 636
`cp "$PI_MCP_CONFIG" "$PI_MCP_BACKUP" 2>/dev/null`). These two new lines are
bare statements with no `2>/dev/null`, no `|| true`, and no enclosing `if` —
so `set -e` applies to them directly.

**Verified:**
```
$ bash -c 'set -e; echo before; mkdir -p /nonexistent_root_test_xyz/sub; echo after'
before
mkdir: /nonexistent_root_test_xyz: Read-only file system
exit 1        # "after" never printed — script terminated
```

**Failure sequence:** `~/.pi/agent/extensions/` doesn't exist and its parent
is on a read-only mount, or disk is full, or `~/.pi` is owned by another
user/root (common after `sudo pi` runs once) → `mkdir -p "$EXT_DEST_DIR"` (or
the subsequent `cp -r`) exits non-zero → `set -e` kills `install.sh`
immediately → every step after this point in the script (currently just the
two closing "Restart Pi" log lines, but this block sits before whatever
future steps get appended after it) silently never runs, and the user sees a
bare `mkdir:`/`cp:` OS error with no `warn`, no `ok`, no indication install
"completed" or not. This directly contradicts the architecture's explicit
requirement ("every failure path is `warn`, not `err`... reports
success/failure without aborting the rest of install") and both prior
reviews' claim that this was verified — neither review actually forced a
permission failure to check it.

**Fix:** guard both statements so they degrade the same way every other
write in this block does:
```bash
if ! mkdir -p "$EXT_DEST_DIR" 2>/dev/null || ! cp -r "$EXT_SRC_DIR"/* "$EXT_DEST_DIR/" 2>/dev/null; then
    warn "Could not write to $EXT_DEST_DIR — skipping cost tracker extension"
else
    if (cd "$EXT_DEST_DIR" && ...); then ...
fi
```

## WARNING — `cp -r` never removes files, so `--update` can leave stale `.ts` sources in the destination that `tsc` will still compile

**Location:** `scripts/install.sh:790`, combined with
`extensions/hephaestus-cost-tracker/tsconfig.json`'s `"include": ["src/**/*"]`

`cp -r "$EXT_SRC_DIR"/* "$EXT_DEST_DIR/"` only ever adds/overwrites files; it
never deletes a destination file whose source counterpart was removed
upstream. If a future change to the extension deletes or renames a `.ts`
file in `extensions/hephaestus-cost-tracker/src/`, an operator who installed
an earlier version and later runs `install.sh --update` keeps the orphaned
file at `~/.pi/agent/extensions/hephaestus-cost-tracker/src/<old-file>.ts`
forever. Because `tsconfig.json`'s `include` is a glob over the whole `src`
tree, `tsc` will still compile that orphaned file into `dist/` on every
`--update`, either producing stale/dead code in the bundle or (if the
orphaned file references something that was removed elsewhere) a compile
error — which, combined with BLOCKER 1, would be reported as success anyway.

This wasn't exercised by either prior review: the development phase's
"scratch-dir `--update` simulation" (referenced in commit `dc933f6`) only
tested modifying an existing file's contents, not removing one — the exact
case that exposes this gap.

**Fix:** `rm -rf "$EXT_DEST_DIR"` before the `cp -r` (safe: it's a
purpose-built extension directory the script fully owns), or `rsync -a --delete`
in place of `cp -r`.

## NIT — new block's `cp -r`/`mkdir -p` don't redirect stderr, inconsistent with every other write in this section

Every other mutating filesystem command in this `if command -v pi` block
(lines 595, 636, 709, 740, 758, 779-780) redirects stderr with
`2>/dev/null` and lets `warn`/`ok` carry the user-facing message. The new
`mkdir -p`/`cp -r` (lines 789-790) don't, so a failure prints a raw
`mkdir:`/`cp:` OS error to the terminal before (or, per BLOCKER 2, instead
of) any `warn`. Cosmetic once BLOCKER 2 is fixed with the guard above, since
that guard already adds the `2>/dev/null`.

## Scope notes

- Confirmed via `git diff d570606 dc933f6` that this is genuinely the
  entire feature's code change — no other files are touched. The
  already-implemented collector runtime (`PiJsonlCollector`,
  `ClaudeCodeCollector`, `cost_collection_service.py`, `/cost-entries`,
  the extension's `src/index.ts`) is out of scope per `docs/architecture.md`
  and was not re-audited here beyond confirming it's untouched by this diff.
- Composition/polymorphism/complex-logic-pushed-down criteria: not
  meaningfully applicable — this diff is 24 lines of bash sequencing
  existing shell idioms (`log`/`ok`/`warn`, `command -v`, `[ -d ]`) already
  used throughout `install.sh`. No classes, no conditionals standing in for
  polymorphism, nothing to push down.
- README.md fix (Task 2): the two `8000` → `8300` line changes are correct
  and match `src/index.ts`'s actual default (`HEPHAESTUS_API_URL`
  fallback) and `hephaestus_config.yaml`'s `port: 8300`. No issues found.
- No concurrency, no DB access, no long-lived network connections in this
  diff beyond the `npm install` registry fetch already covered by BLOCKER 1
  — the "concurrent code paths" and "DB/file/network connection leaks"
  criteria turned up nothing beyond what's reported above, since this diff
  has no threads or held connections to leak.

## Summary

2 BLOCKERs, both in the ~10 lines of new `install.sh` logic that the
architecture and both prior reviews described as a verbatim, verified match
to spec. Neither prior review actually forced a build failure or a
permission failure to confirm the "warn, don't abort" behavior they signed
off on — they confirmed the code *reads* like it does the right thing, not
that it *does* the right thing under failure. Both blockers reproduce with a
one-line `bash -c` repro (included above) and both directly undermine FR-1's
explicit acceptance criterion that this step "reports success/failure
without aborting the rest of install."
