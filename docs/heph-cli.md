# heph — Hephaestus CLI

Unified command-line interface for the Hephaestus multi-agent workflow engine.

## Installation

### Remote install (no repo needed)

```bash
curl -sSL https://raw.githubusercontent.com/hmuhlestein/HephaestusNG/main/scripts/install.sh | bash
```

Options:
```bash
# Custom install location
curl -sSL .../install.sh | bash -s -- --prefix ~/my-heph

# Include dev tools (pytest, black, mypy)
curl -sSL .../install.sh | bash -s -- --dev

# Skip Docker/Qdrant setup
curl -sSL .../install.sh | bash -s -- --skip-docker
```

### Local install (from cloned repo)

```bash
git clone https://github.com/hmuhlestein/HephaestusNG.git
cd Hephaestus
./scripts/install.sh
```

### Add to PATH

After installation, add `heph` to your shell:

```bash
# Temporary (current session)
export PATH="$HOME/.hephaestus/.venv/bin:$PATH"

# Permanent (add to ~/.zshrc or ~/.bashrc)
echo 'export PATH="$HOME/.hephaestus/.venv/bin:$PATH"' >> ~/.zshrc
```

### Updating

```bash
scripts/install.sh --update
```

---

## Quick Start

```bash
# Check system health
heph status

# Start all services (backend, monitor, frontend, Qdrant)
heph start

# Test service connectivity
heph exec test

# Run the autopilot pipeline
heph autopilot start --project-path ~/my-project
```

---

## Commands

### Service Lifecycle

```bash
heph status                    # Health check across all services
heph start                     # Start backend, monitor, frontend, Qdrant
heph start --backend-only      # Start only the backend
heph start --no-frontend       # Skip frontend dashboard
heph start --reload            # Enable auto-reload for development
heph stop                      # Stop all services (graceful)
heph stop --force              # Force kill (SIGKILL)
heph restart                   # Stop + start
heph init                      # Initialize database and Qdrant collections
heph init --drop               # Drop and recreate (backs up first)
```

### Workflow Management

```bash
heph workflow list              # List registered workflow definitions
heph workflow executions        # List workflow executions
heph workflow executions --status completed  # Filter by status
heph workflow launch <id> -d "Build auth system"  # Launch a workflow
heph workflow launch <id> -d "..." --path ~/project  # With working directory
heph workflow status <exec-id>  # Get execution details
```

### Agent Management

```bash
heph agent list                 # List all agents
heph agent list --status working  # Filter by status
heph agent logs <agent-id>      # Get agent output
heph agent terminate <agent-id> # Terminate an agent
heph agent message <agent-id> "focus on auth first"  # Send message
```

### Task Management

```bash
heph task list                  # List recent tasks
heph task list --status pending # Filter by status
heph task list --limit 50       # More results
heph task inspect <task-id>     # Detailed task info
heph task create "Add rate limiting" --priority high  # Create task
```

### Autopilot Pipeline

```bash
# Start the continuous pipeline
heph autopilot start --project-path ~/my-project

# Custom design queue location
heph autopilot start --project-path ~/my-project --design-queue ./designs

# More iterations per design
heph autopilot start --project-path ~/my-project --max-iterations 5

# Stop the pipeline
heph autopilot stop

# Check pipeline status
heph autopilot status

# View design queue
heph autopilot queue --project-path ~/my-project

# Add a design document
heph autopilot add my-feature.md --project-path ~/my-project
```

### Knowledge Base (Memory)

```bash
# Search for relevant memories
heph memory search "authentication patterns"

# Save a discovery
heph memory save "Rate limiter should use sliding window" --type discovery --tags api security

# Filter by memory type
heph memory search "architecture" --type decision
```

### Service Testing (exec)

```bash
# Run a shell command and capture output to a log file
heph exec run pytest tests/test_vector_store.py
heph exec run python scripts/smoke_test.py --cwd ~/my-project --timeout 60
heph exec run ls -la --log /tmp/my-test.log

# Ping the backend
heph exec ping

# List available MCP tools
heph exec endpoints

# Execute an MCP tool directly
heph exec tool search_memory --args '{"query": "auth"}'

# Raw API request
heph exec raw GET /api/tasks
heph exec raw POST /api/tickets/create --data '{"title": "test"}'
```

### Configuration

```bash
# Show current config
heph config show

# Show config file paths
heph config path
```

---

## Global Options

```bash
heph --help                     # Show help
heph --version                  # Show version
heph --json status              # Output as JSON (for scripting)
heph --host 10.0.0.1 --port 9000 status  # Custom backend address
```

---

## JSON Output

All commands support `--json` for machine-readable output:

```bash
heph --json status | jq '.agents.total'
heph --json task list --status failed | jq '.[].description'
heph --json exec test
```

---

## Configuration

Hephaestus reads configuration from:

1. Environment variables
2. `.env` file (project root)
3. `hephaestus_config.yaml` (project root)

Key environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `openrouter` | LLM provider |
| `LLM_MODEL` | `xiaomi/mimo-v2.5` | Default model |
| `OPENROUTER_API_KEY` | — | OpenRouter API key |
| `VECTOR_STORE_BACKEND` | `turbovec` | Vector store (`turbovec` or `qdrant`) |
| `EMBEDDING_BACKEND` | `fastembed` | Embedding provider |
| `MCP_PORT` | `8000` | Backend port |
| `LITELLM_PROXY_URL` | — | LiteLLM proxy for cost tracking |
| `LITELLM_API_KEY` | — | LiteLLM virtual key |
| `LITELLM_MASTER_KEY` | — | LiteLLM admin key for spend queries |
| `LITELLM_COST_TRACKING` | `false` | Enable per-feature cost tracking |

---

## Architecture

```
heph
├── main.py              # Entry point, argument parsing
├── utils/               # Shared helpers (API, PID, formatting)
│   └── __init__.py
└── commands/            # One module per command group
    ├── status.py        # heph status
    ├── start.py         # heph start
    ├── stop.py          # heph stop
    ├── restart.py       # heph restart
    ├── init.py          # heph init
    ├── workflow.py       # heph workflow
    ├── agent.py         # heph agent
    ├── task.py          # heph task
    ├── autopilot.py     # heph autopilot
    ├── memory.py        # heph memory
    ├── exec_cmd.py      # heph exec
    └── config.py        # heph config
```

Each command module exposes:
- `register(subparsers)` — adds its subcommand to argparse
- `run(args)` — executes the command

---

## Process Management

`heph start` tracks spawned processes via PID files in `~/.hephaestus/pids/`:

```
~/.hephaestus/pids/
├── backend.pid
├── monitor.pid
├── frontend.pid
└── orchestrator.pid
```

`heph stop` and `heph autopilot stop` read these PIDs and send SIGTERM,
then SIGKILL after 5 seconds. No pattern matching against process names.
