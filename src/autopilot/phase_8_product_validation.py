"""
Phase 8: Product Validation

After QA passes, the product agent validates that the implementation
meets the original design intent and spec. This is the final human-like
sign-off before marking a feature as complete.
"""

from src.sdk.models import Phase

PHASE_8_PRODUCT_VALIDATION = Phase(
    id=8,
    name="product_validation",
    description="""Validate that the implementation meets the original design intent.

After QA testing passes, this phase performs a final product-level validation.
Compares the implemented feature against the original design document,
verifies all requirements are met, checks integration with existing system,
and produces a validation report for human review.""",
    done_definitions=[
        "Original design document re-read and compared to implementation",
        "All functional requirements verified against working code",
        "Non-functional requirements checked (performance, security)",
        "Integration with existing system validated",
        "User experience flows verified",
        "Edge cases from design doc confirmed handled",
        "product_validation.md created with verdict",
        "Validation report includes recommendations for human reviewer",
        "Memory saved with validation outcome",
        "Task marked as done",
    ],
    working_directory=None,
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE A PRODUCT VALIDATOR - CONFIRM THE FEATURE MEETS SPEC
═══════════════════════════════════════════════════════════════════════

YOUR MISSION: Verify the implementation matches the original design intent

═══════════════════════════════════════════════════════════════════════
STEP 1: RE-READ THE ORIGINAL DESIGN DOCUMENT
═══════════════════════════════════════════════════════════════════════

CRITICAL PATH RULE: You MUST use the FULL ABSOLUTE PATHS from your task description.
- NEVER write files to the current working directory or project root.
- ALL docs/reports go in "Docs Path:" (product_validation.md, etc.).
- Code goes in "Project Path:" (src/, tests/, etc.).
- Your task description contains the exact paths — copy them exactly.

Read the original design document again. This is your SOURCE OF TRUTH.

Also read:
- AGENTS.md - Repository guidelines, coding conventions, project structure
- requirements_analysis.md (what was extracted)
- architecture.md (how it was designed)
- doc_review_report.md (documentation quality review)
- qa_report.md (what was tested)

═══════════════════════════════════════════════════════════════════════
STEP 2: VERIFY FUNCTIONAL REQUIREMENTS
═══════════════════════════════════════════════════════════════════════

For EACH functional requirement from the design document:

1. Find the corresponding code implementation
2. Verify it does what the design doc specifies
3. Check acceptance criteria are met
4. Note any gaps or deviations

Create a compliance matrix:

| Design Requirement | Implementation | Status | Evidence |
|--------------------|----------------|--------|----------|
| [From design doc]  | [File/function] | PASS/FAIL | [How verified] |

═══════════════════════════════════════════════════════════════════════
STEP 3: VERIFY NON-FUNCTIONAL REQUIREMENTS
═══════════════════════════════════════════════════════════════════════

Check performance, security, and scalability claims:
- Are performance targets met? (check benchmarks if available)
- Are security measures in place? (check security_report.md)
- Does it scale as designed?

═══════════════════════════════════════════════════════════════════════
STEP 4: VERIFY INTEGRATION WITH EXISTING SYSTEM
═══════════════════════════════════════════════════════════════════════

Check:
- Does it follow existing code patterns?
- Does it integrate with existing components correctly?
- Does it break any existing functionality?
- Are existing tests still passing?

═══════════════════════════════════════════════════════════════════════
STEP 5: VERIFY USER EXPERIENCE FLOWS
═══════════════════════════════════════════════════════════════════════

Walk through the user journeys described in the design doc:
- Can a user actually do what the design describes?
- Are error messages helpful?
- Is the workflow intuitive?

═══════════════════════════════════════════════════════════════════════
STEP 6: CREATE VALIDATION REPORT
═══════════════════════════════════════════════════════════════════════

Write product_validation.md:

# Product Validation Report

## Design: [Name]
## Date: [Date]
## Status: PASS / NEEDS_WORK

## Executive Summary
[1-2 paragraphs: Does this feature meet the original design intent?]

## Requirements Compliance Matrix

| # | Design Requirement | Status | Evidence |
|---|-------------------|--------|----------|
| 1 | [requirement] | PASS/FAIL | [evidence] |

## Non-Functional Validation
- Performance: [status]
- Security: [status]
- Scalability: [status]

## Integration Validation
- Existing system compatibility: [status]
- Code pattern compliance: [status]
- Test regression: [status]

## User Experience Validation
- Primary user flow: [status]
- Error handling: [status]
- Edge cases: [status]

## Issues Found (if any)
1. [issue]: [description] - Severity: [high/medium/low]

## Recommendations for Human Reviewer
1. [What to look at]
2. [What to test manually]
3. [What to consider before merging]

## Verdict
[PASS: Feature meets design intent and is ready for human review]
or
[NEEDS_WORK: [specific issues that need addressing]]

═══════════════════════════════════════════════════════════════════════
STEP 7: SAVE TO MEMORY
═══════════════════════════════════════════════════════════════════════

Save validation outcome to memory:
- Feature name and status
- Key requirements verified
- Issues found (if any)
- Integration points confirmed
- Recommendations for future features

═══════════════════════════════════════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════════════════════════════════════

DO:
- Re-read the original design document
- Verify EVERY requirement (not just a sample)
- Check integration with existing system
- Test user experience flows
- Provide specific evidence for each verification

DO NOT:
- Assume requirements are met without checking
- Skip non-functional requirements
- Ignore integration with existing system
- Give a PASS verdict if there are significant gaps
- Rush through validation
""",
    outputs=[
        "product_validation.md with compliance matrix and verdict",
        "Requirements verification evidence",
        "Integration validation results",
        "Recommendations for human reviewer",
    ],
    next_steps=[
        "If PASS: Feature is ready for human review in the HTML report",
        "If NEEDS_WORK: Specific issues documented for next iteration",
    ],
)
