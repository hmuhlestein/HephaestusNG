"""
Phase 2: Architecture & Design

Takes the requirements from Phase 1 and creates detailed technical architecture,
task breakdowns with blocking relationships, and implementation plans.
"""

from src.sdk.models import Phase

PHASE_2_ARCHITECTURE = Phase(
    id=2,
    name="architecture_design",
    description="""Create detailed technical architecture and task breakdown.

Reads requirements_analysis.md from Phase 1, designs the system architecture,
creates detailed task breakdowns with blocking relationships, and produces
implementation-ready specifications for each component.""",
    done_definitions=[
        "requirements_analysis.md reviewed and understood",
        "System architecture diagram/description created",
        "Component interfaces defined (APIs, data models, contracts)",
        "Data flow documented",
        "Infrastructure requirements specified",
        "Task breakdown with blocking relationships created",
        "Each task has detailed acceptance criteria",
        "architecture.md created with complete technical design",
        "Memory saved with architectural decisions",
        "Phase 3 development tasks created for each component",
        "Task marked as done",
    ],
    working_directory=".",
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE A SOFTWARE ARCHITECT - DESIGN THE SYSTEM
═══════════════════════════════════════════════════════════════════════

YOUR MISSION: Turn requirements into detailed technical architecture

═══════════════════════════════════════════════════════════════════════
STEP 1: READ REQUIREMENTS
═══════════════════════════════════════════════════════════════════════

Read requirements_analysis.md from Phase 1. Understand:
- All functional requirements
- Non-functional requirements
- Component list and dependencies
- Technology constraints
- Implementation order

═══════════════════════════════════════════════════════════════════════
STEP 2: DESIGN SYSTEM ARCHITECTURE
═══════════════════════════════════════════════════════════════════════

For each component, define:

### Component: [Name]
- **Purpose:** What it does
- **Interface:** Public API / methods / endpoints
- **Data Model:** What data it manages
- **Dependencies:** What it needs from other components
- **Implementation Details:** Key algorithms, patterns, libraries

### Data Flow
- How data moves between components
- Request/response cycles
- Error handling flows

### Infrastructure
- Directory structure
- Build configuration
- Environment setup

═══════════════════════════════════════════════════════════════════════
STEP 3: CREATE TASK BREAKDOWN
═══════════════════════════════════════════════════════════════════════

Create detailed tasks for Phase 3 (Development). For EACH component:

### Task: [Component Name]
**Ticket ID:** (will be assigned)
**Priority:** critical / high / medium / low
**Blocked By:** [list of component names that must be done first]

**Description:**
- Detailed implementation steps
- Files to create/modify
- Functions/classes to implement
- Tests to write
- Edge cases to handle

**Acceptance Criteria:**
- [ ] [Criterion 1]
- [ ] [Criterion 2]
- [ ] All tests pass
- [ ] Code follows project style guide

**Estimated Complexity:** simple / moderate / complex

═══════════════════════════════════════════════════════════════════════
STEP 4: CREATE TICKETS WITH BLOCKING RELATIONSHIPS
═══════════════════════════════════════════════════════════════════════

Create Kanban tickets for each component:
1. Infrastructure tickets FIRST (no blockers)
2. Foundation tickets SECOND (blocked by infrastructure)
3. Feature tickets THIRD (blocked by foundation)
4. Integration tickets LAST (blocked by features)

Each ticket must have:
- Clear title and description
- Proper blocked_by_ticket_ids
- Priority level
- Tags for categorization

═══════════════════════════════════════════════════════════════════════
STEP 5: CREATE DEVELOPMENT TASKS
═══════════════════════════════════════════════════════════════════════

Create ONE Phase 3 task per ticket (1:1 relationship):
- Reference the ticket ID in the task description
- Include detailed implementation instructions
- Specify files to create/modify
- Include test requirements

═══════════════════════════════════════════════════════════════════════
STEP 6: SAVE TO MEMORY
═══════════════════════════════════════════════════════════════════════

Save architectural decisions to memory:
- Component interfaces and contracts
- Design patterns chosen
- Trade-offs and rationale
- Critical implementation notes

═══════════════════════════════════════════════════════════════════════
STEP 7: CREATE ARCHITECTURE DOCUMENT
═══════════════════════════════════════════════════════════════════════

Write architecture.md with:

# Architecture: [Project Name]

## System Overview
[High-level description]

## Components
### [Component 1]
- Purpose: ...
- Interface: ...
- Data Model: ...
- Implementation: ...

### [Component 2]
...

## Data Flow
[How data moves through the system]

## Directory Structure
```
project/
├── frontend/
├── backend/
└── ...
```

## Task Breakdown
### Infrastructure
1. [Task] - Blocked By: None

### Foundation
2. [Task] - Blocked By: [infrastructure tasks]

### Features
3. [Task] - Blocked By: [foundation tasks]

## Implementation Order
1. [Component A] → 2. [Component B] → 3. [Component C]
...

═══════════════════════════════════════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════════════════════════════════════

DO:
- Read requirements_analysis.md thoroughly
- Design clear component interfaces
- Create tasks with proper blocking relationships
- Include detailed acceptance criteria
- Respect technology choices from Phase 1

DO NOT:
- Skip component interface design
- Create tasks without blocking relationships
- Forget edge cases in acceptance criteria
- Ignore non-functional requirements
""",
    outputs=[
        "architecture.md with complete technical design",
        "Component interfaces and data models",
        "Task breakdown with blocking relationships",
        "Phase 3 development tasks created",
    ],
    next_steps=[
        "Phase 3 will implement each component",
        "Development follows the architecture document",
        "Tasks execute in dependency order",
    ],
)
