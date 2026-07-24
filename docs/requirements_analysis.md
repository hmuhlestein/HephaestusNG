# Product Requirements Analysis: CLI Cost Collectors (Pi + Claude Code)

**Feature ID:** des-91c8-cost-collectors
**Feature Name:** CLI Cost Collectors (Pi + Claude Code)
**Status:** Requirements Extracted
**Date:** 2026-07-24
**Design Document:** `.hephaestus/design.md` ("Collection Architecture" and "Pi Extension Collector" sections) + `docs/COST_TRACKING_DESIGN.md` (same content, project-tracked copy)
**Parent Features (already merged to `main`):** Cost Tracking Database Schema (DES-91c8) → Cost Derivation Engine → Budget Enforcement and Pipeline Throttling

---

## 1. Executive Summary

This is **not a greenfield build**. Direct inspection of `main` (this branch is currently identical to `main`, zero diff) shows the Pi and Claude Code collectors described in the design are **already fully implemented, wired, and tested**:

- `src/services/cost_collection_service.py` — `PiJsonlCollector` and `ClaudeCodeCollector` classes exist, both correct against the design's documented schemas (pi's pre-computed `usage.cost.total`; Claude Code's token-based price table with 1h/5m cache-write tiers).
- `collect_task_cost()` is wired into `task_completion_service.py:926-928` on task completion.
- `SessionCostCheckpoint` (keyed by `session_id`, not `Agent.id` — the exact bug the design calls out) exists and is used for checkpointing.
- Claude Code's UUID5 session-ID fix is landed: `cli_interface.py:411` derives `uuid.uuid5(uuid.NAMESPACE_URL, session_id)` and passes `--session-id`.
- `POST /cost-entries` exists at `autopilot_api.py:2075` with agent authentication.
- `extensions/hephaestus-cost-tracker/src/index.ts` (the real-time pi extension) exists, hooks `turn_end`, reads `HEPHAESTUS_AGENT_ID`/`TASK_ID`/`WORKFLOW_ID` from env vars that `manager.py:481-484` already sets when launching agent tmux sessions, and POSTs to the API.
- `tests/test_cost_collection_service.py` has dedicated test classes for `PiJsonlCollector`, `ClaudeCodeCollector`, `CodexStubCollector`, `OpenCodeCollector`, and session-file discovery.

**What this feature is actually about, given that state:** closing the gap between "the extension exists as source" and "the extension is actually installed and running for real pi sessions" — plus fixing two concrete defects found during this review. The design doc explicitly states the extension "is installed globally at `~/.pi/agent/extensions/hephaestus-cost-tracker/` by `scripts/install.sh` when pi is detected" — that wiring does not exist. `scripts/install.sh` (792 lines) has zero references to `cost-tracker` or extension installation.

## 2. Problem Statement

### 2.1 The collector code is real; the deployment path is not

Both collector classes are correct and tested against synthetic JSONL fixtures. But two real defects prevent them from being fully effective in practice:

1. **Pi extension is never installed.** Nothing copies `extensions/hephaestus-cost-tracker/` to `~/.pi/agent/extensions/hephaestus-cost-tracker/`, runs `npm install && npm run build`, or checks pi is present. Per the extension's own `package.json` (`"main": "dist/index.js"`), it must be compiled — there is no committed `dist/`. Without install-time wiring, the real-time path is dead code that only works if a developer manually follows the extension's README by hand. The JSONL-tailing fallback (`PiJsonlCollector`, at task-completion time) still covers pi cost collection in this state, so cost data isn't lost — only the "real-time TUI display" and "no filesystem access" benefits described in the design are unrealized.

2. **API URL default mismatch between the extension's two docs.** `extensions/hephaestus-cost-tracker/src/index.ts:9` defaults `HEPHAESTUS_API_URL` to `http://localhost:8300`; the extension's own `README.md` documents the default as `http://localhost:8000`. Confirmed against `hephaestus_config.yaml:3` (`port: 8300`) — `index.ts` is correct, `README.md` is the stale/wrong one and needs the one-line fix.

### 2.2 Everything else in the Pi + Claude Code scope is done

No other functional gaps were found for the two sources this feature is explicitly scoped to (`pi`, `claude_code`). OpenCode and Codex collectors also already exist as stub/full implementations, but per `.hephaestus/design.md`'s Implementation Phases and Non-Goals sections they are separately gated (OpenCode on confirming actual deployment usage; Codex permanently stubbed until the CLI is available to inspect) — out of scope for this feature and left untouched.

## 3. Existing Project Context

- **Cost Tracking Database Schema** (merged): `cost_entries` ledger table, `SessionCostCheckpoint`, `cost_total_usd` rollup columns on `Task`/`Feature`/`Workflow`/`AutopilotDesign`/`AutopilotProject`.
- **Cost Derivation Engine** (merged): self-healing rollup (`src/core/cost_derivation.py`), mirroring `src/core/status_derivation.py`'s pattern.
- **Budget Enforcement and Pipeline Throttling** (merged): `cost_limit_usd` on `AutopilotProject`, `_pause_project_workflows`, `paused_by == "budget"` guard generalization, UI budget config in `ProjectSettingsModal.tsx`.
- This feature sits on top of all three — it does not touch schema, derivation, or enforcement, only the two named collectors and their deployment path.

