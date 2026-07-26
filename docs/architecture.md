---
type: architecture
feature_id: des-91c8-pi-extension
status: complete
---

# Architecture: Pi Cost Tracker Extension

**Feature ID:** des-91c8-pi-extension
**Status:** Architecture Complete
**Date:** 2026-07-26
**Input:** `docs/requirements_analysis.md` (PASS per `docs/scope_review/scope_review_result.md`)

## 1. Scope Recap

This feature has no new runtime architecture. The pi extension
(`extensions/hephaestus-cost-tracker/src/index.ts`), its install/build
wiring (`scripts/install.sh:783-806`), the `POST /api/autopilot/cost-entries`
endpoint (`src/mcp/autopilot_api.py:2144`), and the env-var-based
attribution scheme are all already implemented and merged. Confirmed by
direct inspection (`git log main..HEAD` is empty as of the requirements
phase).

What remains is exactly what `docs/requirements_analysis.md` scoped:

1. **FR-1**: one wrong line in `extensions/hephaestus-cost-tracker/README.md`
   (documents `POST /cost-entries`, actual route is
   `POST /api/autopilot/cost-entries`).
2. **FR-2**: verification-only — confirm the extension actually loads under
   a real `pi` binary and produces a `cost_entries` row. No code is expected
   to come out of this unless verification finds a defect.
3. **FR-3**: verification-only — re-run the existing Python collector tests
   and confirm no regression.

No component diagram is warranted beyond this — there is one incorrect
sentence in a doc file and two verification checks, not a system with new
parts that talk to each other.

## 2. Design Decision: no code changes beyond the doc fix

Development should not use this phase as license to touch
`cost_collection_service.py`, `cost_derivation.py`, the `CostEntry` schema,
budget enforcement, or the extension's `index.ts` logic. All of that is
correct, tested-by-inspection, and out of scope per
`docs/requirements_analysis.md` §5/§9. **Decision:** the only file this
feature edits is `extensions/hephaestus-cost-tracker/README.md`. Any other
diff produced by the development phase is scope creep and should be
rejected at architectural review.

This also means there is nothing to introduce a JS/TS test framework for
(NFR in requirements doc, §5) — do not add Jest/Vitest to verify a 140-line
extension that has no existing test tooling anywhere in this repo.

## 3. Component: `README.md` fix

**File:** `extensions/hephaestus-cost-tracker/README.md`, "How It Works",
line 44.

**Change:**
```diff
-4. The cost entry is posted to Hephaestus API (`POST /cost-entries`)
+4. The cost entry is posted to Hephaestus API (`POST /api/autopilot/cost-entries`)
```

No other line in the file is wrong — the `Configuration` section's
`http://localhost:8300` default (lines 30-31) already matches
`index.ts:58` and `hephaestus_config.yaml`; that was fixed by the prior
`CLI Cost Collectors` feature and is not touched here.

## 4. Interface Contract (existing, unchanged — documented for reference)

**Request:** `POST {HEPHAESTUS_API_URL}/api/autopilot/cost-entries`
(`index.ts:123`, matching `autopilot_api.py:2144`'s router prefix
`/api/autopilot` + route `/cost-entries`)

Headers: `Content-Type: application/json`, `X-Agent-ID: <agent_id>`
(`index.ts:127-130`, verified server-side by `verify_agent_authentication`,
`autopilot_api.py:2156`).

Body (`CostEntry` interface, `index.ts:35-48`):
```
{
  task_id?: string; agent_id?: string; workflow_id?: string;
  source: string;                // always "pi" for this collector
  model?: string;
  input_tokens?, output_tokens?, cache_read_tokens?,
  cache_write_tokens?, reasoning_tokens?: number;
  cost_usd: number;
  raw_usage?: Record<string, any>;
}
```

This contract is not changing. It's included here only so development and
QA don't have to re-derive it from source when writing the FR-2
verification report.

## 5. Data Flow (existing, unchanged)

