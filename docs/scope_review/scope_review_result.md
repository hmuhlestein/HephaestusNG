---
type: scope_review_result
verdict: PASS
out_of_scope: []
missing: []
correction_instructions: ""
summary: "requirements_analysis.md traces cleanly to design.md's Pi Extension Collector section; no additions, no omissions"
---

# Scope Review: Pi Cost Tracker Extension

`docs/requirements_analysis.md` declares its scope as design.md's "Pi Extension Collector" section (lines 621-647); everything else in design.md belongs to already-merged parent features (Cost Tracking Database Schema → Cost Derivation Engine → Budget Enforcement and Pipeline Throttling → CLI Cost Collectors (Pi + Claude Code)), which the requirements doc correctly treats as existing context rather than new scope. Confirmed `git log main..HEAD` has exactly one commit ahead of `main` (the requirements-analysis write itself), consistent with that framing.

Compared each functional requirement against design.md and spot-checked the requirements doc's factual claims directly against the repo rather than trusting them: `scripts/install.sh:783-806` does have the cost-tracker install/build wiring; `extensions/hephaestus-cost-tracker/README.md:44` does say `POST /cost-entries`; `extensions/hephaestus-cost-tracker/src/index.ts:123` does POST to `${apiUrl}/api/autopilot/cost-entries`; `src/mcp/autopilot_api.py:2144` does define `@router.post("/cost-entries")` with `X-Agent-ID` auth; `src/core/database.py:1230`'s `CostEntry` has no `session_id` column. All check out.

FR-1 (fix README's `POST /cost-entries` → `POST /api/autopilot/cost-entries`) has no literal design.md sentence but is a correction to the extension's own documentation, bringing it in line with design.md's description of the extension posting cost entries to Hephaestus's API (lines 633-634) — in scope, not new functionality. FR-2 (verify the extension loads and runs under a real `pi` install) traces to design.md lines 622-624 (`turn_end` hook, `usage.cost.total` capture) and is verification-only. FR-3 (re-run existing tests, confirm no regression) traces to design.md lines 640-642 (JSONL-tailing fallback must keep working when the extension isn't loaded) and is verification-only.

The one deliberate deviation from design.md's literal text — attributing cost by `HEPHAESTUS_AGENT_ID`/`TASK_ID`/`WORKFLOW_ID` env vars instead of the `session_id` design.md describes reading via `ctx.sessionManager` (lines 638-639) — is explicitly surfaced and justified in requirements_analysis.md §9 (no `session_id` column exists on `CostEntry`; every other collector attributes the same way), not silently dropped. Out-of-scope items (schema/derivation/budget-enforcement changes, OpenCode/Codex collectors, a new JS/TS test framework) are correctly excluded and correctly attributed to already-merged parent features or design.md's own Non-Goals section.

One informational note for the architecture phase, not a scope defect: design.md line 646 states the extension's default API URL as `http://localhost:8080`, but the actual and correct value, confirmed in `hephaestus_config.yaml:3`, `index.ts:58`, and `README.md:31`, is `8300` everywhere — a stale/typo'd number inside design.md itself. requirements_analysis.md reports the real value correctly; it just doesn't call out that design.md's text differs. Does not change any requirement or the verdict.

No requirement in requirements_analysis.md lacks a traceable basis in design.md's Pi Extension Collector section. No requirement from that section is missing from requirements_analysis.md. **Verdict: PASS.**
