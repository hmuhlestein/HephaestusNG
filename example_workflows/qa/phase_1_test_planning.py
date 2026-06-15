"""
Phase 1: Test Planning

Reads TESTING.md from the project root, analyzes the codebase structure,
identifies testable components, and creates a comprehensive test plan.
"""

from src.sdk.models import Phase

PHASE_1_TEST_PLANNING = Phase(
    id=1,
    name="test_planning",
    description="""Analyze the project and create a comprehensive test plan.

Reads TESTING.md for project-specific test instructions, scans the codebase
for testable components, and produces a prioritized test plan covering
unit tests, integration tests, API tests, and browser automation tests.""",
    done_definitions=[
        "TESTING.md checked (exists or noted as missing)",
        "TESTING.md read thoroughly (if exists)",
        "App startup instructions followed (or default approach used)",
        "Log locations documented",
        "Project structure analyzed (languages, frameworks, entry points)",
        "Testable components identified and categorized",
        "Test plan created with prioritized test cases",
        "Test environment requirements documented",
        "Chrome DevTools Protocol (CDP) targets identified for browser tests",
        "Server log capture points identified",
        "Phase 2 implementation task created with full test plan",
        "Task marked as done",
    ],
    working_directory=".",
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE A QA TEST PLANNER - CREATE A COMPREHENSIVE TEST PLAN
═══════════════════════════════════════════════════════════════════════

🎯 YOUR MISSION: Read TESTING.md, analyze the codebase, create a test plan

═══════════════════════════════════════════════════════════════════════
STEP 1: CHECK FOR TESTING.md
═══════════════════════════════════════════════════════════════════════

Look for TESTING.md in the project root.

If TESTING.md DOES NOT EXIST:
- Note this in your plan: "TESTING.md not found - using standard test discovery"
- Continue to Step 2 using default test approach
- Create basic smoke tests if none exist

If TESTING.md EXISTS:
1. Read it thoroughly - it contains:
   - How to start/run the application
   - How to run existing tests
   - Where to find logs
   - Known issues and workarounds
   - Test priorities and focus areas
   - Environment setup instructions
2. Follow its instructions to verify the app runs
3. Document all log locations mentioned
4. Note any specific test commands provided
5. Use the exact commands from TESTING.md for running tests

═══════════════════════════════════════════════════════════════════════
STEP 2: ANALYZE PROJECT STRUCTURE
═══════════════════════════════════════════════════════════════════════

Scan the codebase to understand:
- What language(s) and frameworks are used
- Entry points (main files, server startup, CLI commands)
- API endpoints (REST, GraphQL, WebSocket)
- Database models and queries
- Frontend components (if any)
- Existing tests (if any)
- Configuration files
- Docker/services setup

Use bash commands like:
  find . -name "*.py" -not -path "./.venv/*" | head -50
  find . -name "*.ts" -o -name "*.tsx" | head -50
  cat pyproject.toml || cat requirements.txt || cat package.json
  grep -r "def test_\|async def test_\|describe(\|it(" tests/ 2>/dev/null

═══════════════════════════════════════════════════════════════════════
STEP 3: IDENTIFY TESTABLE COMPONENTS
═══════════════════════════════════════════════════════════════════════

Categorize what needs testing:

**Unit Tests:**
- Core business logic functions
- Data models and validation
- Utility functions
- Error handling

**Integration Tests:**
- API endpoint request/response cycles
- Database operations (CRUD)
- Service interactions
- Authentication/authorization flows

**Browser Automation Tests (CDP):**
- UI workflows (login, forms, navigation)
- Dynamic content loading
- Error states in the UI
- WebSocket connections in the browser
- Console log monitoring

**Log Monitoring:**
- Server startup/shutdown logs
- Error logs during test execution
- Performance metrics
- API response times

═══════════════════════════════════════════════════════════════════════
STEP 4: CREATE TEST PLAN
═══════════════════════════════════════════════════════════════════════

Create `test_plan.md` with:

```markdown
# Test Plan: [Project Name]

## Test Environment
- Python version: X.Y.Z
- Node version: X.Y.Z
- Services required: [list]
- Environment variables: [list]

## Test Categories

### 1. Unit Tests
| # | Test Case | Component | Priority | Status |
|---|-----------|-----------|----------|--------|
| 1 | ... | ... | P0/P1/P2 | TODO |

### 2. Integration Tests
| # | Test Case | Endpoint/Flow | Priority | Status |
|---|-----------|---------------|----------|--------|

### 3. Browser Tests (CDP)
| # | Test Case | Page/Route | Expected | Priority |
|---|-----------|------------|----------|----------|

### 4. Log Monitoring
| What to monitor | Where to find logs | Alert conditions |
|-----------------|-------------------|------------------|

## Test Data
- [Required test fixtures]
- [Mock data needed]
- [External service stubs]

## Risk Areas
- [High-risk components]
- [Known flaky areas]
- [Performance-sensitive paths]
```

═══════════════════════════════════════════════════════════════════════
STEP 5: IDENTIFY CDP TARGETS
═══════════════════════════════════════════════════════════════════════

For browser automation, identify:
- Which pages/routes need browser testing
- What user workflows should be automated
- What console errors to watch for
- What network requests to intercept/verify

Chrome DevTools Protocol will be used for:
- `Runtime.evaluate()` - Run JS in page context
- `Page.navigate()` - Navigate to routes
- `Network.enable()` - Monitor network requests
- `Console.enable()` - Capture console messages
- `Runtime.consoleAPICalled` - Log console output
- `Page.loadEventFired` - Detect page loads

═══════════════════════════════════════════════════════════════════════
STEP 6: IDENTIFY LOG CAPTURE POINTS
═══════════════════════════════════════════════════════════════════════

Document where to find logs:
- Application logs: [path]
- Server access logs: [path]
- Error logs: [path]
- Database logs: [path]
- Docker container logs: `docker logs [container]`

═══════════════════════════════════════════════════════════════════════
STEP 7: CREATE PHASE 2 TASK
═══════════════════════════════════════════════════════════════════════

Create a Phase 2 task with:
- Complete test plan reference
- List of test files to create
- CDP configuration requirements
- Log monitoring setup requirements

═══════════════════════════════════════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════════════════════════════════════

✅ DO:
- Read TESTING.md if it exists
- Analyze the actual codebase (don't guess)
- Create actionable test cases
- Identify real CDP targets
- Document log locations
- Prioritize tests by risk

❌ DO NOT:
- Skip codebase analysis
- Create vague test cases
- Forget browser automation targets
- Ignore existing tests
- Create Phase 2 task without test plan
""",
    outputs=[
        "test_plan.md with comprehensive test plan",
        "Phase 2 implementation task with full context",
        "Test environment requirements documented",
        "CDP test targets identified",
        "Log capture points documented",
    ],
    next_steps=[
        "Phase 2 will implement the test scripts",
        "Phase 2 will set up CDP browser automation",
        "Phase 2 will configure log capture",
        "Phase 3 will execute all tests and generate reports",
    ],
)
