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
══════════════════════════════════════════════════════════════════════

Chrome DevTools Protocol is available as built-in MCP tools. Use these
tools directly via /tools/execute or curl instead of writing raw CDP code.

Available devtools_* tools:
  devtools_connect             - Connect to Chrome (start session)
  devtools_navigate            - Navigate to a URL
  devtools_evaluate            - Execute JavaScript in the page
  devtools_screenshot          - Capture page screenshot
  devtools_click               - Click an element by CSS selector
  devtools_fill                - Fill an input field
  devtools_get_console_errors  - Get console errors
  devtools_get_failed_requests - Get failed network requests
  devtools_get_network_logs    - Get all network logs
  devtools_get_performance     - Get page performance metrics
  devtools_get_page_info       - Get page title, URL
  devtools_check_broken_images - Find broken images
  devtools_wait_for_selector   - Wait for element to appear
  devtools_get_cookies         - Get browser cookies
  devtools_close               - Close browser session

Create tests/qa/cdp/browser.py as a wrapper around the MCP devtools tools:

```python
import requests
import json
from typing import Any, Dict, Optional

MCP_URL = "http://localhost:8300/tools/execute"

class CDPBrowser:
    """Wrapper around Hephaestus built-in DevTools MCP tools."""

    def __init__(self, session_id: str = "qa", debug_url: str = "http://localhost:9222"):
        self.session_id = session_id
        self.debug_url = debug_url

    def _call(self, tool: str, **kwargs) -> Dict[str, Any]:
        resp = requests.post(MCP_URL, json={
            "tool": f"devtools_{tool}",
            "arguments": {"session_id": self.session_id, **kwargs}
        })
        resp.raise_for_status()
        return resp.json()

    def connect(self, target_url: Optional[str] = None) -> Dict[str, Any]:
        args = {"debug_url": self.debug_url}
        if target_url:
            args["target_url"] = target_url
        return self._call("connect", **args)

    def navigate(self, url: str) -> Dict[str, Any]:
        return self._call("navigate", url=url)

    def evaluate(self, expression: str) -> Any:
        result = self._call("evaluate", expression=expression)
        return result.get("result")

    def screenshot(self, path: Optional[str] = None, fmt: str = "png") -> Dict[str, Any]:
        return self._call("screenshot", path=path, format=fmt)

    def click(self, selector: str) -> Dict[str, Any]:
        return self._call("click", selector=selector)

    def fill(self, selector: str, value: str) -> Dict[str, Any]:
        return self._call("fill", selector=selector, value=value)

    def get_console_errors(self) -> list:
        result = self._call("get_console_errors")
        return result.get("errors", [])

    def get_failed_requests(self, status: Optional[int] = None) -> list:
        args = {}
        if status:
            args["status"] = status
        result = self._call("get_failed_requests", **args)
        return result.get("failed_requests", [])

    def get_network_logs(self, **kwargs) -> list:
        result = self._call("get_network_logs", **kwargs)
        return result.get("logs", [])

    def get_performance(self) -> Dict[str, Any]:
        result = self._call("get_performance")
        return result.get("metrics", {})

    def get_page_info(self) -> Dict[str, Any]:
        return self._call("get_page_info")

    def check_broken_images(self) -> list:
        result = self._call("check_broken_images")
        return result.get("broken_images", [])

    def wait_for_selector(self, selector: str, timeout_ms: int = 5000) -> bool:
        result = self._call("wait_for_selector", selector=selector, timeout_ms=timeout_ms)
        return result.get("found", False)

    def close(self) -> Dict[str, Any]:
        return self._call("close")
```

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

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8300")

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
══════════════════════════════════════════════════════════════════════

Create `tests/qa/test_browser.py` using the CDPBrowser wrapper from STEP 3:

```python
import pytest
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'cdp'))
from browser import CDPBrowser

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

@pytest.fixture(scope="module")
def browser():
    b = CDPBrowser(session_id="qa-test")
    b.connect(target_url=FRONTEND_URL)
    yield b
    b.close()

class TestBrowserAutomation:
    """Browser tests using built-in DevTools MCP tools."""

    def test_page_loads(self, browser):
        browser.navigate(FRONTEND_URL)
        info = browser.get_page_info()
        assert info.get("title") is not None

    def test_console_no_errors(self, browser):
        browser.navigate(FRONTEND_URL)
        errors = browser.get_console_errors()
        assert len(errors) == 0, f"Console errors: {errors}"

    def test_no_failed_requests(self, browser):
        browser.navigate(FRONTEND_URL)
        import time
        time.sleep(3)
        failed = browser.get_failed_requests()
        assert len(failed) == 0, f"Failed requests: {failed}"

    def test_no_broken_images(self, browser):
        browser.navigate(FRONTEND_URL)
        broken = browser.check_broken_images()
        assert len(broken) == 0, f"Broken images: {broken}"

    def test_performance_metrics(self, browser):
        metrics = browser.get_performance()
        assert "load_event" in metrics
```

To run CDP tests, Chrome must be started with:
  google-chrome --remote-debugging-port=9222

The test uses the built-in devtools MCP tools via the wrapper.

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
