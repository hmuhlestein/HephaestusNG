---
type: requirements
feature_id: des-91c8-pi-extension
status: complete
---

# Product Requirements Analysis: Pi Cost Tracker Extension

**Feature ID:** des-91c8-pi-extension
**Feature Name:** Pi Cost Tracker Extension
**Status:** Requirements Extracted
**Date:** 2026-07-26
**Design Document:** `.hephaestus/spec.md`, "Pi Extension Collector" section (lines 621-647)
**Parent Features (already merged to `main`):** Cost Tracking Database Schema → Cost Derivation Engine → Budget Enforcement and Pipeline Throttling → CLI Cost Collectors (Pi + Claude Code)

---

## 1. Executive Summary

This branch (`feature/des-91c8/pi-extension`) is currently **identical to `main`** (`git log main..HEAD` is empty). Direct inspection shows the pi extension described in the design's "Pi Extension Collector" section is **already fully implemented, installed, and wired**:

- `extensions/hephaestus-cost-tracker/src/index.ts` exists: hooks `turn_end`, reads `message.usage.cost.total`, accumulates a running session total, updates the pi TUI status bar via `ctx.ui.setStatus()`, and POSTs a fire-and-forget cost entry that never blocks the turn on failure.
- `scripts/install.sh:783-806` copies the extension to `~/.pi/agent/extensions/hephaestus-cost-tracker/`, runs `npm install && npm run build`, and degrades gracefully (warns, doesn't abort install) if `npm` is missing or the build fails.
- `POST /api/autopilot/cost-entries` (`src/mcp/autopilot_api.py:2144`) requires and verifies `X-Agent-ID` via `verify_agent_authentication`, matching the extension's request headers.
- `HEPHAESTUS_API_URL` defaults agree everywhere: `index.ts:58` → `http://localhost:8300`, `README.md:31` → `http://localhost:8300`, `hephaestus_config.yaml` → `port: 8300`.
- Cost attribution uses `HEPHAESTUS_AGENT_ID`/`TASK_ID`/`WORKFLOW_ID` env vars (already set by `manager.py` when launching agent tmux sessions), not a `session_id` field — this is a deliberate, reasonable deviation from one sentence in the design doc (which describes reading `session_id` via `ctx.sessionManager`), since `CostEntry` (`src/core/database.py:1230`) has no `session_id` column at all; every other collector attributes by `task_id`/`agent_id`/`workflow_id` too. Not a defect.

**What this feature is actually about, given that state:** one concrete documentation bug, plus closing an unverified assumption about how pi loads the extension. There is no missing collection logic, no missing install wiring, and no missing schema/API work.

## 2. Problem Statement

### 2.1 Confirmed defect: `README.md`'s documented POST path is wrong

`extensions/hephaestus-cost-tracker/README.md:44` ("How It Works", step 4) says the extension posts to `POST /cost-entries`. The actual code (`src/index.ts:123`) posts to `${apiUrl}/api/autopilot/cost-entries`, matching the real route (`autopilot_api.py:37,144`: router prefix `/api/autopilot` + `@router.post("/cost-entries")`). A developer following the README literally to test the API by hand would hit a 404. One-line doc fix.

### 2.2 Unverified: does pi actually load this extension shape?

Nothing in this repo can confirm that pi's real extension loader invokes `initialize(ctx)` / `turn_end(ctx, turn)` on a default-exported class instance the way `index.ts` assumes — `.pi/agent/extensions/` is pi's own runtime, external to this codebase, and there is no pi installation available here to smoke-test against. The design doc asserts this shape works; nothing in this repo's test suite (`tests/test_cost_collection_service.py` only covers the Python-side JSONL/Claude Code collectors) exercises the TypeScript extension at all. This is a real gap in verification, not implementation — flagged for QA/architecture to close with an actual `pi` install + a running turn, not more source-reading.

### 2.3 Everything else in scope is done

Extension install/build (FR-1/FR-2 from the prior `CLI Cost Collectors` requirements doc), the `HEPHAESTUS_API_URL` port fix (its FR-3), and the collector's own correctness (its FR-4) all landed in the merged `feat: CLI Cost Collectors (Pi + Claude Code)` work. No further code changes to `cost_collection_service.py`, the schema, or budget enforcement are needed or in scope here.

## 3. Existing Project Context

- **Cost Tracking Database Schema** (merged): `cost_entries` ledger, `SessionCostCheckpoint`, `cost_total_usd` rollups on `Task`/`Feature`/`Workflow`/`AutopilotDesign`/`AutopilotProject`.
- **Cost Derivation Engine** (merged): `src/core/cost_derivation.py` self-healing rollup.
- **Budget Enforcement and Pipeline Throttling** (merged): `cost_limit_usd`, `_pause_project_workflows`, UI budget config.
- **CLI Cost Collectors (Pi + Claude Code)** (merged): `PiJsonlCollector`, `ClaudeCodeCollector`, task-completion wiring, extension install/build in `scripts/install.sh`, README port fix.
- This feature sits entirely on top of that stack — it touches only the pi extension's own doc accuracy and its verification status, nothing upstream.

## 4. Functional Requirements

**FR-1: Fix `README.md`'s documented POST path**
- Change `extensions/hephaestus-cost-tracker/README.md`'s "How It Works" step 4 from `POST /cost-entries` to `POST /api/autopilot/cost-entries` to match `src/index.ts:123` and the real route.
- Acceptance: README's documented path matches the literal string in `index.ts` and resolves against `autopilot_api.py`'s router prefix + route decorator.

**FR-2 (verification, no code expected unless a defect is found): Confirm the extension actually loads and runs under a real pi install**
- Install `pi`, run `scripts/install.sh`, confirm `~/.pi/agent/extensions/hephaestus-cost-tracker/dist/index.js` builds and loads on pi startup without error, and that a real turn triggers `turn_end`, updates the TUI status, and produces a `cost_entries` row via the API.
- Acceptance: one real pi session with `HEPHAESTUS_AGENT_ID`/`TASK_ID`/`WORKFLOW_ID` set produces a `cost_entries` row with `source="pi"` and correct `cost_usd`.

**FR-3 (verification only, no code expected): Existing behavior holds**
- `PiJsonlCollector` fallback, `verify_agent_authentication` gating on `/cost-entries`, and the install.sh copy/build step must not regress. Re-run `tests/test_cost_collection_service.py`; do not re-implement.

## 5. Non-Functional Requirements

- **No behavior change to the extension's collection logic**: `turn_end`'s cost extraction, TUI status update, and fire-and-forget POST are already correct and tested-by-inspection against the design; this feature must not alter them beyond FR-1's doc fix.
- **No new test tooling**: this repo has no JS/TS test framework anywhere (not even `frontend/`, which has no `test` script). Do not introduce Jest/Vitest solely to unit-test this 140-line extension — inconsistent with existing project conventions. Verification is manual/integration (FR-2), not a new unit-test suite.
- **Graceful degradation preserved**: extension failures (build failure, POST failure, auth failure when run outside Hephaestus) must continue to only log a warning and never block a pi turn or fail `scripts/install.sh`.

## 6. Component Dependencies

- `extensions/hephaestus-cost-tracker/README.md` — doc-only change (FR-1).
- `extensions/hephaestus-cost-tracker/src/index.ts` → `POST /api/autopilot/cost-entries` (`autopilot_api.py:2144`, already exists, no change) — verification only (FR-2).
- No dependency on `cost_collection_service.py`, `cost_derivation.py`, or the database schema — all upstream, merged, and untouched by this feature.

## 7. Technology Constraints

- `README.md` is plain Markdown; no toolchain implications for FR-1.
- FR-2's verification requires a real `pi` binary and a machine where `npm`/`node` are available — this cannot be done inside this sandboxed repo checkout and must happen wherever the next phase actually has `pi` installed (or be explicitly deferred/accepted as an open risk if no such environment is available to this pipeline).

## 8. Integration Points

- No new integration points. The extension already integrates with `scripts/install.sh` (install-time) and `POST /api/autopilot/cost-entries` (runtime) — both pre-existing and unchanged.

## 9. Out of Scope

- Any change to `cost_entries` schema, `cost_derivation.py`, or budget enforcement (merged, stable).
- Adding a `session_id` field to `CostEntry` or the extension's POST body — the design doc's mention of `ctx.sessionManager` is superseded by the simpler, already-working env-var-based attribution; not a gap to close.
- OpenCode/Codex collectors (separately deferred per `.hephaestus/spec.md`'s Non-Goals).
- Introducing a JS/TS test framework (see NFR above).

## 10. Risks / Open Questions for Architecture/Scope Review

- **This feature may be a documentation-only, one-line change plus a manual verification step.** Scope review should confirm whether that's sufficient to satisfy the workflow's intent, or whether there's additional scope (e.g., a real pi-install smoke test harness) expected that isn't visible from the design doc or repo state alone.
- FR-2 depends on an environment with `pi` actually installed, which this repo checkout does not have. If no such environment exists anywhere in this pipeline's execution, FR-2 should be downgraded to a documented, accepted risk rather than blocking the pipeline.
