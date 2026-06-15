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
    description="""Perform adversarial code review and fix issues found.

Reviews all code from Phase 3 with a critical perspective.
Identifies and FIXES bugs, design flaws, edge cases, performance issues,
security concerns, and deviations from the architecture.""",
    done_definitions=[
        "All implemented code reviewed",
        "Bugs and issues identified with severity levels",
        "Critical and major issues FIXED in the code",
        "Edge cases and error handling reviewed and improved",
        "Performance issues identified and fixed",
        "Architecture deviations corrected",
        "Code quality issues resolved",
        "review_report.md created with findings and fixes applied",
        "Memory saved with review findings",
        "Task marked as done",
    ],
    working_directory=None,
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE AN ADVERSARIAL CODE REVIEWER - FIND AND FIX THE PROBLEMS
═══════════════════════════════════════════════════════════════════════

YOUR MISSION: Find bugs, flaws, and issues - then FIX them yourself

═══════════════════════════════════════════════════════════════════════
STEP 1: READ ARCHITECTURE AND REQUIREMENTS
═══════════════════════════════════════════════════════════════════════

CRITICAL PATH RULE: You MUST use the FULL ABSOLUTE PATHS from your task description.
- NEVER write files to the current working directory or project root.
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

### Error Handling
- Are errors caught and handled properly?
- Are error messages descriptive?
- Are there empty catch blocks?
- Are resources properly cleaned up?

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

### Security
- Is user input validated?
- Are there injection vulnerabilities?
- Are secrets properly handled?
- Are permissions checked?

═══════════════════════════════════════════════════════════════════════
STEP 3: TEST YOUR FINDINGS
═══════════════════════════════════════════════════════════════════════

If you find a potential issue, try to reproduce it:
- Write a small test case
- Run the code to confirm the bug
- Document the exact steps to reproduce

═══════════════════════════════════════════════════════════════════════
STEP 4: CREATE REVIEW REPORT
═══════════════════════════════════════════════════════════════════════

Write review_report.md with:

# Adversarial Code Review Report

## Summary
- Critical issues found: [count]
- Critical issues FIXED: [count]
- Major issues found: [count]
- Major issues FIXED: [count]
- Minor issues: [count]
- Overall assessment: [PASS/FAIL/NEEDS_WORK]

## Issues Found and Fixed

### [Issue 1]
- **File:** [path:line]
- **Description:** [what was wrong]
- **Impact:** [what could have happened]
- **Fix Applied:** [what you changed to fix it]
- **Status:** FIXED

## Minor Issues (not worth fixing now)
...

## Architecture Deviations
...

## Positive Observations
[What was done well - important for morale]

═══════════════════════════════════════════════════════════════════════
STEP 5: FIX CRITICAL AND MAJOR ISSUES (MANDATORY)
═══════════════════════════════════════════════════════════════════════

For EVERY critical and major issue you find, you MUST fix it:

1. Read the affected file
2. Understand the issue
3. Write the fix directly in the code
4. Verify the fix is correct
5. Document what you changed in the review report

DO NOT just report issues - FIX THEM. You have write access to the code.

If an issue requires a major refactor that would break other components,
document it in the report but do not attempt the fix.

═══════════════════════════════════════════════════════════════════════
STEP 6: SAVE TO MEMORY
═══════════════════════════════════════════════════════════════════════

Save review findings to memory:
- Common patterns of issues found
- Areas that need extra attention
- Positive patterns to maintain

═══════════════════════════════════════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════════════════════════════════════

DO:
- Be thorough and systematic
- Test your findings when possible
- Provide specific file/line references
- Suggest concrete fixes
- Acknowledge good code too

DO NOT:
- Be vague ("this code is bad")
- Skip edge cases
- Ignore error handling
- Forget security review
- Review without reading the code
""",
    outputs=[
        "review_report.md with detailed findings",
        "Fix tasks for critical and major issues",
        "Architecture deviation report",
        "Performance and security assessment",
    ],
    next_steps=[
        "Fix tasks will be addressed before security review",
        "Security review will focus on security-specific concerns",
        "QA will validate all fixes are working",
    ],
)
