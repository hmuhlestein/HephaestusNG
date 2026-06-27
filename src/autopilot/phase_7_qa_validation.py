"""
Phase 7: QA Testing & Validation

Runs comprehensive tests to validate the implementation meets
all requirements. Generates a final report with pass/fail status
and recommendations for iteration.
"""

from src.sdk.models import Phase

PHASE_7_QA_VALIDATION = Phase(
    id=7,
    name="qa_validation",
    thinking_level="medium",  # run tests + reason about failures
    description="""Run comprehensive QA tests and validate the implementation.

Executes unit tests, integration tests, and end-to-end validation.
Compares implementation against requirements and generates a final
QA report with pass/fail status and recommendations.""",
    done_definitions=[
        "TESTING.md checked (exists or noted as missing)",
        "TESTING.md read thoroughly (if exists)",
        "If TESTING.md exists: followed its instructions exactly",
        "If TESTING.md missing: used fallback test approach (unit tests only)",
        "Test commands executed and results captured",
        "Log locations documented",
        "Unit tests executed and results captured",
        "Integration tests executed and results captured",
        "End-to-end validation performed",
        "Requirements compliance verified",
        "Security fixes validated",
        "qa_report.md created with comprehensive results",
        "./docs/qa_result.json created with structured pass/fail counts (gate input)",
        "Memory saved with QA findings",
        "Iteration recommendation provided (done/needs_work)",
        "Task marked as done",
    ],
    working_directory=None,
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE A QA ENGINEER - VALIDATE THE IMPLEMENTATION
═══════════════════════════════════════════════════════════════════════

YOUR MISSION: Run comprehensive tests and validate against requirements

═══════════════════════════════════════════════════════════════════════
STEP 0: CHECK FOR TESTING.md (PRIMARY INSTRUCTIONS)
═══════════════════════════════════════════════════════════════════════

Look for TESTING.md in the project root (Project Path from your task).

If TESTING.md EXISTS:
  → TESTING.md is your PRIMARY instructions. Follow it EXACTLY.
  → It contains:
     - How to start/run the application
     - How to run existing tests (exact commands)
     - Where to find application logs
     - Known issues and workarounds
     - Test priorities and focus areas
     - Environment setup instructions
  → Read it thoroughly, then follow its instructions step by step.
  → Skip to STEP 7 (Create Report) after following TESTING.md.
  → The steps below are FALLBACK only — ignore them if TESTING.md exists.

If TESTING.md DOES NOT EXIST:
  → Note in your report: "TESTING.md not found - using fallback instructions"
  → Continue with the fallback steps below (STEP 1 through STEP 7).

═══════════════════════════════════════════════════════════════════════
FALLBACK STEPS (only if TESTING.md does not exist)
═══════════════════════════════════════════════════════════════════════

STEP 1: READ REQUIREMENTS AND CONTEXT
═══════════════════════════════════════════════════════════════════════

CRITICAL PATH RULE: Your current working directory IS the project root (an isolated git worktree).
- Write ALL code and tests inside your working directory (e.g. ./src, ./tests).
- "Project Path" = your working directory (.).  "Docs Path" = ./docs/ (create it if missing).
- Read the design document and prior inputs from ./.hephaestus/ (design.md, context.md, qa_spec.json).
- Do NOT use absolute paths outside your working directory. Do NOT write into ./.hephaestus/ (it is never merged to main).
- ALL docs/reports go in "Docs Path:" (qa_report.md, etc.).
- Code/tests go in "Project Path:" (src/, tests/, etc.).
- Your task description contains the exact paths — copy them exactly.

Read:
- Your task description for "Docs Path:" and "Project Path:" locations
- TESTING.md — How to run the app and tests
- requirements_analysis.md (from Docs Path) - What must it do?
- architecture.md (from Docs Path) - How should it work?
- review_report.md (from Docs Path) - What issues were found?
- doc_review_report.md (from Docs Path) - Were docs updated to match implementation?
- security_report.md (from Docs Path) - What security fixes were made?

═══════════════════════════════════════════════════════════════════════
STEP 2: START APPLICATION AND VALIDATE ENVIRONMENT
═══════════════════════════════════════════════════════════════════════

Follow TESTING.md instructions EXACTLY to start the application:

```bash
# Use the startup command from TESTING.md
# Example: python run_server.py or npm start
# Follow the exact command documented in TESTING.md

# After starting, verify it's running:
curl -s http://127.0.0.1:8300/health || echo "Backend not running"  # 127.0.0.1, not localhost (localhost→IPv6 ::1 fails; server binds IPv4)

# Check test dependencies
python -m pytest --version || echo "pytest not installed"

# Check test files exist
find . -name "test_*.py" -o -name "*_test.py" | head -20
```

Document:
- Application startup command used
- Service URLs and ports
- Log file locations (from TESTING.md)
- Any startup issues encountered

═══════════════════════════════════════════════════════════════════════
STEP 3: DISCOVER AND RUN TESTS
═══════════════════════════════════════════════════════════════════════

First, discover where tests actually live:
```bash
# Find test directories and files
find . -name "test_*.py" -o -name "*_test.py" -o -name "*.test.*" -o -name "*.spec.*" | head -20
find . -type d -name "tests" -o -name "test" -o -name "__tests__" | head -10
```

Then run whatever tests you find:
```bash
# If pytest tests exist
python -m pytest -v --tb=short -p no:libtmux 2>&1 | tee all_results.txt
# Note: -p no:libtmux avoids plugin conflict if libtmux is installed globally

# If npm/node tests exist
npm test 2>&1 | tee all_results.txt

# If no tests exist, note this and create basic smoke tests
```

If no tests exist, create minimal smoke tests based on the architecture document to validate the core functionality works.

Capture:
- Pass/fail counts
- Failed test names and errors
- Execution time
- Coverage (if available)


═══════════════════════════════════════════════════════════════════════
STEP 4: REQUIREMENTS COMPLIANCE CHECK
═══════════════════════════════════════════════════════════════════════

For EACH functional requirement from requirements_analysis.md:

1. Identify which test(s) validate this requirement
2. If no test exists, write a quick validation
3. Mark as: PASS / FAIL / PARTIAL / UNTESTED

Create a compliance matrix:

| Requirement | Test Coverage | Status | Notes |
|-------------|---------------|--------|-------|
| FR-1: [desc] | test_fr1.py | PASS | |
| FR-2: [desc] | - | UNTESTED | Need test |
| FR-3: [desc] | test_fr3.py | FAIL | [error] |

═══════════════════════════════════════════════════════════════════════
STEP 5: VALIDATE SECURITY FIXES
═══════════════════════════════════════════════════════════════════════

For each critical security fix from security_report.md:
- Verify the fix is in place
- Test that the vulnerability is no longer exploitable
- Ensure the fix doesn't break functionality

═══════════════════════════════════════════════════════════════════════
STEP 6: RUN SMOKE TESTS
═══════════════════════════════════════════════════════════════════════

Run quick end-to-end validation:
- Start the application
- Hit key API endpoints
- Verify responses are correct
- Check error handling

═══════════════════════════════════════════════════════════════════════
STEP 7: CREATE TICKETS FOR ISSUES FOUND
═══════════════════════════════════════════════════════════════════════

For EACH issue found (Critical, Major, Minor), create a ticket using create_ticket.

TICKET TEMPLATE (copy this for each issue):

```
create_ticket(
  agent_id="<your agent id>",
  workflow_id="<your workflow id>",
  task_id="<your task id>",
  phase_id="<your phase id>",
  title="[BUG] <short descriptive title>",
  description='''
## Issue Description
<What is the problem?>

## Expected Behavior
<What should happen?>

## Actual Behavior
<What actually happens?>

## Steps to Reproduce
1. <Step 1>
2. <Step 2>
3. <Step 3>

## Environment
- OS: <macOS/Linux/Windows>
- Python/Node version: <version>
- Related files: <file paths involved>

## Severity
<Critical/Major/Minor>

## Suggested Fix
<If known, suggest a fix>
''',
  ticket_type="bug",
  priority="<critical/high/medium/low>",
  tags=["qa-finding", "phase-7", "<feature-name>"]
)
```

TICKET CATEGORIES:
- **Critical**: Security vulnerabilities, data loss, complete feature failure
- **Major**: Core functionality broken, significant UX issues, performance degradation
- **Minor**: Cosmetic issues, minor edge cases, documentation gaps

After creating tickets, include them in your QA report:

```markdown
## Tickets Created

| Ticket ID | Title | Priority | Category |
|-----------|-------|----------|----------|
| ticket-xxx | [BUG] Auth bypass | Critical | Security |
| ticket-yyy | [BUG] Slow query | Major | Performance |
```

═══════════════════════════════════════════════════════════════════════
STEP 8: CREATE QA REPORT
═══════════════════════════════════════════════════════════════════════

Write qa_report.md with:

# QA Report: [Project Name]

## Executive Summary
- **Overall Status:** PASS / FAIL / NEEDS_WORK
- **Tests Run:** [total]
- **Tests Passed:** [count] ([percent]%)
- **Tests Failed:** [count]
- **Requirements Met:** [met]/[total] ([percent]%)
- **Critical Issues:** [count]
- **Recommendation:** [done / iterate / needs_major_fixes]

## Test Results

### Unit Tests
| Test | Status | Duration | Notes |
|------|--------|----------|-------|
| ... | PASS/FAIL | ... | ... |

Summary: [X] passed, [Y] failed, [Z] skipped

### Integration Tests
| Test | Status | Duration | Notes |
|------|--------|----------|-------|
| ... | PASS/FAIL | ... | ... |

Summary: [X] passed, [Y] failed, [Z] skipped

## Requirements Compliance

| ID | Requirement | Status | Evidence |
|----|-------------|--------|----------|
| FR-1 | [desc] | PASS | test_fr1.py passed |
| FR-2 | [desc] | FAIL | test_fr2.py failed: [error] |

## Security Validation

| Vulnerability | Fix Verified | Test |
|---------------|--------------|------|
| SQL Injection | YES | sql_injection_test.py passed |
| XSS | YES | xss_test.py passed |

## Issues Found

### Critical (Blocks completion)
- [Issue 1]: [description]

### Major (Should fix)
- [Issue 1]: [description]

### Minor (Nice to fix)
- [Issue 1]: [description]

## Iteration Recommendation

**If PASS:**
All requirements met, tests pass, security validated.
The implementation is complete.

**If NEEDS_WORK:**
[Specific issues that need addressing in next iteration]
1. [Issue 1 - what to fix]
2. [Issue 2 - what to fix]

**If FAIL:**
[Major problems that require significant rework]
1. [Problem 1]
2. [Problem 2]

═══════════════════════════════════════════════════════════════════════
STEP 8.5: EMIT STRUCTURED QA RESULT (REQUIRED — drives the pipeline gate)
═══════════════════════════════════════════════════════════════════════

In addition to qa_report.md, write a machine-readable ./docs/qa_result.json.
The pipeline scores this against the project spec to decide whether to continue,
return to development, or return to architecture. Use REAL numbers from your run.

Write ./docs/qa_result.json with EXACTLY this schema:

```json
{
  "failed_tests": 0,
  "passed_tests": 0,
  "total_tests": 0,
  "pass_rate": 100,
  "critical_issues": 0,
  "major_issues": 0,
  "requirements_total": 0,
  "requirements_met": 0,
  "agent_score": 0.0,
  "verdict": "PASS",
  "summary": "one-line summary"
}
```

Field rules:
- Counts must be integers reflecting actual test/issue counts.
- pass_rate is a percent (0–100): passed_tests / total_tests * 100.
- critical_issues = security/data-loss/complete-failure issues (these send the
  pipeline back to architecture). major issues go in major_issues.
- agent_score (0.0–1.0) is YOUR subjective quality judgement for things the
  numbers don't capture (only used when all hard floors pass).
- verdict is "PASS", "NEEDS_WORK", or "FAIL".
- Do NOT inflate numbers. The gate enforces hard floors regardless of verdict.

═══════════════════════════════════════════════════════════════════════
STEP 9: SAVE TO MEMORY
═══════════════════════════════════════════════════════════════════════

Save QA findings to memory:
- Test results and coverage
- Requirements compliance status
- Issues found and fixed
- Patterns to watch for in future iterations

═══════════════════════════════════════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════════════════════════════════════

DO:
- Run ALL existing tests
- Validate against ALL requirements
- Test security fixes thoroughly
- Provide specific pass/fail evidence
- Give clear iteration recommendation
- CREATE TICKETS for all issues found (Critical, Major, Minor)
- Include reproduction steps in every ticket
- Search for existing tickets before creating (avoid duplicates)

DO NOT:
- Skip test categories
- Assume requirements are met without testing
- Ignore failed tests
- Forget to validate security fixes
- Give vague recommendations


═══════════════════════════════════════════════════════════════════════
WHEN YOU ARE DONE - MARK YOUR TASK AS COMPLETE (DO NOT SKIP THIS)
═══════════════════════════════════════════════════════════════════════

CRITICAL: Do NOT just print a summary and stop. Do NOT exit to the command line.
You MUST call the update_task_status tool. The system CANNOT detect you finished
without this call. The pipeline WILL get stuck.

After writing all your output files, call:

mcp__hephaestus__update_task_status({
  "task_id": "<your task id>",
  "status": "done",
  "summary": "<brief summary of what was accomplished>",
  "key_learnings": ["<key findings or decisions>"]
})

Then wait for confirmation. Do NOT exit until you see the task marked as done.
""",
    outputs=[],
    next_steps=[],
)
