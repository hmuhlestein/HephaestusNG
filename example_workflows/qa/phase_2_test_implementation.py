"""
Phase 2: Test Implementation

Implements the test scripts based on the test plan from Phase 1.
Sets up Chrome DevTools Protocol (CDP) browser automation,
writes unit/integration tests, and configures log capture.
"""

from src.sdk.models import Phase

PHASE_2_TEST_IMPLEMENTATION = Phase(
    id=2,
    name="test_implementation",
    description="""Implement test scripts based on the test plan.

Writes unit tests, integration tests, CDP browser automation scripts,
and configures log capture. All test code is ready to execute in Phase 3.""",
    done_definitions=[
        "test_plan.md read and understood",
        "Test directory structure created (tests/qa/)",
        "Unit test files written",
        "Integration test files written",
        "CDP browser automation scripts written",
        "Log capture scripts configured",
        "Test runner script created (run_qa_tests.py)",
        "All test files syntax-validated",
        "Phase 3 execution task created",
        "Task marked as done",
    ],
    working_directory=".",
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE A QA TEST IMPLEMENTER - WRITE THE TEST CODE
═══════════════════════════════════════════════════════════════════════

🎯 YOUR MISSION: Implement all test scripts from the test plan

═══════════════════════════════════════════════════════════════════════
STEP 1: READ THE TEST PLAN
═══════════════════════════════════════════════════════════════════════

Read test_plan.md from Phase 1. Understand:
- What tests need to be written
- What components need testing
- What browser workflows to automate
- What logs to capture

═══════════════════════════════════════════════════════════════════════
STEP 2: CREATE TEST DIRECTORY STRUCTURE
═══════════════════════════════════════════════════════════════════════

Create:
```
tests/qa/
  __init__.py
  conftest.py              # Shared fixtures
  test_unit.py             # Unit tests
  test_integration.py      # Integration/API tests
  test_browser.py          # CDP browser automation tests
  test_logs.py             # Log capture and analysis
  run_qa_tests.py          # Main test runner
  cdp/
    __init__.py
    browser.py             # CDP browser helper class
    console_monitor.py     # Console log capture
    network_monitor.py     # Network request monitoring
  fixtures/
    test_data.json         # Test data fixtures
```

═══════════════════════════════════════════════════════════════════════
STEP 3: IMPLEMENT CDP BROWSER AUTOMATION
═══════════════════════════════════════════════════════════════════════

Create `tests/qa/cdp/browser.py`:

```python
import asyncio
import json
import websockets
from typing import Optional, Callable

class CDPBrowser:
    \"\"\"Chrome DevTools Protocol browser automation helper.\"\"\"

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.msg_id = 0
        self.listeners: dict[str, list[Callable]] = {}

    async def connect(self):
        self.ws = await websockets.connect(self.ws_url)

    async def send(self, method: str, params: dict = None) -> dict:
        self.msg_id += 1
        msg = {"id": self.msg_id, "method": method}
        if params:
            msg["params"] = params
        await self.ws.send(json.dumps(msg))
        while True:
            response = json.loads(await self.ws.recv())
            if response.get("id") == self.msg_id:
                return response

    async def navigate(self, url: str):
        await self.send("Page.navigate", {"url": url})

    async def evaluate(self, expression: str) -> any:
        result = await self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True
        })
        return result.get("result", {}).get("result", {}).get("value")

    async def enable_console(self, callback: Callable):
        await self.send("Console.enable")
        self.listeners["console"].append(callback)

    async def enable_network(self, callback: Callable):
        await self.send("Network.enable")
        self.listeners["network"].append(callback)

    async def get_console_logs(self) -> list:
        logs = []
        await self.send("Runtime.enable")
        # Collect logs for a short period
        return logs

    async def screenshot(self, path: str):
        result = await self.send("Page.captureScreenshot", {"format": "png"})
        import base64
        with open(path, "wb") as f:
            f.write(base64.b64decode(result["result"]["data"]))

    async def close(self):
        if self.ws:
            await self.ws.close()
```

═══════════════════════════════════════════════════════════════════════
STEP 4: IMPLEMENT UNIT TESTS
═══════════════════════════════════════════════════════════════════════

Create `tests/qa/test_unit.py`:

```python
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

# Add actual unit tests based on test_plan.md
# Example:
# from sotto.core.some_module import some_function
#
# def test_some_function():
#     result = some_function(input)
#     assert result == expected
```

Write tests for:
- Core business logic
- Data validation
- Utility functions
- Error handling paths

═══════════════════════════════════════════════════════════════════════
STEP 5: IMPLEMENT INTEGRATION TESTS
═══════════════════════════════════════════════════════════════════════

Create `tests/qa/test_integration.py`:

```python
import pytest
import requests
import time

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8000")

class TestAPIIntegration:
    \"\"\"Integration tests for API endpoints.\"\"\"

    def test_health_endpoint(self):
        response = requests.get(f"{BASE_URL}/health")
        assert response.status_code == 200
        assert response.json().get("status") == "healthy"

    # Add tests based on test_plan.md
    # Example:
    # def test_create_task(self):
    #     response = requests.post(f"{BASE_URL}/api/tasks", json={...})
    #     assert response.status_code == 200
```

Test:
- API endpoints respond correctly
- Request/response cycles work
- Authentication flows
- Error responses are correct

═══════════════════════════════════════════════════════════════════════
STEP 6: IMPLEMENT BROWSER AUTOMATION TESTS
═══════════════════════════════════════════════════════════════════════

Create `tests/qa/test_browser.py`:

```python
import pytest
import asyncio
import os
from cdp.browser import CDPBrowser

CHROME_WS_URL = os.getenv("CHROME_WS_URL", "ws://localhost:9222")

@pytest.fixture
def browser():
    return CDPBrowser(CHROME_WS_URL)

class TestBrowserAutomation:
    \"\"\"Browser tests using Chrome DevTools Protocol.\"\"\"

    @pytest.mark.asyncio
    async def test_page_loads(self, browser):
        await browser.connect()
        await browser.navigate("http://localhost:5300")
        title = await browser.evaluate("document.title")
        assert title is not None
        await browser.close()

    @pytest.mark.asyncio
    async def test_console_no_errors(self, browser):
        await browser.connect()
        errors = []
        await browser.enable_console(lambda msg: errors.append(msg))
        await browser.navigate("http://localhost:5300")
        await asyncio.sleep(3)
        assert len(errors) == 0, f"Console errors: {errors}"
        await browser.close()

    @pytest.mark.asyncio
    async def test_api_calls_succeed(self, browser):
        await browser.connect()
        failed_requests = []
        await browser.enable_network(lambda req: failed_requests.append(req))
        await browser.navigate("http://localhost:5300")
        await asyncio.sleep(5)
        assert len(failed_requests) == 0
        await browser.close()
```

To run CDP tests, Chrome must be started with:
  google-chrome --remote-debugging-port=9222

═══════════════════════════════════════════════════════════════════════
STEP 7: IMPLEMENT LOG CAPTURE
═══════════════════════════════════════════════════════════════════════

Create `tests/qa/test_logs.py`:

```python
import subprocess
import re
from pathlib import Path

LOG_DIR = Path.home() / ".hephaestus" / "logs"

class TestLogCapture:
    \"\"\"Capture and analyze server logs during testing.\"\"\"

    def capture_backend_logs(self) -> str:
        backend_log = LOG_DIR.glob("*/backend.log")
        logs = []
        for log_file in sorted(backend_log, key=os.path.getmtime, reverse=True):
            with open(log_file) as f:
                logs.append(f.read())
            break
        return "\\n".join(logs)

    def test_no_errors_in_logs(self):
        logs = self.capture_backend_logs()
        error_pattern = re.compile(r"ERROR|CRITICAL|Exception|Traceback", re.IGNORECASE)
        errors = error_pattern.findall(logs)
        assert len(errors) == 0, f"Found {len(errors)} errors in logs"

    def capture_docker_logs(self, container: str) -> str:
        result = subprocess.run(
            ["docker", "logs", "--tail", "100", container],
            capture_output=True, text=True
        )
        return result.stdout + result.stderr

    def test_qdrant_no_errors(self):
        logs = self.capture_docker_logs("qdrant")
        assert "ERROR" not in logs
```

═══════════════════════════════════════════════════════════════════════
STEP 8: CREATE TEST RUNNER
═══════════════════════════════════════════════════════════════════════

Create `tests/qa/run_qa_tests.py`:

```python
#!/usr/bin/env python3
\"\"\"QA Test Runner - Executes all tests and generates report.\"\"\"
import subprocess
import json
import sys
from datetime import datetime
from pathlib import Path

def run_tests():
    report = {
        "timestamp": datetime.now().isoformat(),
        "results": {}
    }

    # Run unit tests
    result = subprocess.run(
        ["pytest", "test_unit.py", "-v", "--tb=short"],
        capture_output=True, text=True
    )
    report["results"]["unit"] = {
        "passed": result.returncode == 0,
        "output": result.stdout
    }

    # Run integration tests
    result = subprocess.run(
        ["pytest", "test_integration.py", "-v", "--tb=short"],
        capture_output=True, text=True
    )
    report["results"]["integration"] = {
        "passed": result.returncode == 0,
        "output": result.stdout
    }

    # Run browser tests (if Chrome is available)
    result = subprocess.run(
        ["pytest", "test_browser.py", "-v", "--tb=short"],
        capture_output=True, text=True
    )
    report["results"]["browser"] = {
        "passed": result.returncode == 0,
        "output": result.stdout
    }

    # Run log analysis
    result = subprocess.run(
        ["pytest", "test_logs.py", "-v", "--tb=short"],
        capture_output=True, text=True
    )
    report["results"]["logs"] = {
        "passed": result.returncode == 0,
        "output": result.stdout
    }

    # Write report
    report_path = Path("qa_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"Report saved to {report_path}")
    return report

if __name__ == "__main__":
    run_tests()
```

═══════════════════════════════════════════════════════════════════════
STEP 9: VALIDATE SYNTAX
═══════════════════════════════════════════════════════════════════════

Run syntax validation on all test files:
  python -m py_compile tests/qa/test_unit.py
  python -m py_compile tests/qa/test_integration.py
  python -m py_compile tests/qa/test_browser.py
  python -m py_compile tests/qa/test_logs.py

═══════════════════════════════════════════════════════════════════════
STEP 10: CREATE PHASE 3 TASK
═══════════════════════════════════════════════════════════════════════

Create a Phase 3 task to execute all tests and generate the final report.

═══════════════════════════════════════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════════════════════════════════════

✅ DO:
- Follow test_plan.md exactly
- Write clean, runnable test code
- Include proper error handling in tests
- Create reusable CDP helpers
- Validate all test files compile
- Document how to run tests

❌ DO NOT:
- Write tests that depend on external state
- Skip CDP browser automation setup
- Forget log capture configuration
- Leave syntax errors in test files
- Create tests without assertions
""",
    outputs=[
        "tests/qa/ directory with all test files",
        "CDP browser automation helpers",
        "Log capture and analysis scripts",
        "Test runner (run_qa_tests.py)",
        "Phase 3 execution task",
    ],
    next_steps=[
        "Phase 3 will execute all tests",
        "Phase 3 will capture and analyze logs",
        "Phase 3 will generate comprehensive QA report",
    ],
)
