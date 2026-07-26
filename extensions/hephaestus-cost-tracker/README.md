# Hephaestus Cost Tracker

A pi extension that provides real-time cost tracking for LLM API calls made through Hephaestus agents.

## Features

- **Real-time cost display**: Shows running cost in pi TUI status bar
- **Automatic cost capture**: Posts each turn's cost to Hephaestus API
- **Session tracking**: Maintains running total for the session
- **Graceful degradation**: Never blocks pi on API failures

## Installation

1. Copy this directory to `~/.pi/agent/extensions/hephaestus-cost-tracker/`

2. Build the extension:
   ```bash
   cd ~/.pi/agent/extensions/hephaestus-cost-tracker
   npm install
   npm run build
   ```

3. The extension will be automatically loaded by pi on next startup.

## Configuration

Set environment variables before launching pi:

```bash
# Hephaestus API URL (default: http://localhost:8300)
export HEPHAESTUS_API_URL=http://localhost:8300

# Agent/Task/Workflow IDs for cost attribution (optional)
export HEPHAESTUS_AGENT_ID=your-agent-id
export HEPHAESTUS_TASK_ID=your-task-id
export HEPHAESTUS_WORKFLOW_ID=your-workflow-id
```

## How It Works

1. On each LLM turn completion, the extension extracts cost data from `message.usage.cost.total`
2. The cost is added to a running session total
3. The TUI status bar is updated: `💰 $1.23`
4. The cost entry is posted to Hephaestus API (`POST /api/autopilot/cost-entries`)
5. Hephaestus derives and rolls up costs through the entity hierarchy

## Fallback Behavior

If the extension is not loaded, Hephaestus falls back to JSONL tailing:
- Costs are collected when tasks complete (not real-time)
- `SessionCostCheckpoint` prevents double-counting
- Same data accuracy, just delayed timing

## Development

```bash
# Watch mode
npm run dev

# Build
npm run build
```
