---
type: product_validation_result
feature_id: des-91c8-cost-collectors
verdict: PASS
blocker_count: 0
requirements_met: 4
requirements_total: 4
---

# Product Validation Report: CLI Cost Collectors (Pi + Claude Code)

**Feature ID:** des-91c8-cost-collectors
**Feature Name:** CLI Cost Collectors (Pi + Claude Code)
**Validation Date:** 2026-07-25
**Design Documents:** `.hephaestus/design.md` / `docs/COST_TRACKING_DESIGN.md` ("Collection Architecture", "Pi Extension Collector" sections)
**Requirements Document:** `docs/requirements_analysis.md`
**Architecture Document:** `docs/architecture.md`
**QA Report:** `docs/qa_validation/qa_report.md` (56/56 targeted tests, PASS)
**Security Report:** `docs/security_review/security_report.md` (1 High found and fixed, ACCEPTABLE)
**Prior Run:** None — first product_validation pass for this feature.
**Verdict:** **PASS** — 0 blockers, 4/4 requirements met, no regressions.

---

## 1. Executive Summary

This feature's premise (established during `product_requirements`, re-confirmed here) is that the underlying Pi and Claude Code cost collectors — `PiJsonlCollector`, `ClaudeCodeCollector`, `SessionCostCheckpoint`, the UUID5 Claude Code session-ID fix, and `POST /cost-entries` — were **already implemented, wired into `task_completion_service.py`, and tested** by prior merged features (Cost Tracking Database Schema → Cost Derivation Engine → Budget Enforcement and Pipeline Throttling). This feature's actual scope was narrower: make the design-specified real-time pi extension (`extensions/hephaestus-cost-tracker/`) actually installed and runnable, and fix two concrete defects found in the pre-existing extension source.

Diff vs. merge-base (`c3622c9`) confirms the implementation stayed inside that scope:
- `scripts/install.sh` — 28 new lines, gated inside the existing `if command -v pi ... || [ -d ~/.pi ]` block, installing/building the extension with graceful degradation on missing npm or build failure.
- `extensions/hephaestus-cost-tracker/package.json` — added missing `@types/node` devDependency (a real build-breaking bug QA caught: `tsc` failed on `process.env`/`console`/`fetch` symbols without it).
- `extensions/hephaestus-cost-tracker/README.md` — corrected the documented default `HEPHAESTUS_API_URL` from `8000` to `8300`, matching both the extension's actual code default and `hephaestus_config.yaml:3`.
- `src/mcp/autopilot_api.py` — one incidental, appropriately-scoped fix from security review: `POST /cost-entries`'s rate limit was keyed on the caller-supplied `X-Agent-ID` header (spoofable, since `verify_agent_authentication` trusts `sdk-`/`mcp-`-prefixed IDs unconditionally), letting an attacker reset the rate-limit bucket by rotating the header. Now keyed on `request.client.host`.

No other files were touched. No collector logic, schema, or derivation code was modified — consistent with the requirements' explicit non-goal of not touching that already-shipped code.

## 2. Functional Requirements Verification

| Requirement | Implementation | Status |
|---|---|---|
| FR-1: Pi extension installed automatically by `scripts/install.sh` when pi is detected | `scripts/install.sh:781-806`, inside the pre-existing `command -v pi \|\| [ -d ~/.pi ]` branch (line 569). Copies `extensions/hephaestus-cost-tracker/` to `~/.pi/agent/extensions/hephaestus-cost-tracker/`, runs `npm install --silent && npm run build`. Skips (with `warn`, not `err`) if npm is absent or the source dir is missing; does not abort the rest of install on build failure. | ✅ PASS |
| FR-2: `--update` path also refreshes the extension | The install block is unconditional (not gated on the `UPDATE` flag, unlike e.g. the venv/frontend-node_modules skip checks at lines 209/443) — every invocation of `install.sh`, including `--update`, re-runs `rm -rf` + copy + `npm install && npm run build`, so a `git pull` touching `index.ts` reaches an already-provisioned machine on the next install/update run. | ✅ PASS |
| FR-3: Fix `HEPHAESTUS_API_URL` default mismatch | `README.md` now documents `8300`; `index.ts:9`'s actual default is `8300`; `hephaestus_config.yaml:3` confirms `port: 8300`. All three agree. | ✅ PASS |
| FR-4: Existing collector behavior unchanged, still covered by tests | `git diff c3622c9..HEAD` touches zero lines in `src/services/cost_collection_service.py`, `src/core/cost_derivation.py` (collection logic), or the `cost_entries`/`session_cost_checkpoints` schema. `tests/test_cost_collection_service.py` — 20/20 pass, unmodified. | ✅ PASS |

**4 of 4 requirements met. 0 unmet.**

## 3. Non-Functional Requirements Verification

- **No blocking on install failure**: confirmed — every failure branch in the new `install.sh` block (missing npm, missing source dir, failed `rm -rf`/`mkdir`/`cp`, failed `npm install`/`npm run build`) calls `warn`, not `err`/`exit`, and explicitly logs "Cost data will still be collected via task-completion fallback." Script continues to the MCP-tools-restart message afterward.
- **No behavior change to existing collectors**: confirmed via diff — zero lines changed in the collector/derivation/schema files.
- **Idempotent re-install**: the block does `rm -rf "$EXT_DEST_DIR"` then `mkdir -p` then `cp -r` before building, so re-running against a machine that already has the extension installed cleanly replaces it rather than erroring on "already exists."

