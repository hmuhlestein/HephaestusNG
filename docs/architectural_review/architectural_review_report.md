---
type: architectural_review_result
feature_id: des-91c8-cost-collectors
verdict: PASS
blocker_count: 0
fix_count: 0
defer_count: 0
---

# Architectural Review: CLI Cost Collectors (Pi + Claude Code)

**Reviewed against:** `docs/architecture.md` (Tasks 1–3) and `docs/requirements_analysis.md` (FR-1 through FR-4)
**Implementation commit:** `dc933f6` (development phase, no-op self-review — confirmed correct)
**Diff reviewed:** `git diff d570606 dc933f6 -- scripts/install.sh extensions/hephaestus-cost-tracker/README.md`

Note on scope: `git diff main` shows ~87 files changed, but that diff is
dominated by unrelated drift on `main` since this branch was cut (workflow
YAML configs, orchestrator/spec/monitor changes, frontend components, etc.)
— none of it produced by this feature's development phase. The actual
development-phase diff, isolated via `git diff <architecture_design commit>
<development commit>`, touches exactly the two files this feature's
architecture scoped: `scripts/install.sh` and
`extensions/hephaestus-cost-tracker/README.md`. This review covers that
diff.

## Task 1: pi extension install/build step in `scripts/install.sh`

**Architecture (Section 3) specified:** a block inside the existing
`if command -v pi ...` block, after the "Hephaestus pi agents" step and
before the "Restart Pi" log line, copying
`$PREFIX/extensions/hephaestus-cost-tracker/` to
`~/.pi/agent/extensions/hephaestus-cost-tracker/`, running
`npm install && npm run build`, warning (not aborting) on any failure path,
with no `--skip-pi-extension` flag and no "already installed" pre-check.

**Implementation:** matches verbatim. Placement is exactly between the
backup-file cleanup (`rm -f "$PI_MCP_BACKUP"`) and
`log "Restart Pi after installation..."`. Three failure paths, all `warn`,
none fatal: missing `npm`, missing source dir, build failure. Uses the
project's existing `log`/`ok`/`warn` helpers only, matching NFR style
requirement. `cp -r` + `npm install`/`build` are naturally idempotent —
satisfies FR-2 (`--update` refresh) with zero special-case code, exactly as
the architecture decided. `EXT_SRC_DIR`/`EXT_DEST_DIR` are single-use,
locally scoped, no naming collisions elsewhere in the script (checked via
grep). `bash -n scripts/install.sh` passes.

Verdict: compliant, no findings.

## Task 2: `README.md` API URL fix

**Architecture (Section 4) specified:** change both `8000` occurrences
(comment + `export` line, lines 30–31) to `8300`, no other edits.

**Implementation:** exactly those two lines changed, nothing else touched.
Matches `src/index.ts`'s actual default and `hephaestus_config.yaml`'s
`port: 8300`.

Verdict: compliant, no findings.

## Task 3: Regression verification

**Architecture specified:** re-run `tests/test_cost_collection_service.py`,
expect no changes needed since no Python was touched.

**Verified independently (not just trusting the dev report):** ran
`python -m pytest tests/test_cost_collection_service.py -q` myself —
20 passed, 0 failed. `git status --short` shows a clean tree (no stray
edits). This confirms both the dev phase's claim and that this feature's
diff indeed touches zero Python files.

Verdict: compliant, no findings.

## Component Boundaries / Interface Contracts / Data Flow

- No new interfaces introduced. The install step's only "interface" is the
  filesystem path contract (`~/.pi/agent/extensions/hephaestus-cost-tracker/dist/index.js`)
  that pi's extension loader and the already-implemented `POST /cost-entries`
  consumer expect — architecture correctly identified this as the sole
  integration point, and the implementation lands the artifact at exactly
  that path.
- Zero changes to `PiJsonlCollector`, `ClaudeCodeCollector`, `CostEntry`
  schema, checkpoint semantics, or the `/cost-entries` endpoint — matches
  the NFR "no behavior change to existing collectors."
- Data flow (install-time copy+build → pi loads extension → POSTs to
  `:8300/cost-entries`) is unchanged from the architecture doc; nothing in
  the implementation deviates from it.

## Over-Engineering Check

None found. No `--skip-pi-extension` flag was added (architecture
explicitly decided against it — respected). No new abstractions, no
speculative config, no unrelated refactors to `install.sh`. The diff is
exactly the ~24 lines architecture specified plus a 2-line doc fix — nothing
more.

## Findings Summary

No BLOCKER, FIX, or DEFER findings. Implementation is a precise,
verbatim-level match to the architecture with no scope creep and no
deviation. Development phase's own self-review report (commit `dc933f6`)
accurately described the state of the code — independently re-verified
here, not taken on faith.

**Verdict: PASS.**