## 4. Functional Requirements

**FR-1: Pi extension installed automatically by `scripts/install.sh`**
- When `scripts/install.sh` detects `pi` is installed (verify against existing CLI-detection logic in `install.sh` — how it currently detects other CLI tools' presence, if at all — and match that pattern), it copies `extensions/hephaestus-cost-tracker/` to `~/.pi/agent/extensions/hephaestus-cost-tracker/`, runs `npm install && npm run build` in that directory, and reports success/failure without aborting the rest of install on failure (cost tracking is a nice-to-have, not a hard install dependency).
- Acceptance: running `./scripts/install.sh` on a machine with `pi` installed results in `~/.pi/agent/extensions/hephaestus-cost-tracker/dist/index.js` existing and being loadable by pi on next launch.
- Acceptance: running `./scripts/install.sh` on a machine without `pi` installed does not attempt the copy/build and does not fail the overall install.

**FR-2: `--update` path also refreshes the extension**
- Design doc implies "installed... when pi is detected" applies at install time; `install.sh --update` (existing flag, pulls latest and reinstalls) should re-run the same copy/build step so a `git pull` that touches `extensions/hephaestus-cost-tracker/src/index.ts` actually reaches a machine that already has pi + the extension installed.
- Acceptance: `./scripts/install.sh --update` rebuilds `dist/index.js` from current `src/index.ts` if the extension is already present.

**FR-3: Fix the `HEPHAESTUS_API_URL` default mismatch**
- `README.md`'s documented default (`http://localhost:8000`) is wrong; `hephaestus_config.yaml:3` sets `port: 8300`, matching `index.ts`'s actual default. Fix `README.md`'s "Configuration" section to say `8300`. One-line doc fix, not a design change.
- Acceptance: `README.md`'s documented default matches `index.ts`'s actual default (`8300`), which matches `hephaestus_config.yaml`.

**FR-4 (verification only, no code expected): Existing collector behavior holds**
- `PiJsonlCollector` and `ClaudeCodeCollector` already meet the design's documented behavior (checkpoint-by-`session_id`, glob-and-verify session file discovery, price-table cost conversion for Claude Code, `cost_usd <= 0` lines skipped). Architecture/development phases should re-run `tests/test_cost_collection_service.py` to confirm no regression, not re-implement.

## 5. Non-Functional Requirements

- **No blocking on install failure**: extension install/build failures during `scripts/install.sh` must degrade gracefully (log a warning, continue installing Hephaestus itself) — matches the design's explicit fallback: "the JSONL tailing fallback still works" when the extension isn't loaded.
- **No behavior change to existing collectors**: this feature must not modify `PiJsonlCollector`/`ClaudeCodeCollector`'s collection logic, `CostEntry` schema, or checkpoint semantics — those are already shipped, tested, and in the merged budget-enforcement chain that depends on them being stable.
- **Idempotent re-install**: re-running `install.sh` (fresh or `--update`) against a machine that already has the extension installed must not error (overwrite `dist/`, don't fail on "directory already exists").

## 6. Component Dependencies

- `scripts/install.sh` → `extensions/hephaestus-cost-tracker/` (copy source), Node/npm (build toolchain — confirm `install.sh` already has an npm/node prerequisite check for the frontend build; reuse it rather than adding a second one).
- `extensions/hephaestus-cost-tracker/src/index.ts` → `manager.py`'s `HEPHAESTUS_AGENT_ID`/`TASK_ID`/`WORKFLOW_ID` env vars (already set, no change) and `POST /cost-entries` (already exists, no change).
- No new dependency on `cost_collection_service.py`, `cost_derivation.py`, or the database schema — all upstream and already merged.

## 7. Technology Constraints

- Extension build uses the existing `typescript` devDependency (`^5.0.0`) already declared in `extensions/hephaestus-cost-tracker/package.json` — no new toolchain.
- `scripts/install.sh` is bash; the new install step must follow its existing style (the `log`/`ok`/`warn`/`err` helper functions already defined at the top of the file) rather than introducing a different logging convention.
- Respect the `--skip-frontend`/`--skip-docker` flag pattern already in `install.sh` if a `--skip-pi-extension`-style opt-out turns out to be warranted during architecture (not assumed necessary here — only add if architecture phase decides the copy/build step is heavy or risky enough to need one).

## 8. Integration Points

- `scripts/install.sh`'s existing CLI-detection logic (wherever it currently checks for CLI-tool presence, if it does — verify during architecture) is the natural hook point for "when pi is detected."
- No API, schema, or backend service changes — this feature is packaging/deployment only for a component whose runtime logic already exists and is tested.

## 9. Out of Scope (per design doc's own Non-Goals + this feature's Pi/Claude Code framing)

- OpenCode collector activation/gating (separately deferred pending confirmed deployment usage).
- Codex collector (stubbed only, deferred until the CLI is available to inspect).
- Historical cost backfill.
- Any change to `cost_entries` schema, derivation, or budget enforcement (all merged, stable, out of this feature's blast radius).

## 10. Risks / Open Questions for Architecture Phase

- Confirm whether `scripts/install.sh` already has a pi-detection branch to hook into, or whether one needs to be added from scratch — this changes FR-1's implementation shape but not its acceptance criteria.
