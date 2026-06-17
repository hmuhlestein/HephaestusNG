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
    working_directory=None,
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
- Include code snippets showing key interfaces, function signatures, or data structures
- Specify logging requirements: what to log, at what level, with what context

Example task structure:
```
Task: Implement [Component]
Ticket: TICKET-001

Files to create/modify:
- src/component.py (new)
- src/tests/test_component.py (new)

Implementation:
- Create class with method signatures:
  def process(self, data: Dict[str, Any]) -> Result:
      ...

Logging:
- DEBUG: Input data summary on entry
- INFO: Processing milestones
- WARNING: Retry attempts, fallback usage
- ERROR: Failures with context
- Use structured logging: logger.info("processing", extra={"id": item_id})

Acceptance Criteria:
- [ ] All methods have type hints
- [ ] All public methods have docstrings
- [ ] Logging at appropriate levels
- [ ] Tests cover happy path and error cases
```

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

CRITICAL PATH RULE: You MUST use the FULL ABSOLUTE PATHS from your task description.
- NEVER write files to the current working directory or project root.
- ALWAYS use the "Docs Path:" value for ALL generated docs (.md, .json, .txt).
- ALWAYS use the "Project Path:" value for ALL implementation code.
- Your task description contains the exact paths — copy them exactly.

Read your task description for the correct paths:
- "Docs Path:" tells you where to write architecture.md
- "Project Path:" tells you where implementation code goes

Write architecture.md to the "Docs Path" location from your task description.

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
STEP 8: OBJECT-ORIENTED DESIGN PASS
═══════════════════════════════════════════════════════════════════════

Before finalizing the architecture, perform an OO design pass:

### Inheritance & Composition
- Can related components share a base class or interface?
- Should behavior be composed via containment vs inheritance?
- Are there Strategy / Template patterns that simplify variants?

### Single Responsibility
- Does each class/module do ONE thing well?
- Can any class be split into smaller, focused units?
- Are there God objects that need refactoring?

### Dependency Inversion
- Do high-level modules depend on abstractions, not concretions?
- Can we define interfaces/protocols for key boundaries?
- Are dependencies injected, not hard-coded?

### Refactoring Opportunities
- What code is likely to be duplicated? Extract shared utilities.
- What patterns emerge across components? Create shared abstractions.
- Where can push-down details (implementation specifics) into subclasses
  while keeping base interfaces clean?

### Questions to Answer
1. What are the core abstractions in this system?
2. Which classes can be refactored to use composition over inheritance?
3. What interfaces should exist at component boundaries?
4. What details should be pushed down from base to derived classes?
5. What shared behavior can be extracted into mixins or utility classes?

Document OO decisions in architecture.md under "Object-Oriented Design".

═══════════════════════════════════════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════════════════════════════════════

DO:
- Read requirements_analysis.md thoroughly
- Design clear component interfaces
- Create tasks with proper blocking relationships
- Include detailed acceptance criteria
- Include code snippets showing key interfaces and data structures
- Specify logging requirements (levels, context, structured logging)
- Respect technology choices from Phase 1
- Perform an OO design pass before finalizing
- Identify refactoring opportunities and shared abstractions
- Push down details while keeping base interfaces clean

DO NOT:
- Skip component interface design
- Create tasks without blocking relationships
- Forget edge cases in acceptance criteria
- Ignore non-functional requirements
- Skip the OO design pass
- Create tightly coupled components without abstractions
- Use silent fallbacks that hide configuration errors — throw clear exceptions instead
  (Exception: retry logic, graceful degradation with explicit logging, and user-facing defaults are acceptable)


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
