---
type: code_summary
feature_id: des-91c8-cost-collectors
---

# Code Summary: CLI Cost Collectors (Pi + Claude Code)

**Feature ID:** des-91c8-cost-collectors

This feature closes the gap between the already-implemented real-time pi
cost-tracker extension (source code merged by an earlier feature) and it
actually being installed and correctly configured. It's packaging work, not
new collection logic — the `PiJsonlCollector`/`ClaudeCodeCollector` runtime,
the `CostEntry` schema, and `POST /cost-entries` were untouched.

## `scripts/install.sh`

Adds a new step inside the existing pi-detection block (`if command -v pi ...
|| [ -d ~/.pi ]`), placed after the pi-mcp-adapter install and before the
"Restart Pi" log line — the same branch every other pi-specific install step
already lives in, so there's one place to look for "what happens when pi is
present."

The step copies `extensions/hephaestus-cost-tracker/` to
`~/.pi/agent/extensions/hephaestus-cost-tracker/` and runs `npm install &&
npm run build` there, producing `dist/index.js`, the file pi loads as an
extension on next launch. It always re-runs on both fresh install and
`--update` (no "already installed" skip-gate), which is what makes
`--update` refresh a stale build. Every failure path (`npm` missing, write
failure, build failure) degrades to a `warn` and continues install rather
than aborting — the JSONL-tailing fallback in
`cost_collection_service.py` still collects the same cost data, just not in
real time, so a broken extension build was never meant to be fatal.

## `extensions/hephaestus-cost-tracker/README.md`

Fixed the documented default `HEPHAESTUS_API_URL` from `http://localhost:8000`
to `http://localhost:8300`, matching both the extension's actual code
default (`src/index.ts`) and `hephaestus_config.yaml`'s `port: 8300`. The old
value would have pointed a correctly-installed extension at the wrong port,
silently dropping every cost POST.

## `extensions/hephaestus-cost-tracker/package.json`

Added `@types/node` to `devDependencies`. Without it, `tsc` fails on
`process.env`, `console`, and `fetch` type references — a real
build-breaking bug caught by QA, not a style fix. This is what made the new
`install.sh` build step (above) actually able to succeed.

## `src/mcp/autopilot_api.py`

`POST /cost-entries`'s rate limiter was keyed on the caller-supplied
`X-Agent-ID` header. `verify_agent_authentication()` trusts any
`sdk-`/`mcp-`-prefixed ID unconditionally (it's an identity check, not a
secret), and the server binds `0.0.0.0`, so a caller could reset the
60/minute rate-limit bucket on every request just by rotating the header
value. Since each cost entry can carry `cost_usd` up to $1000 and drives
budget-pause rollups, an attacker with a real `task_id`/`workflow_id` could
have forced premature budget pausing; against unknown IDs, unbounded DB
writes. Fixed by keying the rate limit on `request.client.host` instead,
which required adding a `request: Request` parameter to the endpoint. Found
and fixed during security review, scoped appropriately since this new
extension is the endpoint's new traffic source.

## Tests

No new tests were added — this feature explicitly doesn't touch collector
logic. `tests/test_cost_collection_service.py` (20/20) and the broader
budget/cost-tracking suite (`tests/test_budget_enforcement_integration.py`,
`tests/test_cost_tracking.py`, 56/56) were run as regression checks and pass
unchanged.
