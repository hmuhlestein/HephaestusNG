# Tmux Session Viewer

A standalone, reusable tmux session viewer with a Python backend (FastAPI) and React frontend. View and interact with any tmux session's terminal output in real time through a web UI.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    React Frontend                        │
│                                                          │
│  ┌──────────────────┐    ┌────────────────────────────┐  │
│  │ RealTimeAgent     │    │ ObservabilityPanel          │  │
│  │ Output (modal)    │    │ (grid panel for multi-agent)│  │
│  └────────┬─────────┘    └────────────┬───────────────┘  │
│           │                           │                   │
│  ┌────────▼───────────────────────────▼───────────────┐  │
│  │              Hooks Layer                            │  │
│  │  useRealTimeAgentOutput    useMultiAgentOutput      │  │
│  │  (single agent polling)    (multi-agent staggered)  │  │
│  └────────────────────┬───────────────────────────────┘  │
│                       │                                  │
│  ┌────────────────────▼───────────────────────────────┐  │
│  │               API Service (tmuxApi)                 │  │
│  └────────────────────┬───────────────────────────────┘  │
└───────────────────────┼──────────────────────────────────┘
                        │ HTTP polling (1s intervals)
┌───────────────────────┼──────────────────────────────────┐
│                  FastAPI Backend                          │
│  ┌────────────────────▼───────────────────────────────┐  │
│  │              REST Router (/api/sessions/*)          │  │
│  └────────────────────┬───────────────────────────────┘  │
│  ┌────────────────────▼───────────────────────────────┐  │
│  │           TmuxSessionManager                        │  │
│  │  ┌─────────────────────────────────────────────┐   │  │
│  │  │              libtmux.Server                   │   │  │
│  │  └─────────────────────────────────────────────┘   │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│                    tmux server (OS)                       │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐                │
│  │ session1 │ │ session2 │ │ session3 │  ...            │
│  └──────────┘ └──────────┘ └──────────┘                │
└──────────────────────────────────────────────────────────┘
```

### Data Flow

1. **Backend creates tmux sessions** via `libtmux.Server()` - each session runs a detached terminal
2. **Output capture** uses `pane.cmd("capture-pane", "-p", "-S -{lines}")` to read terminal scrollback
3. **Message injection** uses `pane.send_keys(text, enter=True)` to type into the terminal
4. **REST API** exposes `/api/sessions/{name}/output` and `/api/sessions/{name}/send`
5. **React hooks** poll the API every 1 second and manage connection/retry state
6. **React components** render the output in a terminal-styled `<pre>` block with auto-scroll, search, and controls

## Directory Structure

```
tmux-viewer/
├── README.md                          # This file
├── backend/
│   ├── __init__.py                    # Package init
│   ├── tmux_manager.py                # Core TmuxSessionManager class
│   ├── api.py                         # FastAPI router with REST endpoints
│   └── requirements.txt               # Python dependencies
├── frontend/
│   └── src/
│       ├── index.ts                   # Public exports
│       ├── types.ts                   # TypeScript interfaces
│       ├── services/
│       │   └── api.ts                 # API client (axios)
│       ├── hooks/
│       │   ├── useRealTimeAgentOutput.ts   # Single-session polling hook
│       │   └── useMultiAgentOutput.ts      # Multi-session staggered polling
│       └── components/
│           ├── RealTimeAgentOutput.tsx     # Modal viewer with send-message
│           └── ObservabilityPanel.tsx      # Compact panel for grid layouts
└── examples/
    └── integration_example.py         # Quick-start FastAPI server
```

## Backend: `TmuxSessionManager`

The core Python class wrapping `libtmux`. No database, no agent dependencies - just pure tmux control.

### API

```python
from tmux_viewer.backend import TmuxSessionManager

manager = TmuxSessionManager(session_prefix="myapp")

# Create a session
session = manager.create_session(
    session_name="worker-1",
    working_directory="/path/to/project",
    env_vars={"API_KEY": "xxx"},
)

# Read terminal output (last 500 lines)
output = manager.get_output("worker-1", lines=500)

# Send keystrokes into the terminal
manager.send_message("worker-1", "npm test", enter=True)

# Check if session exists
if manager.session_exists("worker-1"):
    print("alive")

# List all sessions with a prefix
sessions = manager.list_sessions(prefix="myapp")

# Kill a session
manager.kill_session("worker-1")
```

### REST Endpoints

Mount the router in any FastAPI app:

```python
from fastapi import FastAPI
from tmux_viewer.backend.api import router as tmux_router

app = FastAPI()
app.include_router(tmux_router, prefix="/api")
```

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/sessions` | List sessions (optional `?prefix=`) |
| `POST` | `/api/sessions` | Create session `{session_name, working_directory?, env_vars?}` |
| `DELETE` | `/api/sessions/{name}` | Kill a session |
| `GET` | `/api/sessions/{name}/output?lines=2000` | Capture terminal output |
| `POST` | `/api/sessions/{name}/send` | Send message `{message, enter?}` |
| `GET` | `/api/sessions/{name}/exists` | Check if session exists |

## Frontend Components

### `RealTimeAgentOutput`

Full-screen modal viewer for a single session. Features:
- Real-time output polling (1s interval)
- Auto-scroll with manual override
- Search/filter output lines
- Copy to clipboard
- Send messages to the running agent
- Pause/resume updates
- Keyboard shortcuts (Space=pause, Ctrl+F=search, Ctrl+R=retry, Escape=close)

```tsx
import { RealTimeAgentOutput } from './tmux-viewer/frontend/src';

<RealTimeAgentOutput
  agent={{ id: "abc-123", status: "working", tmux_session_name: "my-session", ... }}
  onClose={() => setShowViewer(false)}
/>
```

### `ObservabilityPanel`

Compact panel for embedding in grid layouts. Features:
- Compact header with status indicator
- Local pause control (independent of global pause)
- Fullscreen toggle
- Hide/remove from grid

```tsx
import { ObservabilityPanel, useMultiAgentOutput } from './tmux-viewer/frontend/src';

const { outputs } = useMultiAgentOutput(["agent-1", "agent-2"]);

<div className="grid grid-cols-2 gap-4">
  {Object.entries(outputs).map(([id, data]) => (
    <ObservabilityPanel key={id} agent={agents[id]} output={data} />
  ))}
</div>
```

### Hooks

#### `useRealTimeAgentOutput(agentId, options)`
Single-session polling hook.

```tsx
const { output, isConnected, lastUpdateTime, retry } = useRealTimeAgentOutput(
  "abc-123",
  { updateInterval: 1000, enabled: true }
);
```

#### `useMultiAgentOutput(agentIds, options)`
Multi-session polling with staggered fetches and change detection.

```tsx
const { outputs, stats, retryAgent } = useMultiAgentOutput(
  ["agent-1", "agent-2", "agent-3"],
  { updateInterval: 1000, staggerInterval: 100 }
);
```

## Integration into a Different Project

### 1. Backend Integration

**Install dependencies:**
```bash
pip install libtmux fastapi uvicorn
```

**Copy the backend directory** into your project, then mount the router:

```python
# your_app/main.py
from fastapi import FastAPI
from tmux_viewer.backend.api import router as tmux_router, init_manager
from tmux_viewer.backend.tmux_manager import TmuxSessionManager

app = FastAPI()
manager = TmuxSessionManager(session_prefix="yourapp")
init_manager(manager)
app.include_router(tmux_router, prefix="/api")
```

**Or use `TmuxSessionManager` directly** without FastAPI:

```python
from tmux_viewer.backend import TmuxSessionManager

manager = TmuxSessionManager()
manager.create_session("my-worker", working_directory="/path/to/project")
output = manager.get_output("my-worker", lines=500)
manager.send_message("my-worker", "ls -la")
```

### 2. Frontend Integration

**Install peer dependencies:**
```bash
npm install react react-dom framer-motion lucide-react date-fns axios
npm install -D @types/react @types/react-dom typescript
```

**Copy the frontend `src/` directory** into your project, then import:

```tsx
import {
  RealTimeAgentOutput,
  ObservabilityPanel,
  useRealTimeAgentOutput,
  useMultiAgentOutput,
  tmuxApi,
} from '@/tmux-viewer/frontend/src';
```

**Configure the API base URL** if your backend is on a different port:

```typescript
// In your API service or proxy config
const tmuxApi = axios.create({
  baseURL: 'http://localhost:8080/api',
});
```

### 3. Standalone Usage (FastAPI Server)

Run the example server directly:

```bash
cd tools/tmux-viewer
pip install -r backend/requirements.txt
python examples/integration_example.py
# Server at http://localhost:8080
# API docs at http://localhost:8080/docs
```

### 4. Proxy Configuration

If your frontend runs on a different port, configure a proxy:

```json
// vite.config.ts or package.json proxy
{
  "proxy": {
    "/api": "http://localhost:8080"
  }
}
```

## Dependencies

### Backend
- `libtmux >= 0.23.0` - Python tmux server bindings
- `fastapi >= 0.104.0` - REST API framework
- `uvicorn >= 0.24.0` - ASGI server

### Frontend
- `react >= 18` - UI framework
- `framer-motion >= 10` - Animations
- `lucide-react >= 0.263` - Icons
- `date-fns >= 2.29` - Date formatting
- `axios >= 1.6` - HTTP client

### System
- `tmux` must be installed on the host (`brew install tmux` on macOS, `apt install tmux` on Linux)
