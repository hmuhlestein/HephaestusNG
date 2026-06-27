"""
Phase 3: Development

Implements each component according to the architecture from Phase 2.
Agents work on tasks in dependency order, implementing code and tests.
"""

from src.sdk.models import Phase

PHASE_3_DEVELOPMENT = Phase(
    id=3,
    name="development",
    thinking_level="high",  # the core coding work — reasoning is the value
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
        "`ruff check .` and `ruff format --check .` pass with ZERO errors (logging not print, imports at top, sorted) and `mypy` passes — run `ruff check --fix . && ruff format .` first, then verify clean before marking done",
        "Implementation status documented",
        "Phase 4 review tasks created",
        "Task marked as done",
    ],
    working_directory=None,
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE A SOFTWARE DEVELOPER - IMPLEMENT THE SYSTEM
═══════════════════════════════════════════════════════════════════════

YOUR MISSION: Implement components according to the architecture

═══════════════════════════════════════════════════════════════════════
STEP 1: READ ARCHITECTURE AND GUIDELINES
═══════════════════════════════════════════════════════════════════════

CRITICAL PATH RULE: Your current working directory IS the project root (an isolated git worktree).
- Write ALL code and tests inside your working directory (e.g. ./src, ./tests).
- "Project Path" = your working directory (.).  "Docs Path" = ./docs/ (create it if missing).
- Read the design document and prior inputs from ./.hephaestus/ (design.md, context.md, qa_spec.json).
- Do NOT use absolute paths outside your working directory. Do NOT write into ./.hephaestus/ (it is never merged to main).
- ALL implementation code goes in "Project Path:" (src/, tests/, etc.).
- ALL docs/reports go in "Docs Path:" — not the project root.
- Your task description contains the exact paths — copy them exactly.

Read:
- Your task description for "Docs Path:" and "Project Path:" locations
- AGENTS.md - Coding style, naming conventions, test commands, commit format
- architecture.md (from Docs Path) - Component interfaces and contracts
- pyproject.toml (if present) — check `requires-python` or `python_requires` before writing
  type hints: `X | Y` union syntax requires Python ≥ 3.10; `list[str]` / `dict[k,v]`
  builtins require ≥ 3.9. Use `Union[X, Y]` / `Optional[X]` / `List[str]` from `typing`
  if the project targets an earlier version.

Write implementation code (src/, tests/) to the "Project Path" location.

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
