"""
Autopilot Multi-Agent Workflow - Python Phase Definitions

A fully automated pipeline that takes design documents and iterates through:
1. Product Requirements Extraction (context-aware)
2. Architecture & Design
3. Development
4. Adversarial Code Review
5. Security Review
6. QA Testing & Validation
7. Product Validation (final spec check)
8. Git Commit & Push

The workflow loops until the original intent is satisfied or a hard stop
condition is met (hard error, impasse, or major architectural issue).

Designed to run continuously, picking designs from a queue and processing
them through the full pipeline until complete.
"""

from example_workflows.autopilot.phase_1_product_requirements import PHASE_1_PRODUCT_REQUIREMENTS
from example_workflows.autopilot.phase_2_architecture import PHASE_2_ARCHITECTURE
from example_workflows.autopilot.phase_3_development import PHASE_3_DEVELOPMENT
from example_workflows.autopilot.phase_4_adversarial_review import PHASE_4_ADVERSARIAL_REVIEW
from example_workflows.autopilot.phase_5_security_review import PHASE_5_SECURITY_REVIEW
from example_workflows.autopilot.phase_6_qa_validation import PHASE_6_QA_VALIDATION
from example_workflows.autopilot.phase_7_product_validation import PHASE_7_PRODUCT_VALIDATION
from example_workflows.autopilot.phase_8_git_commit_push import PHASE_8_GIT_COMMIT_PUSH

from src.sdk.models import WorkflowConfig, LaunchTemplate, LaunchParameter

AUTOPILOT_PHASES = [
    PHASE_1_PRODUCT_REQUIREMENTS,
    PHASE_2_ARCHITECTURE,
    PHASE_3_DEVELOPMENT,
    PHASE_4_ADVERSARIAL_REVIEW,
    PHASE_5_SECURITY_REVIEW,
    PHASE_6_QA_VALIDATION,
    PHASE_7_PRODUCT_VALIDATION,
    PHASE_8_GIT_COMMIT_PUSH,
]

AUTOPILOT_WORKFLOW_CONFIG = WorkflowConfig(
    has_result=True,
    result_criteria="Feature validated and committed to git, ready for human review",
    on_result_found="do_nothing",
    enable_tickets=True,
    board_config={
        "columns": [
            {"id": "backlog", "name": "Backlog", "order": 1, "color": "#94a3b8"},
            {"id": "requirements", "name": "Requirements", "order": 2, "color": "#3b82f6"},
            {"id": "architecture", "name": "Architecture", "order": 3, "color": "#8b5cf6"},
            {"id": "in-progress", "name": "In Progress", "order": 4, "color": "#f59e0b"},
            {"id": "review", "name": "In Review", "order": 5, "color": "#ec4899"},
            {"id": "security", "name": "Security", "order": 6, "color": "#ef4444"},
            {"id": "qa", "name": "QA", "order": 7, "color": "#14b8a6"},
            {"id": "validated", "name": "Validated", "order": 8, "color": "#22c55e"},
            {"id": "shipped", "name": "Shipped", "order": 9, "color": "#3b82f6"},
        ],
        "ticket_types": ["infrastructure", "feature", "bug-fix", "security", "integration"],
        "default_ticket_type": "feature",
        "initial_status": "backlog",
        "auto_assign": True,
        "require_comments_on_status_change": True,
        "allow_reopen": True,
        "track_time": True,
    },
)

AUTOPILOT_LAUNCH_TEMPLATE = LaunchTemplate(
    parameters=[
        LaunchParameter(
            name="design_document",
            label="Design Document Path",
            type="text",
            required=True,
            description="Path to the design document (PRD, DESIGN.md, etc.)"
        ),
        LaunchParameter(
            name="project_path",
            label="Project Working Directory",
            type="text",
            required=True,
            description="Where to build the feature"
        ),
        LaunchParameter(
            name="project_context",
            label="Project Context (Optional)",
            type="textarea",
            required=False,
            description="Optional: Additional context about the larger project vision"
        ),
    ],
    phase_1_task_prompt="""Phase 1: Product Requirements Extraction

**Design Document:** {design_document}
**Project Path:** {project_path}

---

## Additional Context
{project_context}

---

## Your Task

You are extracting requirements from the design document.

### STEP 0: Gather Project Context
Before reading the design document:
1. Check for existing requirements_analysis.md, architecture.md
2. Look in features/ directory for previously completed features
3. Read existing source code to understand the current system
4. Search memory for technology decisions and constraints

### STEP 1: Read the Design Document
Read the file at: {design_document}

### STEP 2: Extract Requirements
- Functional requirements with acceptance criteria
- Non-functional requirements
- Integration points with existing system
- Technology constraints

### STEP 3: Create Requirements Document
Write requirements_analysis.md in {project_path}

### STEP 4: Save to Memory
Save key decisions and project context.

### STEP 5: Create Phase 2 Task
Create a Phase 2 task with full requirements and context.

### STEP 6: Mark Done
Mark your task as done.
""",
)

__all__ = [
    "AUTOPILOT_PHASES",
    "AUTOPILOT_WORKFLOW_CONFIG",
    "AUTOPILOT_LAUNCH_TEMPLATE",
]
