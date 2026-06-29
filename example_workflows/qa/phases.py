"""
QA Workflow - Python Phase Definitions

A comprehensive QA workflow that:
1. Reads TESTING.md for project-specific test instructions
2. Analyzes the codebase and creates a test plan
3. Implements test scripts with Chrome DevTools Protocol (CDP) automation
4. Executes tests, captures logs, and generates a QA report

Usage:
    from example_workflows.qa.phases import QA_PHASES, QA_WORKFLOW_CONFIG, QA_LAUNCH_TEMPLATE
    from src.sdk.models import WorkflowDefinition

    qa_workflow = WorkflowDefinition(
        id="qa",
        name="QA Testing",
        description="Comprehensive QA with browser automation and log analysis",
        phases=QA_PHASES,
        config=QA_WORKFLOW_CONFIG,
        launch_template=QA_LAUNCH_TEMPLATE,
    )

    sdk = HephaestusSDK(workflow_definitions=[qa_workflow])
"""

from example_workflows.qa.phase_1_test_planning import PHASE_1_TEST_PLANNING
from example_workflows.qa.phase_2_test_implementation import PHASE_2_TEST_IMPLEMENTATION
from example_workflows.qa.phase_3_test_execution import PHASE_3_TEST_EXECUTION
from src.sdk.models import LaunchParameter, LaunchTemplate, WorkflowConfig

# Export phase list
QA_PHASES = [
    PHASE_1_TEST_PLANNING,
    PHASE_2_TEST_IMPLEMENTATION,
    PHASE_3_TEST_EXECUTION,
]

# Workflow Configuration
QA_WORKFLOW_CONFIG = WorkflowConfig(
    has_result=True,
    result_criteria="All tests pass or comprehensive QA report generated with actionable findings",
    on_result_found="complete",
)

# Launch Template - defines the form users fill out to start this workflow
QA_LAUNCH_TEMPLATE = LaunchTemplate(
    parameters=[
        LaunchParameter(
            name="project_name",
            label="Project Name",
            type="text",
            required=True,
            description="Name of the project to test (e.g., 'Sotto AI Assistant')",
        ),
        LaunchParameter(
            name="test_scope",
            label="Test Scope",
            type="dropdown",
            required=True,
            options=[
                "Full Suite",
                "Unit Tests Only",
                "Integration Only",
                "Browser Only",
                "Smoke Test",
            ],
            default="Full Suite",
            description="What scope of tests to run",
        ),
        LaunchParameter(
            name="focus_areas",
            label="Focus Areas",
            type="textarea",
            required=False,
            description="Optional: Specific areas to focus testing on (e.g., 'authentication, API endpoints, frontend forms')",
        ),
        LaunchParameter(
            name="skip_browser",
            label="Skip Browser Tests",
            type="dropdown",
            required=True,
            options=["No", "Yes"],
            default="No",
            description="Skip Chrome DevTools Protocol browser tests?",
        ),
        LaunchParameter(
            name="services_url",
            label="Services URL",
            type="text",
            required=False,
            description="Base URL for services (default: http://localhost:8300)",
        ),
        LaunchParameter(
            name="frontend_url",
            label="Frontend URL",
            type="text",
            required=False,
            description="Frontend URL for browser tests (default: http://localhost:5173)",
        ),
        LaunchParameter(
            name="known_issues",
            label="Known Issues",
            type="textarea",
            required=False,
            description="Optional: Known issues to be aware of during testing",
        ),
    ],
    phase_1_task_prompt="""Phase 1: QA Test Planning - {project_name}

**Test Scope:** {test_scope}
**Services URL:** {services_url}
**Frontend URL:** {frontend_url}

---

## Focus Areas

{focus_areas}

---

## Known Issues

{known_issues}

---

## Your Task

You are a QA Test Planner for {project_name}.

### STEP 1: Read TESTING.md
Look for TESTING.md in the project root. It contains project-specific test instructions.

### STEP 2: Analyze Codebase
- Identify languages, frameworks, and entry points
- Find API endpoints and database models
- Locate existing tests (if any)
- Identify frontend components for browser testing

### STEP 3: Create Test Plan
Create `test_plan.md` with:
- Unit test cases for core logic
- Integration test cases for API endpoints
- Browser automation test cases for CDP
- Log monitoring requirements
- Test environment setup instructions

### STEP 4: Identify CDP Targets
For browser testing, identify:
- Which pages/routes need browser testing
- What user workflows to automate
- What console errors to watch for
- What network requests to intercept

### STEP 5: Create Phase 2 Task
Create a Phase 2 task with the complete test plan for implementation.

### STEP 6: Mark Done
Mark your task as done with test_plan.md as deliverable.
""",
)

# Export everything
__all__ = ["QA_PHASES", "QA_WORKFLOW_CONFIG", "QA_LAUNCH_TEMPLATE"]
