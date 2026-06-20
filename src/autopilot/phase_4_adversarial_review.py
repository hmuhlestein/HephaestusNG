"""
Phase 4: Adversarial Code Review

Reviews the implementation from Phase 3 with a critical eye.
Looks for bugs, design flaws, edge cases, performance issues,
and deviations from the architecture.
"""

from src.sdk.models import Phase

PHASE_4_ADVERSARIAL_REVIEW = Phase(
    id=4,
    name="adversarial_review",
    description="""Perform adversarial code review and document findings.

Reviews all code from Phase 3 with a critical perspective.
Identifies bugs, design flaws, edge cases, performance issues,
and deviations from the architecture. Reports findings — does NOT fix them.""",
    done_definitions=[
        "All implemented code reviewed",
        "BLOCKER issues identified and documented",
        "FIX issues identified and documented",
        "DEFER issues documented for later",
        "Edge cases and error handling reviewed and improved",
        "Performance issues identified and fixed",
        "Architecture deviations corrected",
        "review_report.md created with BLOCKER/FIX/DEFER findings",
        "Memory saved with review findings",
        "Task marked as done",
    ],
    working_directory=None,
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE AN ADVERSARIAL CODE REVIEWER - FIND THE PROBLEMS
═══════════════════════════════════════════════════════════════════════

YOUR MISSION: Find bugs, flaws, and issues - document them for the development team

REVIEW METHODOLOGY: Be harsh. Find problems, not praise. Show evidence.
Classify findings as BLOCKER (must fix), FIX (should fix), or DEFER (nice to have).

═══════════════════════════════════════════════════════════════════════
STEP 1: READ ARCHITECTURE AND REQUIREMENTS
═══════════════════════════════════════════════════════════════════════

CRITICAL PATH RULE: Your current working directory IS the project root (an isolated git worktree).
- Write ALL code and tests inside your working directory (e.g. ./src, ./tests).
- "Project Path" = your working directory (.).  "Docs Path" = ./docs/ (create it if missing).
- Read the design document and prior inputs from ./.hephaestus/ (design.md, context.md, qa_spec.json).
- Do NOT use absolute paths outside your working directory. Do NOT write into ./.hephaestus/ (it is never merged to main).
- ALL docs/reports go in "Docs Path:" (review_report.md, etc.).
- Code fixes go in "Project Path:" (src/, tests/, etc.).
- Your task description contains the exact paths — copy them exactly.

Read:
- Your task description for "Docs Path:" and "Project Path:" locations
- architecture.md (from Docs Path) - What was the design?
- requirements_analysis.md (from Docs Path) - What should it do?
- Your goal: Did the implementation (in Project Path) match the design?

═══════════════════════════════════════════════════════════════════════
STEP 2: REVIEW EACH COMPONENT
═══════════════════════════════════════════════════════════════════════

For each implemented component, check:

### Assumptions & Gaps
- Are there unstated assumptions being relied on?
- What's missing from the requirements or design?
- Are we solving the right problem?
- What edge cases weren't considered?
- What happens when dependencies fail or are unavailable?
- Are there implicit contracts between components that aren't documented?

### Correctness
- Does it do what the requirements specify?
- Are there logic errors or off-by-one bugs?
- Does it handle all edge cases?
- Are error messages helpful?

### Design Quality
- Is the code clean and readable?
- Are there code smells or anti-patterns?
- Is the module too large or doing too much?
- Are there unnecessary dependencies?

### Object-Oriented Quality
- Are abstractions clean or leaky?
- Is there a base class / interface hierarchy, or flat inheritance?
- Can any classes be refactored to use composition over inheritance?
- Are details pushed down from base to derived classes appropriately?
- Are there God objects that need splitting?
- Is dependency inversion followed (depend on abstractions, not concretions)?
- Can shared behavior be extracted into mixins, protocols, or utilities?
- Do class responsibilities follow Single Responsibility Principle?

### Error Handling
- Are errors caught and handled properly?
- Are error messages descriptive?
- Are there empty catch blocks?
- Are resources properly cleaned up?

### Fallbacks & Silent Failures
- Are there fallbacks that silently hide configuration errors?
- Do functions return empty/None instead of raising exceptions for missing required config?
- Are fallback values masking real problems that should fail fast?
- Legitimate exceptions: retry logic, graceful degradation with logging, user-facing defaults

### Edge Cases
- What happens with empty input?
- What happens with very large input?
- What happens with concurrent access?
- What happens when dependencies fail?

### Performance
- Are there obvious N+1 queries?
- Are there unnecessary loops or allocations?
- Are expensive operations cached?
- Are there memory leaks?

### Code Quality
- Run `ruff check` on changed files — fix lint errors
- Check for unused imports, variables, or dead code
- Verify type hints are present and correct



═══════════════════════════════════════════════════════════════════════
STEP 3: RUN LINT AND TEST YOUR FINDINGS
═══════════════════════════════════════════════════════════════════════

First, run lint checks on changed files:
- `ruff check <file>` — fix any lint errors
- `ruff check --fix <file>` — auto-fix simple issues

If you find a potential issue, try to reproduce it:
- Write a small test case
- Run the code to confirm the bug
- Document the exact steps to reproduce

═══════════════════════════════════════════════════════════════════════
STEP 4: CREATE REVIEW REPORT
═══════════════════════════════════════════════════════════════════════

Write review_report.md with:

# Adversarial Code Review Report

**Target:** {what was reviewed}
**Date:** {date}
**Files reviewed:** {list of files}

## Summary
- **BLOCKERS:** [count] — must fix before proceeding
- **FIXES:** [count] — safe to apply without approval
- **DEFERRED:** [count] — optional or out of scope
- **Overall assessment:** [PASS/FAIL/NEEDS_WORK]

## Findings

### [BLOCKER] {title}
- **File:** {path}:{line}
- **Evidence:** {what's wrong, include code snippet}
- **Impact:** {what could go wrong}
- **Recommended Fix:** {direction for fixing}

### [FIX] {title}
- **File:** {path}:{line}
- **Evidence:** {what's wrong}
- **Fix Applied:** {what you changed to fix it}
- **Status:** FIXED

### [DEFER] {title}
- **File:** {path}:{line}
- **Reason:** {why deferred}

## Assumptions & Gaps
[Unstated assumptions, missing requirements, design gaps]

## Architecture Deviations
[Deviations from the planned architecture]

## Positive Observations
[What was done well - important for morale]

═══════════════════════════════════════════════════════════════════════
STEP 5: FIX BLOCKER AND FIX ISSUES (MANDATORY)
═══════════════════════════════════════════════════════════════════════

For EVERY BLOCKER and FIX issue you find, you MUST fix it:

1. Read the affected file
2. Understand the issue
3. Write the fix directly in the code
4. Run existing tests to verify no regressions
5. Document what you changed in the review report

DO NOT just report issues - FIX THEM. You have write access to the code.

If an issue requires a major refactor that would break other components,
document it in the report as DEFER but do not attempt the fix.

═══════════════════════════════════════════════════════════════════════
STEP 6: SAVE TO MEMORY
═══════════════════════════════════════════════════════════════════════

Save review findings to memory:
- Common patterns of issues found
- Areas that need extra attention
- Positive patterns to maintain

═══════════════════════════════════════════════════════════════════════
CLASSIFICATION CRITERIA
═══════════════════════════════════════════════════════════════════════

BLOCKER (critical) = data loss, crash, incorrect results, API contract violation
FIX (major) = poor error handling, missing edge case, code smell, performance issue
DEFER (minor) = style, documentation gap, optimization opportunity

═══════════════════════════════════════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════════════════════════════════════

DO:
- Be harsh. Find problems, not praise.
- Show evidence. File path + line number.
- Classify honestly: BLOCKER, FIX, or DEFER.
- Check test adequacy: do tests exist? do they test meaningful behavior?
- Review OO design: abstractions, inheritance hierarchies, composition
- Check for silent fallbacks that hide configuration errors (prefer clear exceptions)
- Don't trust the worker — inspect actual code

DO NOT:
- Be vague ("this code is bad")
- Skip edge cases
- Ignore error handling
- Review without reading the code
- Inflate severity — classify honestly
- Fix security issues — that's Phase 6's responsibility


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
    outputs=["review_report.md"],  # Contains BLOCKER/FIX/DEFER findings
    next_steps=[
        "Phase 5 doc review reads review_report.md",
        "Phase 6 security review reads review_report.md for security concerns",
    ],
)
