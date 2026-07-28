---
type: security_review_result
feature_id: des-91c8-pi-extension
verdict: ACCEPTABLE
critical_count: 0
high_count_found: 1
high_count_fixed: 1
medium_count_open: 0
low_count_open: 0
---

# Security Review Report: Pi Cost Tracker Extension

**Feature:** des-91c8-pi-extension
**Feature Type: DATA_SERVICE** (internal cost-ingestion consumer; this feature adds no new HTTP endpoint and no new auth flow — it only changes fallback-collection logic and a README on top of the already-reviewed `POST /api/autopilot/cost-entries` endpoint)
**Scope reviewed (this feature's actual diff vs. `main`):** `extensions/hephaestus-cost-tracker/README.md`, `src/services/cost_collection_service.py` (`collect_task_cost`), `tests/test_cost_collection_service.py`. Also re-traced, without re-litigating, the security-relevant paths this diff sits on top of: `POST /api/autopilot/cost-entries` (`src/mcp/autopilot_api.py:2144`), `verify_agent_authentication`/`_check_rate_limit` (`src/mcp/server.py`), `record_cost` (`src/core/cost_derivation.py`), and `extensions/hephaestus-cost-tracker/src/index.ts` — all unchanged by this feature and already covered by the prior `des-91c8-cost-collectors` security review (same file, superseded by this run).

## Automated Scan Results
`.hephaestus/ash_results.txt`: scan completed (not timed out), 542 actionable findings — but this is a whole-repo scan (`source-dir: '.'`), not scoped to this feature's 3-file diff, and the detailed per-file SARIF/JSON reports were not persisted in this worktree (`.ash/ash_output/` absent) to cross-reference. By category: bandit 305 medium (whole `src/`), checkov 2 critical (IaC — this feature adds no IaC), detect-secrets 50 critical (whole repo; manually grepped this feature's changed files for secret/token/credential patterns — only `*_tokens` LLM-usage-count fields matched, no actual secrets), npm-audit 181 critical/medium/low (frontend `node_modules` — this feature's only TS file, `extensions/hephaestus-cost-tracker/src/index.ts`, has zero runtime dependencies, confirmed via `package.json`, unchanged by this diff), semgrep 4 critical (whole repo). None of these categories apply to this feature's actual changes; treated as pre-existing/out-of-scope, consistent with the prior sibling review's treatment of ash noise.

## Summary
- Critical vulnerabilities found: 0
- High vulnerabilities found: 1 — **FIXED** in this pass
- Medium vulnerabilities: 0 open
- Low vulnerabilities: 0 open (prior findings from `des-91c8-cost-collectors` — ticket-6b452476, ticket-5c041735 — unchanged, not re-litigated)
- Overall security posture: **ACCEPTABLE** — the one High finding (a new consequence of this feature's own B-1 double-counting fix) was fixed in code this pass; the deeper systemic root cause it rests on is ticketed for separate follow-up.

## High Vulnerability Found and Fixed

### 1. Fake `source="pi"` `CostEntry` could suppress real JSONL fallback cost tracking for any task
- **Type:** Broken access control → cost-visibility / budget-enforcement bypass
- **File:** `src/services/cost_collection_service.py:439-451` (`collect_task_cost`) — new code from this feature's B-1 fix (adversarial review, commit `2f9fc73`)
- **Description:** The B-1 fix checked `db.query(CostEntry).filter_by(task_id=task_id, source="pi").first()` to decide whether the pi extension already posted real-time costs for a task, and if so permanently skipped the JSONL fallback tailer for that task — a one-shot decision made exactly once, at task completion, with no retry path (`src/services/task_completion_service.py::collect_cost_on_completion`). This check only tested "does any such row exist," not whether it actually originated from the agent assigned to that task. `POST /api/autopilot/cost-entries` accepts `task_id`, `agent_id`, and `source` as caller-supplied body fields, gated only by `verify_agent_authentication` (`src/mcp/server.py:475`) — an identity check that trusts any `sdk-`/`mcp-`-prefixed ID or `KNOWN_SYSTEM_AGENTS` unconditionally, or any `Agent` row with status `idle`/`working`/`starting`, and does not require the header's identity to match the body's `task_id`/`agent_id`. Both `task_id` and `agent_id` are enumerable via the existing, unauthenticated `GET /api/tasks` and `GET /api/agents` endpoints (`src/mcp/api.py`), so no privileged knowledge is needed to target a victim task. Any caller that clears the identity check could POST one minimal entry (`{"source": "pi", "task_id": "<victim-task>", "cost_usd": 0.0}`) for a task it doesn't own, permanently and silently suppressing all further real cost collection — and the budget rollups derived from it (`_check_budget_enforcement`, `src/core/cost_derivation.py`) — for that task.
- **Impact:** Cost-visibility / budget-enforcement bypass, deterministic and reproducible with a single forged HTTP request (not timing-dependent), scoped to one task at a time. Rated High: the combination of (a) both identifying fields being freely enumerable, (b) authentication being an identity check rather than a binding check, and (c) the suppression being permanent/one-shot with real budget-enforcement consequences, together clear the bar for a directly exploitable authorization gap rather than a theoretical one.
- **Fix Applied:** The existence check now also requires the `CostEntry` to belong to the task's assigned agent — `db.query(CostEntry).filter_by(task_id=task_id, agent_id=agent.id, source="pi")` — instead of matching on `task_id` alone (`src/services/cost_collection_service.py:447-451`). A cost entry posted under an unrelated `agent_id` for the same `task_id` can no longer be mistaken for proof this task's own session already reported in, so it no longer suppresses the fallback. Covered by a new regression test, `test_unrelated_agent_entry_does_not_suppress_fallback` (`tests/test_cost_collection_service.py`), which posts a forged entry under a different agent and asserts the JSONL fallback still runs and records the task's real costs.
- **Status:** FIXED. **Residual, out-of-scope root cause ticketed:** this scoped fix narrows the blast radius but doesn't close the underlying gap — `POST /api/autopilot/cost-entries` still doesn't bind the caller-supplied `agent_id`/`task_id` to the authenticated `X-Agent-ID` identity at all, so a caller that also knows/forges the victim's actual assigned `agent_id` (itself enumerable) could still reproduce the original issue. That fix belongs in `src/mcp/autopilot_api.py`'s request handling, which is outside this feature's file scope (`docs/architecture.md` limits this feature to the fallback-collection logic and README). Filed as `ticket-5a75167a-27d3-4a9a-bb01-0409bd128cd7` (High priority).

## Authentication Review
No new endpoint, no new auth code in this diff. `collect_task_cost` runs server-side on task completion (not directly caller-triggered); its existence-check reads `CostEntry` rows written through the already-reviewed, unchanged `verify_agent_authentication` gate on `POST /api/autopilot/cost-entries`. The fix above adds an ownership check (`agent_id` match) on top of that pre-existing identity gate; it does not change the gate itself. See High finding above and `ticket-5a75167a-27d3-4a9a-bb01-0409bd128cd7` for the remaining endpoint-level gap.

## Authorization Review
Otherwise unchanged from prior review: no per-project/per-workflow authorization scoping on cost data; any authenticated agent can read or write any entity's cost rows. This feature does not add or remove any authorization boundary beyond the one narrow ownership check added in the fix above — it only changes which of two existing collection paths (real-time POST vs. JSONL tail) writes the data for a given task.

## Input Validation Review
This feature's diff does not touch `CostEntryCreate` (`src/mcp/autopilot_api.py:1663-1720`, unchanged) — `source` enum, `cost_usd` bounds (0–$1000), token-count bounds (0–10M), and `raw_usage` size cap all still apply as verified in the prior review. The JSONL-fallback path (`collect_task_cost`'s `record_cost()` calls) doesn't go through `CostEntryCreate` at all — it calls `record_cost()` directly, which re-validates `cost_usd` bounds server-side independent of the Pydantic layer (`src/core/cost_derivation.py:76-81`), so the fallback path isn't a validation-bypass route.

## Data Handling Review
`collect_task_cost`'s per-entry try/except (B-2 fix, lines 535-560) logs failures at `logger.error` with the exception string and an 8-char-truncated `task_id` — consistent with the rest of the codebase's truncated-ID logging convention, no raw cost/token data or secrets logged. The broad `except Exception` only catches `Exception` (not `BaseException`), so it can't mask `KeyboardInterrupt`/`SystemExit`; each failure is logged individually rather than silently swallowed, which is an improvement over the pre-fix behavior (whole-batch silent rollback with only a `logger.warning` at the caller).

## Secret Management Review
Grepped this feature's 3 changed files (`cost_collection_service.py`, `index.ts`, `test_cost_collection_service.py`, `README.md`) for `password|secret|api[_-]?key|token|credential` — only matches are LLM `*_tokens` usage-count fields (input/output/cache/reasoning token counts), not credentials. No hardcoded secrets introduced.

## Dependency Audit
No `package.json`/`requirements`/lockfile changes in this diff. `extensions/hephaestus-cost-tracker/package.json` unchanged: zero runtime dependencies, one build-time devDependency (`typescript@^5.0.0`). No new supply-chain surface.

## OWASP Top 10 Considerations
| Category | Status | Notes |
|---|---|---|
| A01 Broken Access Control | ✅ Fixed this pass | High finding above — task-scoped cost-tracking suppression, fixed by adding an agent-ownership check; residual endpoint-level gap ticketed |
| A02 Cryptographic Failures | N/A | No crypto in this diff |
| A03 Injection | ✅ | ORM-only query (`filter_by`), no string-built SQL |
| A04 Insecure Design | ⚠️ Noted | Per-task (not per-turn) provenance check is still coarse by design even after the agent-ownership fix; tracked in `ticket-5a75167a` and adversarial review's W-1 |
| A05 Security Misconfiguration | N/A | Unchanged by this diff |
| A06 Vulnerable Components | ✅ | No dependency changes |
| A07 Identity & Auth Failures | ⚠️ Pre-existing, partially mitigated | Spoofable `X-Agent-ID` is the known systemic issue (SEC-03/ticket-6b452476); this pass adds an ownership check that narrows one consequence of it without fixing the identity model itself (`ticket-5a75167a`) |
| A08 Software & Data Integrity | ✅ Improved | B-1/B-2 fixes remove double-counting and whole-batch loss; this pass's fix removes the narrower single-task suppression gap those introduced |
| A09 Security Logging Failures | ✅ | Per-entry failures now logged individually (`logger.error`), an improvement over pre-fix silent batch rollback |
| A10 SSRF | N/A | No user-controlled URLs in this diff |

## Verdict
**ACCEPTABLE — approved to proceed.** One High-severity finding (task-scoped cost-tracking suppression via a forged real-time `CostEntry`, a side effect of this feature's own B-1 double-counting fix) was identified and **fixed in code** this pass (`src/services/cost_collection_service.py`, new regression test in `tests/test_cost_collection_service.py`). The deeper root cause — `POST /api/autopilot/cost-entries` not binding caller-supplied `task_id`/`agent_id` to the authenticated identity — remains open and is ticketed (`ticket-5a75167a-27d3-4a9a-bb01-0409bd128cd7`, High priority) since fixing it requires changes to `src/mcp/autopilot_api.py`, outside this feature's file scope per `docs/architecture.md`. No regressions to the authentication, rate-limiting, or input-validation controls reviewed and fixed in the prior `des-91c8-cost-collectors` pass — all confirmed still present and unchanged.

## Re-review Addendum (post qa_validation/doc_review/forensics_analysis)
Re-checked the diff since this report was last written (commit `de8eaab`). Only one code change landed in the interim: `extensions/hephaestus-cost-tracker/src/index.ts` (commit `fc7d41f`) updated `ctx.ui.setStatus(message)` calls to the corrected two-argument signature `setStatus(key, text)` the `pi` extension API actually expects. Both arguments are a fixed string literal (`'cost-tracker'`) and either a static status string or a locally-computed cost float (`this.sessionCost.toFixed(2)`) — no user- or network-controlled input reaches this call, so it introduces no new attack surface. All other changes in this window (`docs/qa_validation/`, `docs/product_validation/`, `docs/doc_review_report.md`, `docs/forensics_report.md`, `docs/code_summary.md`, `docs/feature_report.html`) are documentation only. No change to this verdict.
