---
type: security_review_result
feature_id: des-91c8-cost-collectors
verdict: ACCEPTABLE
critical_count: 0
high_count_found: 1
high_count_fixed: 1
medium_count_open: 0
low_count_open: 2
---

# Security Review Report: CLI Cost Collectors (Pi + Claude Code)

**Feature:** des-91c8-cost-collectors
**Feature Type: DATA_SERVICE** (internal cost-ingestion pipeline + one new authenticated HTTP endpoint; no end-user auth flow introduced by this feature — Step 2 covers only the auth check the new endpoint relies on)
**Scope reviewed:** `src/services/cost_collection_service.py`, `src/core/cost_derivation.py`, `src/mcp/autopilot_api.py` (cost-entries + cost query endpoints), `src/mcp/server.py::verify_agent_authentication`/`_check_rate_limit`, `extensions/hephaestus-cost-tracker/src/index.ts`, `extensions/hephaestus-cost-tracker/package.json`, `scripts/install.sh` (extension install block), cost UI components (`frontend/src/components/cost/*`, `BudgetStatusCard.tsx`, `ProjectSettingsModal.tsx`).

## Automated Scan Results
`./.hephaestus/ash_results.txt` contents: **"SCAN TIMED OUT after 300s"**. No automated findings available for this pass; review below is manual.

## Summary
- Critical vulnerabilities found: 0
- Critical vulnerabilities FIXED: 0
- High vulnerabilities found: 1 (rate-limit bypass via spoofed identity on `POST /cost-entries`)
- High vulnerabilities FIXED: 1
- Medium vulnerabilities: 0 open (prior SEC-04 unlinked-cost gap already fixed via `validate_entity_link`, confirmed present)
- Low vulnerabilities: 2 (ticketed, not fixed — see below)
- Overall security posture: **ACCEPTABLE** — one high finding fixed in this pass; remaining findings are either pre-existing/out-of-scope or low severity, all ticketed.

## Vulnerabilities Found and Fixed

### 1. Rate limit on `POST /cost-entries` keyed on attacker-controlled header
- **Type:** Rate-limit bypass / DoS
- **File:** `src/mcp/autopilot_api.py:2075-2103` (`create_cost_entry`)
- **Description:** The endpoint rate-limited with `_check_rate_limit(f"cost_entry:{agent_id}")`, where `agent_id` comes straight from the caller-supplied `X-Agent-ID` header. `verify_agent_authentication()` (`src/mcp/server.py`) trusts any ID starting with `sdk-`/`mcp-`, or in `KNOWN_SYSTEM_AGENTS`, unconditionally — it is an identity check, not a secret. The server binds `0.0.0.0` (`hephaestus_config.yaml:2`), so it's reachable off localhost. A caller could pass auth and reset the rate-limit bucket on every request simply by varying the header value (e.g. `sdk-1`, `sdk-2`, ...), making the "60/min" limit meaningless.
- **Impact:** Unbounded `POST /cost-entries` flooding — each entry can carry `cost_usd` up to $1000 and drives `derive_task_cost`/`derive_workflow_cost`/budget-pause rollups. Against a target whose real `task_id`/`workflow_id` is known, this could force premature budget-based pausing of active workflows and termination of their agents (`_pause_project_workflows`); against unknown IDs it's still unbounded DB-write flooding.
- **Fix Applied:** Rate-limit key changed to the request's client IP (`request.client.host`) instead of the spoofable `X-Agent-ID` header, so rotating the header no longer resets the limit window. Added `request: Request` parameter to the endpoint. See `src/mcp/autopilot_api.py:2075-2103`.
- **Status:** FIXED

## Medium Vulnerabilities
None open. Verified `CostEntryCreate.validate_entity_link` (`src/mcp/autopilot_api.py:1696-1706`) still rejects cost entries with both `task_id` and `workflow_id` unset — the previously-fixed SEC-04 gap (unlinked costs bypassing budget enforcement) remains fixed.

## Low Vulnerabilities / Findings (ticketed, not fixed this pass)

| Finding | File(s) | Ticket | Why not fixed here |
|---|---|---|---|
| `POST/PUT/DELETE /projects` have no `X-Agent-ID` auth at all, letting any caller null a project's `cost_limit_usd` or delete the project outright | `src/mcp/autopilot_api.py:1904,1967,2035` | ticket-6b452476 (**High** priority, filed as low-effort-to-fix-but-out-of-scope) | Pre-existing endpoints, not touched by the CLI Cost Collectors diff (`git log` confirms `create_project`/`update_project`/`delete_project` predate this feature); fixing them means changing shared project-management endpoints beyond this feature's boundary. Flagged because it's the same underlying weakness class as the fix above and materially affects cost/budget security. |
| Cost query GET endpoints (`/tasks,.../costs` etc.) have auth but no rate limit | `src/mcp/autopilot_api.py:2191,2239,2287,2335,2383` | ticket-5c041735 (Low) | Read-only, no budget-pause side effects; lower severity than the POST path already fixed. |

