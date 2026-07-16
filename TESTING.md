# HephaestusNG Testing Guide

**Version:** 1.0.0  
**Date:** July 2026  
**Status:** Active

> **Parent Document:** See [docs/ARCHITECTURE_REVIEW.md](./ARCHITECTURE_REVIEW.md) for system architecture.  
> **Design Documents:** See [config/workflows/autopilot/](./config/workflows/autopilot/) for phase definitions.

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
**Command:**
```bash
cd frontend && npm run test
cd frontend && npx tsc --noEmit  # Type checking
```

---

## 3. Running Tests

### Quick Smoke Test

```bash
# Run just the core tests (fast)
python -m pytest tests/test_phase_manager.py tests/test_status_derivation.py tests/test_transcript_processing.py -p no:libtmux -q
```

### Full Test Suite

```bash
# Run all tests (may take several minutes)
python -m pytest tests/ -p no:libtmux -q
```

### Test File Naming

```
# Backend (pytest)
test_<module>.py
test_<feature>.py

# Frontend (vitest)
<Component>.test.tsx
<service>.test.ts
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
    with patch("src.interfaces.langchain_llm_client.LangChainLLMClient") as mock:
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

### Project Structure

```
frontend/
├── src/
│   ├── components/
│   ├── hooks/
│   ├── services/
│   └── types/
├── tests/
│   ├── setup.ts
│   └── unit/
└── vitest.config.ts
```

### Running Frontend Tests

```bash
# Run all tests
cd frontend && npm test

# Run with coverage
cd frontend && npm run test:coverage

# Type checking
cd frontend && npx tsc --noEmit

# Linting
cd frontend && npm run lint
```

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

### Run All Tests

```bash
# Backend (with libtmux disabled)
python -m pytest tests/ -p no:libtmux -q

# Frontend type check
cd frontend && npx tsc --noEmit

# Frontend lint
cd frontend && npm run lint
```

### Test File Naming

```
# Backend (pytest)
test_<module>.py
test_<feature>.py

# Frontend (vitest)
<Component>.test.tsx
<service>.test.ts
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

*Last Updated: July 2026*
