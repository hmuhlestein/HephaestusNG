---
type: architecture
feature_id: des-91c8-cost-collectors
status: complete
---

# Architecture: CLI Cost Collectors (Pi + Claude Code)

**Feature ID:** des-91c8-cost-collectors
**Status:** Architecture Complete
**Date:** 2026-07-25
**Input:** `docs/requirements_analysis.md` (PASS per `docs/scope_review/scope_review_result.md`)

## 1. Scope Recap

This feature is packaging/deployment only. The collector runtime logic
(`PiJsonlCollector`, `ClaudeCodeCollector`, checkpointing, the `/cost-entries`
API, the pi extension's TypeScript source) is already implemented and merged.
Nothing here touches `src/services/cost_collection_service.py`,
`src/core/cost_derivation.py`, the DB schema, or budget enforcement.

Three concrete gaps, all inside `scripts/install.sh` and
`extensions/hephaestus-cost-tracker/README.md`:

1. `scripts/install.sh` never copies/builds the pi extension → dead code in
   practice (JSONL-tailing fallback still collects the cost, just not in
   real time).
2. `install.sh --update` doesn't refresh an already-installed extension.
3. `README.md` documents the wrong default API URL (`8000` vs. actual `8300`).

No architecture diagram beyond this is warranted — there is one script, one
doc file, and one one-way data flow (install-time file copy + build), not a
running system with components that talk to each other at runtime.

## 2. Design Decision: Where the install step goes

`scripts/install.sh` already has a pi-detection block at line 569:

```bash
if command -v pi >/dev/null 2>&1 || [ -d "$HOME/.pi" ]; then
    log "Pi detected — configuring MCP tools"
    ...
    log "Restart Pi after installation for MCP tools to take effect"
    ...
else
    log "Pi not detected — skipping MCP tool configuration"
fi
```

This is the FR-1 hook point the requirements doc asked architecture to
confirm — it exists, matching the same `command -v pi / [ -d ~/.pi ]` check
used everywhere else in the script for CLI-presence detection. **Decision:**
add a new step inside this existing `if` block (after the pi-mcp-adapter
install, before the "Restart Pi" log line), rather than adding a second
top-level `if command -v pi` block. One pi-detection branch, one place a
future reader looks for "what happens when pi is present."

The step must run on both fresh install and `--update` (FR-2) — unlike the
venv/frontend/node_modules steps elsewhere in the script, there's no
"skip if already present" gate here: `npm install && npm run build` is
idempotent and cheap (single-package extension, no lockfile-heavy deps), so
it always re-runs. This also satisfies the idempotent-reinstall NFR without
extra branching.

## 3. Component: pi extension install/build step in `scripts/install.sh`

**Location:** inside the existing `if command -v pi ... ; then` block
(current lines 569–790), after the "Generating Hephaestus pi agents" step
and before `log "Restart Pi after installation..."`.

**Logic** (matches existing script style: `log`/`ok`/`warn` helpers, no
fatal exit for this optional step):

```bash
# Install/update the real-time cost tracking extension
EXT_SRC_DIR="$PREFIX/extensions/hephaestus-cost-tracker"
EXT_DEST_DIR="$HOME/.pi/agent/extensions/hephaestus-cost-tracker"

if [ -d "$EXT_SRC_DIR" ]; then
    log "Installing Hephaestus cost tracker extension..."
    if command -v npm >/dev/null 2>&1; then
        mkdir -p "$EXT_DEST_DIR"
        cp -r "$EXT_SRC_DIR"/* "$EXT_DEST_DIR/"
        if (cd "$EXT_DEST_DIR" && npm install --silent 2>&1 | tail -3 && npm run build 2>&1 | tail -3); then
            ok "Cost tracker extension installed"
        else
            warn "Cost tracker extension build failed — real-time cost tracking disabled"
            warn "Cost data will still be collected via task-completion fallback"
        fi
    else
        warn "npm not found — skipping cost tracker extension (fallback collection still works)"
    fi
else
    warn "Extension source not found at $EXT_SRC_DIR — skipping"
fi
```

**Interfaces:**
- Input: `$PREFIX/extensions/hephaestus-cost-tracker/` (source tree, already
  in the repo, contains `src/`, `package.json`, `tsconfig.json`, `README.md`).
- Output: `$HOME/.pi/agent/extensions/hephaestus-cost-tracker/dist/index.js`
  (built artifact pi loads on next launch), plus copied `package.json`,
  `node_modules/` from the local `npm install`.
- No network calls beyond `npm install` resolving `typescript` from the
  configured npm registry (existing script already assumes npm registry
  access for the frontend `npm install` step at line 448).

**Failure handling:** every failure path is `warn`, not `err` — matches
FR-1's acceptance criterion ("reports success/failure without aborting the
rest of install") and the existing script's pattern for the pi-mcp-adapter
install a few lines above it, which uses the same non-fatal `warn` on
failure.

**Idempotency:** `cp -r` overwrites existing files in `$EXT_DEST_DIR`;
`npm install`/`npm run build` are naturally idempotent. No pre-check for
"already installed" needed — re-running always produces a fresh `dist/`
from current `src/`, which is exactly what FR-2 (`--update` refreshes the
build) requires. `--update` needs no special-case code: the same block
already runs unconditionally for both fresh and `--update` invocations.

## 4. Component: `README.md` fix

`extensions/hephaestus-cost-tracker/README.md` has two lines with the wrong
default, both to change from `8000` to `8300`:

- Line 30: `# Hephaestus API URL (default: http://localhost:8000)`
- Line 31: `export HEPHAESTUS_API_URL=http://localhost:8000`

No other change to this file. `src/index.ts` (already correct at `8300`)
and `hephaestus_config.yaml` (`port: 8300`) are untouched — they're the
source of truth this doc is being corrected to match.

## 5. Data Flow

```
scripts/install.sh (pi detected)
  └─ copy extensions/hephaestus-cost-tracker/{src,package.json,tsconfig.json}
       → ~/.pi/agent/extensions/hephaestus-cost-tracker/
  └─ npm install && npm run build (in dest dir)
       → ~/.pi/agent/extensions/hephaestus-cost-tracker/dist/index.js

pi launches an agent session (unrelated to this feature, pre-existing)
  └─ loads dist/index.js as an extension
  └─ on turn_end: reads HEPHAESTUS_AGENT_ID / TASK_ID / WORKFLOW_ID (env,
     already set by manager.py:481-484)
  └─ POSTs to http://localhost:8300/cost-entries (already implemented,
     now correctly documented)
```

Nothing in this flow is new at the API/schema level — the diagram exists
only to confirm the install step lands the artifact in the exact path the
already-implemented `POST /cost-entries` consumer expects it.

## 6. Infrastructure Requirements

- No new infrastructure. Reuses `npm`/`node`, already a prerequisite check
  for the frontend build step (`install.sh` line 148-155). This feature
  does not add a second Node/npm detection block earlier in the script —
  it checks `command -v npm` independently inside the new step, since the
  pi-detection block can run even when `--skip-frontend` skipped the
  earlier Node check.
- No `--skip-pi-extension` flag: per requirements FR-1 risk note, this is
  only warranted if the step is "heavy or risky enough to need one." A
  single-package `npm install && tsc build` (no runtime deps, per
  `package.json`) is neither — it already degrades gracefully via `warn` on
  any failure. Not adding the flag; noting this as a deliberate
  architecture decision so development doesn't add one unprompted.

## 7. Task Breakdown

### Task 1: Add pi extension install/build step to `scripts/install.sh`
**Blocks:** Task 3 (verification)
**Blocked by:** none

- Add the install/build block (Section 3 above) inside the existing
  `if command -v pi ...` block in `scripts/install.sh`, after the
  "Hephaestus pi agents" generation step and before the "Restart Pi"
  log line.
- Use `$PREFIX/extensions/hephaestus-cost-tracker` as source (matches how
  `PI_AGENTS_DIR` copying already resolves paths relative to `$PREFIX` a
  few lines above).
- Use the existing `log`/`ok`/`warn` helpers only — no new logging function.

**Acceptance criteria:**
- On a machine with `pi` installed (`command -v pi` or `~/.pi` present) and
  `npm` available, running `./scripts/install.sh` results in
  `~/.pi/agent/extensions/hephaestus-cost-tracker/dist/index.js` existing.
- On a machine without `pi` detected, the step does not run at all (stays
  inside the existing `if` block) and install completes normally.
- On a machine with `pi` but without `npm`, install completes with a `warn`
  and no `dist/` directory — install does not abort.
- Running `./scripts/install.sh --update` on a machine that already has the
  extension installed re-runs the copy/build and produces a `dist/index.js`
  reflecting the current `src/index.ts` (verify by touching a comment in
  `src/index.ts`, running `--update`, and confirming the built `dist/`
  changed).
- Re-running `install.sh` twice in a row (fresh, no `--update`) does not
  error — `cp -r` and `npm install`/`build` overwrite cleanly.

### Task 2: Fix `HEPHAESTUS_API_URL` default in `extensions/hephaestus-cost-tracker/README.md`
**Blocks:** Task 3 (verification)
**Blocked by:** none

- Change both occurrences of `http://localhost:8000` to `http://localhost:8300`
  in the "Configuration" section (lines 30-31).
- No other edits to the file.

**Acceptance criteria:**
- `README.md`'s documented default is `http://localhost:8300`, matching
  `src/index.ts`'s actual default and `hephaestus_config.yaml`'s `port: 8300`.

### Task 3: Regression check on existing collector tests (verification only)
**Blocks:** none
**Blocked by:** Task 1, Task 2 (run after, to confirm the install-script
change didn't touch anything the tests exercise — it shouldn't, since
`cost_collection_service.py` is untouched by this feature)

- Run `pytest tests/test_cost_collection_service.py` and confirm it passes
  unchanged. No new tests are expected — FR-4 is explicit that this is
  verification only, not re-implementation, and this feature makes no
  changes to Python collector code.
- Do not add install-script tests unless development finds `install.sh`
  already has a test harness (bats, shellcheck, etc.) to extend — check
  `tests/` and the repo root for an existing shell-test runner before
  deciding whether one is warranted. If none exists, manual verification
  per Task 1's acceptance criteria is sufficient — do not introduce a new
  shell-testing framework for a ~15-line script change.

**Acceptance criteria:**
- `tests/test_cost_collection_service.py` passes with no modifications.
- If a shell-test harness for `install.sh` already exists, extend it to
  cover Task 1's acceptance criteria; if none exists, this criterion is
  satisfied by manual verification (documented in the development phase's
  summary), no new framework introduced.

## 8. Non-Goals (carried from requirements, restated for development phase)

- No changes to `PiJsonlCollector`, `ClaudeCodeCollector`, `CostEntry`
  schema, checkpoint semantics, or `POST /cost-entries`.
- No `--skip-pi-extension` flag (Section 6).
- No OpenCode/Codex collector work.
- No historical cost backfill.