```
pi agent session (HEPHAESTUS_AGENT_ID/TASK_ID/WORKFLOW_ID set by
manager.py when the tmux session is launched)
  └─ pi loads ~/.pi/agent/extensions/hephaestus-cost-tracker/dist/index.js
  └─ on turn_end: reads message.usage.cost.total
  └─ updates TUI status bar via ctx.ui.setStatus()
  └─ POSTs CostEntry to /api/autopilot/cost-entries (fire-and-forget,
     failures only logged, never block the turn)
  └─ Hephaestus API authenticates via X-Agent-ID, persists the row,
     triggers cost_derivation.py rollup (unchanged, upstream)

if the extension isn't loaded (pi absent, build failed, etc.):
  └─ PiJsonlCollector tails the JSONL transcript at task completion
     (unchanged fallback, already tested by test_cost_collection_service.py)
```

## 6. Infrastructure Requirements

None new. No schema migration, no new config, no new dependency. FR-2's
verification needs a machine with a real `pi` binary installed, which this
sandboxed repo checkout does not have — per requirements §10, if no such
environment exists anywhere in this pipeline's execution, development
should document that as an accepted, explicit risk rather than block on it
or fabricate a result.

## 7. Task Breakdown

### Task 1: Fix `README.md`'s documented POST path
**Blocks:** none
**Blocked by:** none

- Change `extensions/hephaestus-cost-tracker/README.md` line 44 from
  `POST /cost-entries` to `POST /api/autopilot/cost-entries` (Section 3).
- No other edits to the file.

**Acceptance criteria:**
- Line 44 reads `POST /api/autopilot/cost-entries`.
- The string matches `index.ts:123`'s literal path and resolves against
  `autopilot_api.py`'s router prefix (`/api/autopilot`) + route decorator
  (`/cost-entries`).
- `git diff` for this task touches only that one line.

### Task 2: Verify the extension loads and runs under a real `pi` install
**Blocks:** none
**Blocked by:** none (independent of Task 1 — different files, no shared state)

- Install `pi`, run `scripts/install.sh`, confirm
  `~/.pi/agent/extensions/hephaestus-cost-tracker/dist/index.js` builds and
  loads without error on pi startup.
- Run one real turn with `HEPHAESTUS_AGENT_ID`/`TASK_ID`/`WORKFLOW_ID` set
  and confirm: the TUI status bar updates, and a `cost_entries` row is
  created with `source="pi"` and a correct `cost_usd`.
- If this environment isn't available anywhere in this pipeline run,
  document that explicitly as an accepted risk (per requirements §10) —
  do not fabricate a verification result and do not block the pipeline on
  it.

**Acceptance criteria:**
- Either: a documented real `pi` session produced a `cost_entries` row as
  described above, or: a documented statement that no environment with
  `pi` installed was available to this pipeline, filed as an accepted risk.
- No source code changes result from this task unless verification
  surfaces an actual defect — in which case, stop and report the defect
  rather than silently patching around it, since the requirements and
  scope review both concluded the extension's logic is already correct.

### Task 3: Regression check on existing collector tests
**Blocks:** none
**Blocked by:** Task 1 (run after, to confirm the doc-only change didn't
touch anything — it shouldn't, since no `.py` file changes)

- Run `pytest tests/test_cost_collection_service.py tests/test_cost_tracking.py`
  and confirm both pass unchanged.
- Do not introduce a JS/TS test framework for the extension (Section 2) —
  none exists anywhere in this repo (`frontend/package.json` has no `test`
  script either), and this is a 140-line extension with no logic change.

**Acceptance criteria:**
- `tests/test_cost_collection_service.py` and `tests/test_cost_tracking.py`
  pass with no modifications to their source or to the code they test.

## 8. Non-Goals (carried from requirements, restated for development phase)

- No changes to `PiJsonlCollector`, `ClaudeCodeCollector`, `CostEntry`
  schema, `cost_derivation.py`, or budget enforcement.
- No `session_id` field added anywhere (requirements §9 — deliberate,
  already-justified deviation from design.md's literal text).
- No new JS/TS test framework.
- No OpenCode/Codex collector work.