## 4. Security Verification

Security review found and fixed one High-severity issue introduced risk surface: the new `POST /cost-entries` traffic pattern (this endpoint is what the new pi extension calls on every LLM turn) made an existing rate-limit weakness materially more exploitable — keyed on a spoofable header, reachable off localhost (server binds `0.0.0.0`). Fixed by keying on `request.client.host` instead. Verified in code (`src/mcp/autopilot_api.py:2075-2103`) and via the security report's OWASP walkthrough (ACCEPTABLE posture, 0 open critical/medium findings). Two pre-existing, out-of-scope gaps were correctly ticketed rather than fixed inline (unauthenticated project-mutation endpoints — `ticket-6b452476`; missing rate limit on cost-query GETs — `ticket-5c041735`) — appropriate scope discipline, not a validation gap for this feature.

## 5. Integration With Existing System

- The extension reads `HEPHAESTUS_AGENT_ID`/`HEPHAESTUS_TASK_ID`/`HEPHAESTUS_WORKFLOW_ID` from environment variables already set by `src/agents/manager.py:481-484,1696-1699` when launching pi tmux sessions — no new plumbing needed on the Python side, confirmed present and unmodified.
- POSTs to `POST /cost-entries`, an endpoint that already existed and already fed the merged Cost Derivation Engine / Budget Enforcement rollup chain — the extension is purely an additional producer into an existing, tested consumer path. `SessionCostCheckpoint`-based JSONL tailing (`PiJsonlCollector`) remains as the fallback path when the extension isn't loaded, per the design's explicit "complementary, not exclusive" framing — verified this fallback is untouched.
- `scripts/install.sh`'s new block reuses the file's existing `log`/`ok`/`warn` helper conventions and sits inside the pre-existing pi-detection branch rather than adding a parallel detection mechanism.

## 6. User Experience / Operational Flow

- **Developer with pi installed, runs `./scripts/install.sh`**: extension is copied, built, and ready on next pi launch; sees `ok "Cost tracker extension installed"`. Verified against real `tsc` output in QA (after the `@types/node` fix) — build actually succeeds now, not just "doesn't error at the shell level."
- **Developer without pi**: no extension activity at all (branch never entered), matching the "cost tracking is a nice-to-have" non-functional requirement.
- **Developer with pi but no npm**: gets a clear `warn` explaining real-time tracking is skipped and that the fallback still collects cost data — no silent data loss, no confusing failure.
- **pi TUI user during a session**: per the (unmodified, previously-reviewed) extension source, sees a live `💰 $X.XX` status update per turn — this is the actual product value this feature was building toward; nothing in this pass altered that behavior, only made it reachable via normal install.

## 7. Edge Cases From Design Doc

- **"Extension not loaded" fallback** (design doc, Pi Extension Collector section): explicitly still works — `PiJsonlCollector` untouched, its own test suite (`TestPiJsonlCollector`, 6 tests) unmodified and passing.
- **Build failure shouldn't break `heph install`**: covered (see NFR section above) — verified by reading every exit path in the new bash block, all `warn`+continue, no `exit`.
- **Re-running install shouldn't corrupt an existing extension install**: covered by the `rm -rf` + `mkdir -p` + `cp -r` sequence before build.

## 8. Test Results

```
python -m pytest tests/test_cost_collection_service.py -q
20 passed

python -m pytest tests/ -k "cost_entr or rate_limit" -q
7 passed, 2076 deselected
```

27/27 targeted tests pass, 0 failures, no regressions. Consistent with QA's own run (`tests/test_budget_enforcement_integration.py tests/test_cost_tracking.py`, 56/56 passed) — different but overlapping test selection, same result: no failures anywhere in the cost-tracking test surface.

## 9. Recommendations for Human Reviewer

1. **No code changes needed before merge.** All 4 requirements met, security finding fixed, QA-caught build bug fixed, tests green.
2. **Manual smoke test worth doing once, not automatable in this pipeline**: run `./scripts/install.sh` on a real machine with `pi` and `npm` installed, confirm `~/.pi/agent/extensions/hephaestus-cost-tracker/dist/index.js` is produced and pi picks it up on next launch (shows `💰 Cost tracker active` in the status bar). This was verified in isolation during QA (`npm install && npm run build` succeeds) but not verified end-to-end through the actual `install.sh` invocation against a real pi installation — reasonable to defer to a human with a pi environment handy rather than block the pipeline on it.
3. **Two tickets already filed and out of this feature's scope** (`ticket-6b452476` — unauthenticated project-mutation endpoints, High priority; `ticket-5c041735` — missing rate limit on cost-query GETs, Low) — worth prioritizing `ticket-6b452476` in a near-term follow-up given it touches the same `cost_limit_usd` field this feature's traffic ultimately feeds into, but that's a separate feature, not a blocker here.

## 10. Verdict

**PASS.** 0 blockers, 4/4 functional requirements met, all non-functional requirements verified, security finding resolved, no regressions in 27 targeted tests. Ready to proceed to `doc_review`.
