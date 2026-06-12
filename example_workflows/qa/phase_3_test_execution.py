"""
Phase 3: Test Execution & Reporting

Executes all tests from Phase 2, captures server and browser logs,
analyzes results, and generates a comprehensive QA report.
"""

from src.sdk.models import Phase

PHASE_3_TEST_EXECUTION = Phase(
    id=3,
    name="test_execution",
    description="""Execute all tests and generate a comprehensive QA report.

Runs unit tests, integration tests, browser automation tests,
captures server logs, analyzes results, and produces a final
QA report with pass/fail status and recommendations.""",
    done_definitions=[
        "Phase 2 test implementation reviewed and understood",
        "Test environment validated (services running, Chrome available)",
        "Unit tests executed - results captured",
        "Integration tests executed - results captured",
        "CDP browser tests executed - results captured",
        "Server logs captured and analyzed",
        "Docker container logs captured (if applicable)",
        "QA report generated with pass/fail summary",
        "Failed tests documented with root cause analysis",
        "Recommendations written for fixes/improvements",
        "Results saved to memory",
        "Task marked as done",
    ],
    working_directory=".",
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE A QA EXECUTION AGENT - RUN TESTS AND GENERATE REPORT
═══════════════════════════════════════════════════════════════════════

🎯 YOUR MISSION: Execute all tests, capture logs, produce QA report

═══════════════════════════════════════════════════════════════════════
STEP 1: VALIDATE TEST ENVIRONMENT
═══════════════════════════════════════════════════════════════════════

Before running tests, verify:

```bash
# Check services are running
curl -s http://localhost:8300/health || echo "Backend not running"
curl -s http://localhost:6333/ || echo "Qdrant not running"

# Check Chrome is available for CDP tests
curl -s http://localhost:9222/json/version || echo "Chrome CDP not available"

# Check Python test dependencies
python -c "import pytest; print('pytest OK')" || echo "Install pytest"
python -c "import requests; print('requests OK')" || echo "Install requests"

# Check test files exist
ls -la tests/qa/ || echo "Test directory not found"
```

If Chrome is not available for CDP tests, skip browser tests and note this.

═══════════════════════════════════════════════════════════════════════
STEP 2: CAPTURE BASELINE LOGS
═══════════════════════════════════════════════════════════════════════

Before running tests, capture baseline state:

```bash
# Capture current backend log state
BACKEND_LOG_BEFORE=$(cat $HOME/.hephaestus/logs/*/backend.log 2>/dev/null | wc -l)

# Capture current Docker logs
docker logs --tail 50 qdrant > /tmp/qdrant_baseline.log 2>&1

# Record running processes
ps aux | grep -E "run_server|uvicorn|node" > /tmp/processes_before.txt
```

═══════════════════════════════════════════════════════════════════════
STEP 3: RUN UNIT TESTS
═══════════════════════════════════════════════════════════════════════

```bash
cd tests/qa
python -m pytest test_unit.py -v --tb=short --json-report --json-report-file=unit_results.json 2>&1 | tee unit_output.txt
```

Capture:
- Pass/fail count
- Failed test names and error messages
- Execution time

═══════════════════════════════════════════════════════════════════════
STEP 4: RUN INTEGRATION TESTS
═══════════════════════════════════════════════════════════════════════

```bash
cd tests/qa
python -m pytest test_integration.py -v --tb=short --json-report --json-report-file=integration_results.json 2>&1 | tee integration_output.txt
```

Capture:
- API endpoint test results
- Request/response validation
- Authentication flow results
- Error handling verification

═══════════════════════════════════════════════════════════════════════
STEP 5: RUN BROWSER AUTOMATION TESTS
═══════════════════════════════════════════════════════════════════════

```bash
# Start Chrome with CDP if not running
# google-chrome --remote-debugging-port=9222 --headless &

cd tests/qa
python -m pytest test_browser.py -v --tb=short --json-report --json-report-file=browser_results.json 2>&1 | tee browser_output.txt
```

If Chrome is not available:
```
echo "Chrome CDP not available - skipping browser tests"
echo '{"skipped": true, "reason": "Chrome not available"}' > browser_results.json
```

Capture:
- Page load results
- Console error detection
- Network request validation
- Screenshot comparisons (if configured)

═══════════════════════════════════════════════════════════════════════
STEP 6: CAPTURE POST-TEST LOGS
═══════════════════════════════════════════════════════════════════════

```bash
# Capture backend logs after testing
cat $HOME/.hephaestus/logs/*/backend.log > /tmp/backend_after.log 2>/dev/null
BACKEND_LOG_AFTER=$(wc -l < /tmp/backend_after.log)

# Capture new Docker logs
docker logs --tail 50 qdrant > /tmp/qdrant_after.log 2>&1

# Check for new errors in backend log
grep -i "ERROR\|CRITICAL\|Exception" /tmp/backend_after.log > /tmp/backend_errors.txt 2>/dev/null

# Check for new Qdrant errors
grep -i "ERROR\|error" /tmp/qdrant_after.log > /tmp/qdrant_errors.txt 2>/dev/null
```

═══════════════════════════════════════════════════════════════════════
STEP 7: RUN LOG ANALYSIS TESTS
═══════════════════════════════════════════════════════════════════════

```bash
cd tests/qa
python -m pytest test_logs.py -v --tb=short 2>&1 | tee logs_output.txt
```

═══════════════════════════════════════════════════════════════════════
STEP 8: GENERATE QA REPORT (HTML + MARKDOWN)
═══════════════════════════════════════════════════════════════════════

Create both `qa_report.html` and `qa_report.md`. Include PRD compliance and phase intent sections.

### HTML Report (for human review)

Create `qa_report.html` with a styled, readable report:

```html
<!DOCTYPE html>
<html>
<head>
    <title>QA Report - [Project Name]</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 40px; background: #f5f5f5; }
        .container { max-width: 1000px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; }
        h2 { color: #555; margin-top: 30px; }
        .summary { display: flex; gap: 20px; margin: 20px 0; }
        .stat { flex: 1; padding: 20px; border-radius: 8px; text-align: center; }
        .stat-passed { background: #d4edda; color: #155724; }
        .stat-failed { background: #f8d7da; color: #721c24; }
        .stat-skipped { background: #fff3cd; color: #856404; }
        .stat-total { background: #d1ecf1; color: #0c5460; }
        .stat-number { font-size: 36px; font-weight: bold; }
        .stat-label { font-size: 14px; opacity: 0.8; }
        table { width: 100%; border-collapse: collapse; margin: 15px 0; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }
        th { background: #f8f9fa; font-weight: 600; }
        .pass { color: #28a745; }
        .fail { color: #dc3545; }
        .skip { color: #ffc107; }
        pre { background: #f8f9fa; padding: 15px; border-radius: 4px; overflow-x: auto; font-size: 13px; }
        .section { margin: 25px 0; padding: 20px; background: #f8f9fa; border-radius: 8px; }
        .timestamp { color: #6c757d; font-size: 14px; }
        .recommendation { padding: 10px 15px; margin: 5px 0; border-left: 4px solid #007bff; background: white; }
        .recommendation.critical { border-left-color: #dc3545; }
        .recommendation.warning { border-left-color: #ffc107; }
        .prd-section { border: 2px solid #007bff; padding: 20px; margin: 20px 0; border-radius: 8px; }
        .phase-intent { border: 2px solid #28a745; padding: 20px; margin: 20px 0; border-radius: 8px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>QA Report: [Project Name]</h1>
        <p class="timestamp">Generated: [timestamp]</p>

        <!-- PRD Compliance Section -->
        <div class="prd-section">
            <h2>PRD Compliance</h2>
            <p><strong>PRD Reference:</strong> [path to PRD.md]</p>
            <p><strong>Requirements Met:</strong> [X/Y] ([percent]%)</p>
            <ul>
                <!-- List each PRD requirement and whether it's met -->
                <li class="[pass/fail]">[Requirement]: [Status]</li>
            </ul>
        </div>

        <!-- Phase Intent Section -->
        <div class="phase-intent">
            <h2>Phase Intent Verification</h2>
            <p><strong>Phase:</strong> QA Testing</p>
            <p><strong>Intent:</strong> [Read from TESTING.md - what was this phase supposed to accomplish?]</p>
            <p><strong>Status:</strong> [Met/Partially Met/Not Met]</p>
            <ul>
                <li>[Intent item]: [Status]</li>
            </ul>
        </div>

        <div class="summary">
            <div class="stat stat-total">
                <div class="stat-number">[total]</div>
                <div class="stat-label">Total Tests</div>
            </div>
            <div class="stat stat-passed">
                <div class="stat-number">[passed]</div>
                <div class="stat-label">Passed</div>
            </div>
            <div class="stat stat-failed">
                <div class="stat-number">[failed]</div>
                <div class="stat-label">Failed</div>
            </div>
            <div class="stat stat-skipped">
                <div class="stat-number">[skipped]</div>
                <div class="stat-label">Skipped</div>
            </div>
        </div>

        <h2>Test Results by Category</h2>

        <div class="section">
            <h3>Unit Tests</h3>
            <table>
                <tr><th>Test</th><th>Status</th><th>Duration</th><th>Details</th></tr>
                <!-- For each test -->
                <tr><td>[test_name]</td><td class="[pass/fail]">[PASS/FAIL]</td><td>[duration]</td><td>[details]</td></tr>
            </table>
        </div>

        <div class="section">
            <h3>Integration Tests</h3>
            <!-- Same table format -->
        </div>

        <div class="section">
            <h3>Browser Automation Tests</h3>
            <!-- Same table format -->
        </div>

        <h2>Log Analysis</h2>
        <div class="section">
            <h3>Errors Found</h3>
            <table>
                <tr><th>Category</th><th>Count</th><th>Severity</th><th>First Occurrence</th></tr>
                <!-- For each error type -->
            </table>
        </div>

        <h2>Recommendations</h2>
        <div class="recommendation critical">[Critical fix needed]</div>
        <div class="recommendation warning">[Improvement suggested]</div>
        <div class="recommendation">[Nice to have]</div>

        <h2>Appendix</h2>
        <div class="section">
            <h3>Captured Logs</h3>
            <pre>[log_content]</pre>
        </div>
    </div>
</body>
</html>
```

### Markdown Report (for version control)

Also create `qa_report.md` with the same content in markdown format for tracking in git.

═══════════════════════════════════════════════════════════════════════
STEP 9: SAVE RESULTS TO MEMORY
═══════════════════════════════════════════════════════════════════════

Save key findings to memory:

```python
mcp__hephaestus__save_memory({
    "content": f"QA Report [date]: {total} tests, {passed} passed, {failed} failed. {summary}",
    "agent_id": "[YOUR AGENT ID]",
    "memory_type": "discovery"
})
```

═══════════════════════════════════════════════════════════════════════
STEP 10: CREATE FOLLOW-UP TASKS (IF FAILURES)
═══════════════════════════════════════════════════════════════════════

If critical tests failed, create bug-fix tasks:

```python
# For each critical failure
mcp__hephaestus__create_task({
    "description": f"Fix failing QA test: {test_name}. Error: {error}. Expected: {expected}. Actual: {actual}. See qa_report.md.",
    "phase_id": 1,  # Bug fix workflow phase 1
    "priority": "high"
})
```

═══════════════════════════════════════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════════════════════════════════════

✅ DO:
- Run ALL test categories
- Capture logs before AND after
- Document every failure
- Generate comprehensive report
- Create follow-up tasks for critical failures
- Save results to memory

❌ DO NOT:
- Skip test categories
- Ignore failed tests
- Forget to capture logs
- Create report without evidence
- Mark done if critical tests fail without creating fix tasks
""",
    outputs=[
        "qa_report.html - styled HTML report with PRD compliance and phase intent verification",
        "qa_report.md - markdown report for version control",
        "JSON test results (unit_results.json, etc.)",
        "Captured logs (backend, console, network)",
        "Follow-up tasks for critical failures (if any)",
    ],
    next_steps=[
        "Open qa_report.html in browser for review",
        "Conductor will review PRD compliance and phase intent",
        "Address critical failures if not up to spec",
        "Re-run failed tests after fixes",
    ],
)
