# Feature: Pi Cost Tracker Extension

## Overview
Build a pi extension at `extensions/hephaestus-cost-tracker/` (TypeScript) that hooks `turn_end` events to capture `message.usage.cost.total` in real-time from the provider response, POSTs each turn's cost to Hephaestus's cost-ingestion API endpoint (from cost-collectors feature), and optionally displays running cost in the pi TUI status bar via `ctx.ui.setStatus()`. The extension reads `session_id` from the pi session via `ctx.sessionManager` and includes it in the POST for correct task attribution. Installed globally at `~/.pi/agent/extensions/hephaestus-cost-tracker/` by `scripts/install.sh` when pi is detected. This eliminates the need for the `SessionCostCheckpoint` mechanism for pi sessions (real-time POST means no file tailing), though the Python JSONL fallback remains for sessions without the extension.

## Files Owned
- `extensions/hephaestus-cost-tracker/`

## Dependencies
- `cost-collectors` — requires the cost-ingestion API endpoint at `POST /api/cost`

## Implementation Notes

### Extension structure
```
extensions/hephaestus-cost-tracker/
  extension.ts       # Main extension file (pi extension API)
  package.json       # npm package metadata
  README.md          # Installation and configuration docs
```

### Core logic
1. Register a `turn_end` hook in the pi extension API
2. In the hook callback, extract usage data from the turn's assistant response:
   - `usage.cost.total` → `cost_usd`
   - `usage.input`, `usage.output`, `usage.cacheRead`, `usage.cacheWrite` → token fields
   - Response model/provider info → `model`, `source: "pi"`
3. Read `session_id` from `ctx.sessionManager` (pi's built-in session manager)
4. POST to `${HEPHAESTUS_API_URL}/api/cost` (default `http://localhost:8080`) with:
   ```json
   {
     "session_id": "<session_id>",
     "source": "pi",
     "model": "<model from response>",
     "input_tokens": <input>,
     "output_tokens": <output>,
     "cache_read_tokens": <cacheRead>,
     "cache_write_tokens": <cacheWrite>,
     "cost_usd": <cost.total>,
     "raw_usage": { ...full usage object }
   }
   ```
5. `task_id` is NOT included — the server-side cost-ingestion endpoint resolves task_id from session_id via the `session_cost_checkpoints` / task metadata; the pi extension doesn't know the task context

### TUI status bar
- Use `ctx.ui.setStatus()` to show running session cost: e.g. `💰 $0.43`
- Accumulate `cost_usd` locally per session and update on each `turn_end`
- Make this configurable via extension settings (on/off)

### Installation
- `scripts/install.sh` (existing) should be updated to detect pi installation and copy the extension to `~/.pi/agent/extensions/hephaestus-cost-tracker/`
- Extension auto-activates when pi loads it; no explicit registration needed

### Fallback
When the extension is NOT loaded (standalone pi sessions outside Hephaestus, or if the extension fails to load), the JSONL-tailing Python collector from `cost-collectors` still works as a fallback. The two mechanisms are complementary, not exclusive.

## Acceptance Criteria
- [ ] Extension directory exists at `extensions/hephaestus-cost-tracker/` with `extension.ts` and `package.json`
- [ ] Extension hooks `turn_end` and extracts cost data from the assistant response
- [ ] Extension POSTs cost data to Hephaestus's `/api/cost` endpoint with session_id and usage fields
- [ ] Extension handles network failures gracefully (log warning, don't crash pi)
- [ ] TUI status bar shows accumulated session cost (configurable)
- [ ] `scripts/install.sh` installs the extension when pi is detected