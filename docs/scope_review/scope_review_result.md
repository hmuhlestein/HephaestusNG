---
type: scope_review_result
verdict: PASS
out_of_scope: []
missing: []
correction_instructions: ""
---

# Scope Review: Pi Cost Tracker Extension

**Reviewed:** `docs/requirements_analysis.md` against `.hephaestus/design.md` lines 621–647 ("Pi Extension Collector" section).

## Methodology

Line-by-line comparison of every requirement in `requirements_analysis.md` against the design doc's Pi Extension Collector section, plus spot-checking factual claims (`README.md` content, `index.ts` code, `install.sh` wiring, `autopilot_api.py` route, `database.py` schema) directly against the repository.

## Traceability Matrix

| Design.md Element | Lines | Requirements FR | Status |
|---|---|---|---|
| Pi extension hooks `turn_end`, captures `usage.cost.total` | 622-625 | FR-2 | ✅ Covered (verification) |
| No file-system access needed, extension runs inside pi process | 626-627 | Info only | ✅ Correct context |
| Real-time TUI display via `ctx.ui.setStatus()` | 628-629 | FR-2 | ✅ Covered (verification) |
| No checkpoint table needed for pi | 630-631 | §4/§9 implicit | ✅ Correctly excluded |
| Extension reads `session_id` via `ctx.sessionManager` | 638-639 | §1 deviation noted | ✅ Justified deviation (env vars) |
| JSONL tailing fallback when extension not loaded | 640-642 | FR-3 | ✅ Covered (verification) |
| Installed globally by `scripts/install.sh` | 644 | FR-2 | ✅ Verified: `install.sh:783-806` |
| Default API URL `localhost:8080` | 646 | N/A | ✅ Corrected to `8300` in code |
| POSTs cost to Hephaestus API | 633-634 | FR-1 | ✅ Covered |

## Findings

### No Missing Requirements

Every requirement described in design.md's Pi Extension Collector section (lines 621-647) is either covered by a functional requirement in `requirements_analysis.md`, correctly treated as verification-only (FR-2, FR-3), or correctly excluded as belonging to an already-merged parent feature (data model, derivation, budget enforcement).

### No Out-of-Scope Additions

All requirements trace cleanly to design.md's Pi Extension Collector section:
- **FR-1** (fix README POST path): corrects a documentation discrepancy to match the code that implements the design doc's cost-posting requirement (lines 633-634). In scope.
- **FR-2** (pi install verification): traces directly to lines 622-624 and 644. Verification-only.
- **FR-3** (regression test): traces to lines 640-642 (JSONL fallback). Verification-only.

### Correctly Excluded Items

- Schema changes (`CostEntry`, `SessionCostCheckpoint`) — parent feature, merged.
- `cost_derivation.py` — parent feature, merged.
- Budget enforcement (`cost_limit_usd`, `_pause_project_workflows`) — parent feature, merged.
- Claude Code/OpenCode/Codex collectors — design.md Non-Goals section, separately deferred.
- JS/TS test framework — correctly excluded per project conventions (NFR §5).

### Justified Deviation

The requirements doc explicitly documents one deviation from design.md's literal text: cost attribution uses `HEPHAESTUS_AGENT_ID`/`TASK_ID`/`WORKFLOW_ID` environment variables instead of `session_id` read via `ctx.sessionManager` (design.md lines 638-639). This is properly justified in §1 and §9 — `CostEntry` has no `session_id` column, and all other collectors use the same env-var attribution. Not a scope defect.

### Informational Note (Not a Scope Defect)

**Already-shipped code correction:** FR-1 states that `README.md:44` says `POST /cost-entries` and needs fixing to `POST /api/autopilot/cost-entries`. However, this fix already shipped in a prior merged commit (`618804b`). Current `README.md:44` already reads `POST /api/autopilot/cost-entries`. This is a stale factual claim in the requirements doc, not a scope error — FR-1 is correctly framed in scope (bringing docs in line with design.md), it just happens to already be resolved. If the development phase re-runs FR-1, it will find no change needed, which is the correct outcome for a gate that permits passing through already-complete work.

## Verdict

**PASS.** No requirement in `requirements_analysis.md` lacks a traceable basis in design.md's Pi Extension Collector section. No requirement from that section is missing from `requirements_analysis.md`. The requirements faithfully represent the design doc scope — no additions, no omissions.