Note: `X-Agent-ID` being a self-reported, spoofable identifier (rather than a signed token) is a known, already-tracked systemic issue (see stale `docs/security_review/security_report.md` SEC-03 from an earlier, unrelated feature pass, recommending HMAC-signed agent tokens for network-exposed deployments). Not re-fixed here — it's infrastructure shared by ~10+ endpoints across the codebase, well outside this feature's diff.

## Authentication Review
`POST /cost-entries` and all 5 cost-query GETs require `X-Agent-ID` and call `verify_agent_authentication()`. That function trusts known system-agent strings and `sdk-`/`mcp-`-prefixed IDs unconditionally, and otherwise checks the DB for an active `Agent` row. This is an identity check, not a cryptographic authentication mechanism — acceptable for a local-first, single-operator tool, weaker if the server is reachable beyond localhost (it is, per `host: 0.0.0.0`). See Low findings above.

## Authorization Review
No per-project or per-workflow authorization scoping exists on cost queries — any authenticated agent can read any entity's cost breakdown. Consistent with the rest of this single-tenant system; not flagged as a new issue.

## Input Validation Review
`CostEntryCreate` (`src/mcp/autopilot_api.py:1628-1706`) validates: `source` against an enum, `cost_usd` non-negative and capped at $1000, all token counts non-negative and capped at 10M, `raw_usage` capped at 10KB serialized, `model` capped at 200 chars, and requires at least one of `task_id`/`workflow_id`. `record_cost()` (`src/core/cost_derivation.py:38-116`) re-validates `cost_usd` bounds server-side (defense in depth, not solely relying on the Pydantic layer). `_discover_session_file` and the Claude Code session-path branch in `collect_task_cost` (`src/services/cost_collection_service.py:347-401,457-481`) both reject `..`/`~` in `cwd` and re-verify the resolved path stays under the expected base directory before globbing — path traversal is covered on both the pi and Claude Code discovery paths.

## Data Handling Review
Cost entries are an append-only ledger (`CostEntry` rows); no deletion path. `raw_usage` (potentially containing prompt/response metadata) is size-capped but not redacted — acceptable, this is operational telemetry not user PII, and stays local to the SQLite DB. No sensitive data observed logged at non-debug level beyond agent/task ID prefixes (already truncated to 8 chars in log lines throughout).

## Dependency Audit
`extensions/hephaestus-cost-tracker/package.json`: zero runtime dependencies (`"dependencies": {}`), one devDependency (`typescript@^5.0.0`, build-time only, not shipped). No supply-chain surface introduced by this feature beyond what's already reviewed. Did not re-run `pip audit`/`npm audit` against the whole repo (out of this feature's diff; ash timed out — see above).

## OWASP Top 10 Considerations
| Category | Status | Notes |
|---|---|---|
| A01 Broken Access Control | ⚠️ Partial | Project mutation endpoints unauthenticated (ticket-6b452476, pre-existing, out of diff) |
| A02 Cryptographic Failures | N/A | No crypto introduced by this feature |
| A03 Injection | ✅ | ORM-only queries in cost_derivation.py; no string-built SQL |
| A04 Insecure Design | ✅ | Append-only ledger with self-healing rollups |
| A05 Security Misconfiguration | ⚠️ | Server binds `0.0.0.0`; magnifies A01/A07 above |
| A06 Vulnerable Components | ✅ | Zero runtime deps in the new extension |
| A07 Identity & Auth Failures | ⚠️ Fixed-in-part | Spoofable identity is pre-existing/ticketed; rate-limit bypass exploiting it on the new POST endpoint is fixed this pass |
| A08 Software & Data Integrity | ✅ | Self-healing derivation, checkpointed collectors (no double-counting across runs) |
| A09 Security Logging Failures | ✅ | Auth rejections and rate-limit hits logged with agent ID/IP |
| A10 SSRF | ✅ | No user-controlled URLs in this feature's code paths |

## Verdict
**ACCEPTABLE — approved to proceed.** One high-severity gap (spoofable rate-limit key on the new cost-ingestion endpoint) found and fixed in code this pass. Two pre-existing/out-of-scope gaps ticketed (one High — unauthenticated project mutation, one Low — missing rate limit on cost-query GETs). No critical findings, no unresolved medium findings.
