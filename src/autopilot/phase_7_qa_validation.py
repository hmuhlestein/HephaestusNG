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
    description="""Run comprehensive QA tests and validate the implementation.

Executes unit tests, integration tests, and end-to-end validation.
Compares implementation against requirements and generates a final
QA report with pass/fail status and recommendations.""",
    done_definitions=[
        "Test environment validated (services running)",
        "Unit tests executed and results captured",
        "Integration tests executed and results captured",
        "End-to-end validation performed",
        "Requirements compliance verified",
        "Security fixes validated",
        "qa_report.md created with comprehensive results",
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
STEP 1: READ REQUIREMENTS AND CONTEXT
═══════════════════════════════════════════════════════════════════════

Read:
- Your task description for "Docs Path:" and "Project Path:" locations
- requirements_analysis.md (from Docs Path) - What must it do?
- architecture.md (from Docs Path) - How should it work?
- review_report.md (from Docs Path) - What issues were found?
- doc_review_report.md (from Docs Path) - Were docs updated to match implementation?
- security_report.md (from Docs Path) - What security fixes were made?

═══════════════════════════════════════════════════════════════════════
STEP 2: VALIDATE TEST ENVIRONMENT
═══════════════════════════════════════════════════════════════════════

Check:
```bash
# Check services are running
curl -s http://localhost:8300/health || echo "Backend not running"

# Check test dependencies
python -m pytest --version || echo "pytest not installed"

# Check test files exist
find . -name "test_*.py" -o -name "*_test.py" | head -20
```

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
python -m pytest -v --tb=short 2>&1 | tee all_results.txt

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
STEP 7: CREATE QA REPORT
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
STEP 8: SAVE TO MEMORY
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

DO NOT:
- Skip test categories
- Assume requirements are met without testing
- Ignore failed tests
- Forget to validate security fixes
- Give vague recommendations
""",
    outputs=[
        "qa_report.md with comprehensive test results",
        "Requirements compliance matrix",
        "Security validation results",
        "Iteration recommendation",
    ],
    next_steps=[
        "If PASS: Implementation is complete",
        "If NEEDS_WORK: Return to Phase 3 with specific fixes",
        "If FAIL: Return to Phase 2 for architecture review",
    ],
)
