---
type: security_review_result
feature_id: des-91c8-pi-extension
verdict: ACCEPTABLE
critical_count: 0
high_count_found: 0
medium_count_open: 1
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
- High vulnerabilities found: 0
- Medium vulnerabilities: 1 (new, found this pass — ticketed, not fixed; see below)
- Low vulnerabilities: 0 new (prior findings from `des-91c8-cost-collectors` — ticket-6b452476, ticket-5c041735 — unchanged, not re-litigated)
- Overall security posture: **ACCEPTABLE** — no critical/high findings; one new medium-severity data-integrity/budget-bypass gap identified and ticketed, consistent with this feature's narrow, docs-and-fallback-logic scope.

## Medium Vulnerability Found (ticketed, not fixed this pass)

### 1. Fake `source="pi"` `CostEntry` can suppress real JSONL fallback cost tracking for any task
- **Type:** Broken access control → cost-visibility / budget-enforcement bypass
- **File:** `src/services/cost_collection_service.py:447-451` (`collect_task_cost`) — new code from this feature's B-1 fix (adversarial review, commit `2f9fc73`)
- **Description:** The B-1 fix checks `db.query(CostEntry).filter_by(task_id=task_id, source="pi").first()` to decide whether the pi extension already posted real-time costs for a task, and if so permanently skips the JSONL fallback tailer for that task. This check only tests "does any such row exist," not whether it actually originated from the extension instance running for that task's assigned agent. `POST /api/autopilot/cost-entries` accepts `task_id`, `agent_id`, and `source` as caller-supplied body fields, gated only by `verify_agent_authentication` (`src/mcp/server.py:475`) — an identity check that trusts any `sdk-`/`mcp-`-prefixed ID or `KNOWN_SYSTEM_AGENTS` unconditionally and does not require the header's identity to match the body's `task_id`/`agent_id`. Any caller that clears that identity check can POST one minimal entry (`{"source": "pi", "task_id": "<victim-task>", "cost_usd": 0.01}`) for a task it doesn't own, permanently and silently suppressing all further real cost collection — and the budget rollups derived from it — for that task.
- **Impact:** Cost-visibility / budget-enforcement bypass, scoped to one task at a time. Same pre-existing weak-identity trust model already tracked as SEC-03 in this file's prior revision and `ticket-6b452476` (unauthenticated project mutation) — this is a new consequence of that known gap, not a new trust boundary. Rated Medium, not High: requires the caller to already clear identity check and know the target `task_id`, and the effect is under-counting for one task rather than the unbounded flooding/DoS the prior sibling review's High finding covered.
- **Fix status:** Not fixed this pass — out of this feature's scope per `docs/architecture.md` (explicitly excludes changes to budget enforcement / `cost_derivation.py`), and the minimal correct fix (per-turn provenance tracking, per adversarial review's own W-1 recommendation) is a larger design change than this feature's boundary allows. **Ticketed:** `ticket-9259ff95-51d2-4662-8b79-9923e44a01b1` (medium priority).

## Authentication Review
No new endpoint, no new auth code in this diff. `collect_task_cost` runs server-side on task completion (not directly caller-triggered); its new existence-check reads `CostEntry` rows written through the already-reviewed, unchanged `verify_agent_authentication` gate on `POST /api/autopilot/cost-entries`. See Medium finding above for the one new consequence of that pre-existing identity model surfaced by this feature's fix.

## Authorization Review
Unchanged from prior review: no per-project/per-workflow authorization scoping on cost data; any authenticated agent can read or write any entity's cost rows. This feature does not add or remove any authorization boundary — it only changes which of two existing collection paths (real-time POST vs. JSONL tail) writes the data for a given task.

## Input Validation Review
This feature's diff does not touch `CostEntryCreate` (`src/mcp/autopilot_api.py:1663-1720`, unchanged) — `source` enum, `cost_usd` bounds (0–$1000), token-count bounds (0–10M), and `raw_usage` size cap all still apply as verified in the prior review. The JSONL-fallback path (`collect_task_cost`'s `record_cost()` calls) doesn't go through `CostEntryCreate` at all — it calls `record_cost()` directly, which re-validates `cost_usd` bounds server-side independent of the Pydantic layer (`src/core/cost_derivation.py:76-81`), so the fallback path isn't a validation-bypass route.

## Data Handling Review
`collect_task_cost`'s new per-entry try/except (B-2 fix, lines 535-556) logs failures at `logger.error` with the exception string and an 8-char-truncated `task_id` — consistent with the rest of the codebase's truncated-ID logging convention, no raw cost/token data or secrets logged. The broad `except Exception` only catches `Exception` (not `BaseException`), so it can't mask `KeyboardInterrupt`/`SystemExit`; each failure is logged individually rather than silently swallowed, which is an improvement over the pre-fix behavior (whole-batch silent rollback with only a `logger.warning` at the caller).

## Secret Management Review
Grepped this feature's 3 changed files (`cost_collection_service.py`, `index.ts`, `test_cost_collection_service.py`, `README.md`) for `password|secret|api[_-]?key|token|credential` — only matches are LLM `*_tokens` usage-count fields (input/output/cache/reasoning token counts), not credentials. No hardcoded secrets introduced.

## Dependency Audit
No `package.json`/`requirements`/lockfile changes in this diff. `extensions/hephaestus-cost-tracker/package.json` unchanged: zero runtime dependencies, one build-time devDependency (`typescript@^5.0.0`). No new supply-chain surface.

## OWASP Top 10 Considerations
| Category | Status | Notes |
|---|---|---|
| A01 Broken Access Control | ⚠️ New consequence, ticketed | Medium finding above — task-scoped cost-tracking suppression via the pre-existing weak-identity model |
| A02 Cryptographic Failures | N/A | No crypto in this diff |
| A03 Injection | ✅ | ORM-only query (`filter_by`), no string-built SQL |
| A04 Insecure Design | ⚠️ Noted | Per-task (not per-turn) provenance check is coarse by design; tracked in the ticket above and adversarial review's W-1 |
| A05 Security Misconfiguration | N/A | Unchanged by this diff |
| A06 Vulnerable Components | ✅ | No dependency changes |
| A07 Identity & Auth Failures | ⚠️ Pre-existing, surfaced | Spoofable `X-Agent-ID` is the known systemic issue (SEC-03/ticket-6b452476) this finding builds on |
| A08 Software & Data Integrity | ⚠️ Partial | B-1/B-2 fixes improve integrity (no more double-counting, no more whole-batch loss) but introduce the narrower single-task suppression gap above |
| A09 Security Logging Failures | ✅ | Per-entry failures now logged individually (`logger.error`), an improvement over pre-fix silent batch rollback |
| A10 SSRF | N/A | No user-controlled URLs in this diff |

## Verdict
**ACCEPTABLE — approved to proceed.** No critical or high findings. One new medium-severity finding (task-scoped cost-tracking suppression, a narrower side effect of this feature's own B-1 double-counting fix) identified, documented, and ticketed (`ticket-9259ff95-51d2-4662-8b79-9923e44a01b1`); not fixed in this pass because the correct fix requires per-turn provenance tracking, which is a larger design change than this feature's docs-and-fallback-logic scope permits per `docs/architecture.md`. No regressions to the authentication, rate-limiting, or input-validation controls reviewed and fixed in the prior `des-91c8-cost-collectors` pass — all confirmed still present and unchanged.
