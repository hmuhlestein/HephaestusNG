# HephaestusNG Testing Guide

**Version:** 1.0.0  
**Date:** July 2026  
**Status:** Active

> **Parent Document:** See [docs/ARCHITECTURE_REVIEW.md](./ARCHITECTURE_REVIEW.md) for system architecture.  
> **Design Documents:** See [config/workflows/autopilot/](./config/workflows/autopilot/) for phase definitions.

> **Do not run the full test suite (`pytest tests/`) by default.** It's slow and touches
> unrelated modules. Run only targeted tests for the files you actually changed
> (`pytest tests/test_<module>.py`, or `-k <pattern>`). Only run the full suite if the
> user explicitly asks for it.

---

## Table of Contents

1. [Test Environment](#1-test-environment)
2. [Test Categories](#2-test-categories)
3. [Running Tests](#3-running-tests)
4. [Backend Tests](#4-backend-tests)
5. [Frontend Tests](#5-frontend-tests)
6. [Integration Tests](#6-integration-tests)
7. [Performance Testing](#7-performance-testing)
8. [Known Issues & Gotchas](#8-known-issues--gotchas)

---

## 1. Test Environment

### Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| **Node.js** | 18+ | Frontend build & tests |
| **Python** | 3.12+ | Backend tests |
| **SQLite** | 3.35+ | Test database |
| **tmux** | 3.2+ | Agent sessions |

### Running Tests

```bash
# Run all tests (with libtmux plugin disabled)
python -m pytest tests/ -p no:libtmux -q

# Run with coverage
python -m pytest tests/ -p no:libtmux --cov=src --cov-report=html

# Run specific test file
python -m pytest tests/test_phase_manager.py -p no:libtmux -v

# Run tests matching pattern
python -m pytest tests/ -p no:libtmux -k "test_adversarial"

# Run with verbose output
python -m pytest tests/ -p no:libtmux -v

# Run async tests
python -m pytest tests/ -p no:libtmux --asyncio-mode=auto
```

### Services Required

| Service | Port | Purpose | Status Check |
|---------|------|---------|--------------|
| SQLite | N/A | Test database (in-memory) | N/A |
| tmux | N/A | Agent sessions | `tmux list-sessions` |

### Environment Variables

```bash
# Backend test environment
PROJECT_PATH=/path/to/test/project
VECTOR_STORE_BACKEND=turbovec
EMBEDDING_BACKEND=fastembed
```

---

## 2. Test Categories

### Test Pyramid

```
         /\
        /  \        E2E Tests (10%)
       /    \       - Full pipeline runs
      /------\
     /        \     Integration Tests (20%)
    /          \    - API endpoints + DB
   /------------\
  /              \  Unit Tests (70%)
 /                \ - Business logic, services
/------------------\
```

### 1. Unit Tests

**Location:** `tests/`  
**Command:**
```bash
python -m pytest tests/ -p no:libtmux -v
```

Tests individual components in isolation:
- Phase manager (evaluation, goto, retry)
- Orchestrator helpers (sweep, status derivation)
- Task completion service (verification, forensics)
- Task enrichment service (LLM integration)
- Status derivation (design/feature status)
- Agent manager (lifecycle, termination)
- Transcript processing (ANSI stripping, TUI chrome filtering)

### 2. Integration Tests

**Location:** `tests/integration/`  
**Command:**
```bash
python -m pytest tests/integration/ -p no:libtmux -v
```

Tests API endpoints with real database:
- MCP server endpoints
- Autopilot API
- Ticket system
- Project management

### 3. Frontend Tests

**Location:** `frontend/`  
No automated test suite exists yet (no `vitest.config.ts`, no
`@testing-library/*`, no `test` script) — see §5 for what's actually
available today.
**Command:**
```bash
cd frontend && npx tsc --noEmit  # Type checking -- the only automated frontend check that exists
cd frontend && npm run build     # tsc && vite build -- also catches build-time errors tsc alone won't
```

---

## 3. Running Tests

### Quick Smoke Test

```bash
# Run just the core tests (fast)
python -m pytest tests/test_phase_manager.py tests/test_status_derivation.py tests/test_transcript_processing.py -p no:libtmux -q
```

### Full Test Suite

Avoid this unless explicitly requested — prefer the targeted commands above for the
files you're actually working on.

```bash
# Run all tests (may take several minutes)
python -m pytest tests/ -p no:libtmux -q
```

### Test File Naming

```
# Backend (pytest)
test_<module>.py
test_<feature>.py

# Frontend: no test suite exists yet (see §5) -- no naming convention to follow
```

### Common Commands

```bash
# Run failed tests only
python -m pytest tests/ -p no:libtmux --lf

# Run tests in parallel
python -m pytest tests/ -p no:libtmux -n auto

# Debug test
python -m pytest tests/ -p no:libtmux --pdb

# Run tests matching pattern
python -m pytest tests/ -p no:libtmux -k "phase_manager and not integration"
```

---

## 4. Backend Tests

### Project Structure

```
tests/
├── conftest.py                  # Shared fixtures
├── test_phase_manager.py        # Phase lifecycle tests
├── test_orchestrator_helpers.py # Orchestrator sweep tests
├── test_status_derivation.py    # Status derivation tests
├── test_transcript_processing.py # TUI chrome filtering
├── test_task_completion_service.py # Task completion tests
├── test_autopilot_api.py        # Autopilot API tests
├── test_mcp_server_tickets.py   # Ticket system tests
├── integration/
│   ├── test_task_deduplication_flow.py
│   └── test_validation_flow.py
└── unit/
    └── test_task_similarity_service.py
```

### Writing Backend Tests

#### Unit Test Example

```python
# tests/test_phase_manager.py
import pytest
from unittest.mock import MagicMock, patch
from src.phases.phase_manager import PhaseManager

class TestPhaseManager:
    @pytest.fixture
    def phase_manager(self, mock_db):
        return PhaseManager(db_manager=mock_db)
    
    def test_handle_evaluation_goto_resets_stale_executions(self, phase_manager, mock_session):
        """Test that goto resets all stale executions at/after target phase."""
        # Arrange
        phase = MagicMock()
        phase.workflow_id = "wf-123"
        phase.order = 5
        
        target_phase = MagicMock()
        target_phase.order = 3
        target_phase.name = "development"
        
        # Act
        result = phase_manager._handle_evaluation_goto(
            mock_session, phase, MagicMock(), "summary",
            MagicMock(target_phase="development")
        )
        
        # Assert
        assert result["action"] == "goto"
        assert result["target_phase"] == "development"
```

#### Integration Test Example

```python
# tests/test_autopilot_api.py
import pytest
from fastapi.testclient import TestClient
from src.mcp.server import app

@pytest.fixture
def client():
    return TestClient(app)

class TestAutopilotAPI:
    def test_get_autopilot_status(self, client, auth_headers):
        """Test GET /api/autopilot/status."""
        response = client.get(
            "/api/autopilot/status",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "running" in data
        assert "queue_depth" in data
```

### Mocking External Services

```python
# Mocking LLM responses
@pytest.fixture
def mock_llm():
    with patch("src.interfaces.llm_client.LangChainLLMClient") as mock:
        instance = MagicMock()
        instance.generate.return_value = "Mocked LLM response"
        mock.return_value = instance
        yield mock

# Mocking tmux sessions
@pytest.fixture
def mock_tmux():
    with patch("src.agents.manager.AgentManager") as mock:
        instance = MagicMock()
        instance.create_session.return_value = MagicMock()
        mock.return_value = instance
        yield mock
```

---

## 5. Frontend Tests

**No automated frontend test suite exists in this repo.** There's no
`vitest.config.ts`, no `frontend/tests/`, no `@testing-library/*` in
`package.json`, and no `test`/`lint` npm scripts — `npm run lint` and
`npm test` both fail with "missing script." What actually exists today:

### Project Structure

```
frontend/
├── src/
│   ├── components/
│   ├── hooks/
│   ├── services/
│   └── types/
├── package.json      # scripts: dev, build, preview, type-check
└── vite.config.ts
```

### Running Frontend Checks

```bash
# Type checking -- the only automated frontend verification that exists
cd frontend && npx tsc --noEmit

# Production build -- also catches build-time errors tsc alone won't
cd frontend && npm run build
```

The practical verification path for a frontend change is `tsc --noEmit` +
`npm run build` for correctness, and the Playwright-driven browser check
below for anything visual.

### Visual/Browser Verification (Playwright)

`tsc --noEmit` and `vite build` catch type and syntax errors, not rendering
bugs — a component can type-check cleanly and still render the wrong
color, drop dark-mode support, or silently fail to mount. CLAUDE.md's rule
for UI changes ("start the dev server and use the feature in a browser
before reporting the task as complete") means an actual headless-browser
check, not just a clean build.

**Toolchain**: `chromium-cli` isn't set up in this repo. Node Playwright
isn't installed as a project dependency either — installing it fresh pulls
a ~150MB Chromium binary. **Python Playwright is the path of least
resistance** if it's already on the machine (`pip install playwright &&
playwright install chromium`, or check first — `pyenv versions` /
`which playwright` — before installing a duplicate copy). It drives the
exact same Chromium build; only the driver language differs.

```bash
# Find an existing Python Playwright install before installing a new one
which playwright
pyenv which playwright 2>/dev/null   # if using pyenv, the CLI may be a shim
python3 -c "import playwright" 2>&1  # confirm the module resolves in that interpreter
ls ~/Library/Caches/ms-playwright/   # confirms browser binaries are already downloaded
```

**Dev server**: `frontend/vite.config.ts` proxies `/api` and `/ws` to
`localhost:${BACKEND_PORT:-8300}` regardless of which port Vite itself
picks — so a second frontend instance (e.g. because port 5300 is already
taken by a running `heph start`) still talks to the same real backend and
real data. Don't kill someone else's dev server to free the default port;
let Vite pick the next one and read it from its own stdout.

```bash
cd frontend
(npm run dev > /tmp/vite_dev.log 2>&1 &)
timeout 30 bash -c 'until curl -sf http://localhost:5301 >/dev/null 2>&1; do sleep 1; done'
grep "Local:" /tmp/vite_dev.log   # confirm the actual port Vite chose

# When done, stop ONLY the instance you started -- never a broad pkill:
lsof -ti:5301 -sTCP:LISTEN | xargs -r kill
```

**Driver script** — launch, navigate, screenshot both themes, check for
runtime errors:

```python
from playwright.sync_api import sync_playwright

BASE = "http://localhost:5301"
errors = []

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    for theme in ["light", "dark"]:
        ctx = browser.new_context(color_scheme=theme, viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{BASE}/tasks", wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(1500)  # let the first data fetch settle
        page.screenshot(path=f"/tmp/tasks_{theme}.png")
        ctx.close()
    browser.close()

print("console errors:", errors or "(none)")
```

Run it with whichever interpreter actually has the `playwright` module —
that may not be the `python3` on `PATH` (e.g. a pyenv-managed version):
`/path/to/pyenv/versions/3.x.y/bin/python3 driver.py`.

**Don't just eyeball the screenshot for dark mode — a color can look
"dark enough" at thumbnail scale while the `dark:` Tailwind classes never
actually applied** (e.g. this project's dark mode is class-based, toggled
by a `dark` class on `<html>`, not `prefers-color-scheme` alone — a
component with no wiring to that toggle would still get
`color-scheme: dark` from Playwright's context but never receive the
class). Confirm the classes actually took effect:

```python
info = page.eval_on_selector(
    "span.rounded-full",
    "el => ({class: el.className, bg: getComputedStyle(el).backgroundColor})"
)
print(info)  # bg should be the dark-mode color, not the light one
```

**No live data for the state you need to check?** Don't skip verification
— render the component directly with `react-dom/server`, using `esbuild`
to transpile on the fly (no build step, no test framework needed):

```javascript
// run from frontend/ so node_module resolution works: node render_check.mjs
import { renderToStaticMarkup } from 'react-dom/server';
import React from 'react';
import esbuild from 'esbuild';
import { createRequire } from 'module';
const require = createRequire(import.meta.url);

function loadComponent(tsxPath, exportName) {
  const result = esbuild.buildSync({
    entryPoints: [tsxPath], bundle: true, write: false, format: 'cjs',
    platform: 'node', jsx: 'automatic',
    external: ['react', 'react-dom', 'clsx', 'lucide-react'],
    loader: { '.tsx': 'tsx' },
  });
  const mod = { exports: {} };
  new Function('module', 'exports', 'require', result.outputFiles[0].text)(
    mod, mod.exports, (id) => (id === 'react' ? React : require(id))
  );
  return mod.exports[exportName] ?? mod.exports.default;
}

const Component = loadComponent('./src/components/StatusBadge.tsx', 'default');
console.log(renderToStaticMarkup(React.createElement(Component, { status: 'failed', size: 'sm' })));
```

Diff the emitted HTML/class string against the pre-change component's
output (or against a hand-computed expected class list) — this catches
prop-plumbing and conditional-class bugs a screenshot of unrelated states
can't.

**Cleanup**: kill only the dev-server port you opened, and delete any
scratch driver scripts/screenshots when done — they don't belong in the
repo or its scratchpad past the session that needed them.

---

## 6. Integration Tests

### API Integration Tests

```bash
# Run integration tests (requires test database)
python -m pytest tests/integration/ -p no:libtmux -v
```

### MCP Server Tests

```python
# tests/test_mcp_server_tickets.py
import pytest
from fastapi.testclient import TestClient
from src.mcp.server import app

@pytest.fixture
def client():
    return TestClient(app)

class TestMCPServerTickets:
    def test_create_ticket(self, client, auth_headers):
        """Test POST /api/tickets."""
        response = client.post(
            "/api/tickets",
            json={
                "title": "Test ticket",
                "description": "Test description",
                "ticket_type": "bug",
                "priority": "high"
            },
            headers=auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Test ticket"
```

---

## 7. Performance Testing

### Load Testing

```bash
# Run load tests (if configured)
python -m pytest tests/performance/ -p no:libtmux -v
```

### Database Performance

```python
# Test query performance
import time

def test_query_performance(mock_session):
    start = time.time()
    # Run query
    result = mock_session.query(Task).filter(Task.status == "done").all()
    duration = time.time() - start
    
    assert duration < 0.1  # Should complete in <100ms
```

---

## 8. Known Issues & Gotchas

### pytest Configuration

- **libtmux plugin**: Must use `-p no:libtmux` to avoid `Marks cannot be applied to fixtures` error
- **Async tests**: Use `--asyncio-mode=auto` for async test functions
- **Database**: Tests use in-memory SQLite with `tmp_path` for file-based tests

### Common Test Failures

1. **ForeignKey constraint errors**: Pre-existing issue with some integration tests
2. **Status derivation tests**: Some tests may fail if database schema changes
3. **Tmux tests**: Require tmux server running (skipped in CI)

### Mock Patterns

```python
# Session scope context manager mock
@pytest.fixture
def mock_session_scope():
    with patch("src.core.database.session_scope") as mock:
        session = MagicMock()
        mock.return_value.__enter__ = MagicMock(return_value=session)
        mock.return_value.__exit__ = MagicMock(return_value=False)
        yield session

# Agent manager mock
@pytest.fixture
def mock_agent_manager():
    with patch("src.agents.manager.AgentManager") as mock:
        instance = MagicMock()
        mock.return_value = instance
        yield instance
```

### Test Data

```python
# Create test task
def create_test_task(session, **kwargs):
    task = Task(
        id=str(uuid.uuid4()),
        raw_description="Test task",
        status="pending",
        phase_id="phase-123",
        workflow_id="wf-123",
        **kwargs
    )
    session.add(task)
    session.commit()
    return task
```

---

## Quick Reference

### Run Targeted Tests (default)

```bash
# Backend: only the test file(s) for what you touched
python -m pytest tests/test_<module>.py -p no:libtmux -q
```

### Run All Tests (only when explicitly requested)

```bash
# Backend (with libtmux disabled)
python -m pytest tests/ -p no:libtmux -q

# Frontend type check + build (no automated test suite or linter exists -- see §5)
cd frontend && npx tsc --noEmit && npm run build
```

### Test File Naming

```
# Backend (pytest)
test_<module>.py
test_<feature>.py

# Frontend: no test suite exists yet (see §5) -- no naming convention to follow
```

### Common Commands

```bash
# Run failed tests only
python -m pytest tests/ -p no:libtmux --lf

# Run tests in parallel
python -m pytest tests/ -p no:libtmux -n auto

# Debug test
python -m pytest tests/ -p no:libtmux --pdb

# Run tests matching pattern
python -m pytest tests/ -p no:libtmux -k "phase_manager and not integration"
```

---

*Last Updated: August 2026*
