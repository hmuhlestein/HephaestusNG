"""
Phase 3: Development

Implements each component according to the architecture from Phase 2.
Agents work on tasks in dependency order, implementing code and tests.
"""

from src.sdk.models import Phase

PHASE_3_DEVELOPMENT = Phase(
    id=3,
    name="development",
    description="""Implement all components according to the architecture.

Reads architecture.md from Phase 2, implements each component following
the task breakdown, writes tests, and creates working software.""",
    done_definitions=[
        "Architecture document reviewed and understood",
        "All infrastructure components implemented and verified",
        "All foundation components implemented and tested",
        "All feature components implemented and tested",
        "Integration points implemented",
        "Code follows project style guide",
        "Unit tests written and passing",
        "No linting errors or type check failures",
        "Implementation status documented",
        "Phase 4 review tasks created",
        "Task marked as done",
    ],
    working_directory=".",
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE A SOFTWARE DEVELOPER - IMPLEMENT THE SYSTEM
═══════════════════════════════════════════════════════════════════════

YOUR MISSION: Implement components according to the architecture

═══════════════════════════════════════════════════════════════════════
STEP 1: READ ARCHITECTURE AND GUIDELINES
═══════════════════════════════════════════════════════════════════════

Read:
- AGENTS.md - Coding style, naming conventions, test commands, commit format
- architecture.md from Phase 2 - Component interfaces and contracts

Understand:
- Component interfaces and contracts
- Implementation order and dependencies
- Directory structure
- Technology choices
- Acceptance criteria for your task
- Coding style (Black, flake8, mypy, snake_case, PascalCase)
- Test commands (python tests/run_all_tests.py, npm run type-check)
- Commit format (feat:, fix:, chore: prefixes)

═══════════════════════════════════════════════════════════════════════
STEP 2: IMPLEMENT YOUR COMPONENT
═══════════════════════════════════════════════════════════════════════

For your assigned component:

1. **Set up project structure** (if infrastructure task)
   - Create directories
   - Initialize package manager
   - Set up build configuration
   - Configure linters/formatters

2. **Implement core logic** (if feature task)
   - Create files as specified in architecture
   - Implement classes/functions per interface spec
   - Handle edge cases from requirements
   - Follow existing code patterns in the project

3. **Write tests**
   - Unit tests for core logic
   - Integration tests for interfaces
   - Edge case tests
   - Aim for meaningful coverage, not just line count

4. **Verify implementation**
   - Run tests and ensure they pass
   - Run linter and fix issues
   - Run type checker if applicable
   - Verify component works as specified

═══════════════════════════════════════════════════════════════════════
STEP 3: CREATE PHASE 4 REVIEW TASKS
═══════════════════════════════════════════════════════════════════════

After implementing, create tasks for Phase 4 (Adversarial Review):
- Reference the files you created/modified
- Include context about what was implemented
- Note any areas of concern

═══════════════════════════════════════════════════════════════════════
STEP 4: SAVE TO MEMORY
═══════════════════════════════════════════════════════════════════════

Save implementation notes to memory:
- Files created/modified
- Key implementation decisions
- Any deviations from architecture (with rationale)
- Known limitations or TODOs

═══════════════════════════════════════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════════════════════════════════════

DO:
- Follow the architecture document exactly
- Write clean, readable code
- Include error handling
- Write meaningful tests
- Handle edge cases

DO NOT:
- Deviate from architecture without documenting why
- Skip tests
- Leave TODO/FIXME without tracking
- Ignore error handling
- Create code that doesn't match the interface spec
""",
    outputs=[
        "Implemented source code for all components",
        "Unit and integration tests",
        "Documentation for implemented components",
        "Phase 4 review tasks",
    ],
    next_steps=[
        "Phase 4 will perform adversarial code review",
        "Security review will follow",
        "QA will validate everything works end-to-end",
    ],
)
