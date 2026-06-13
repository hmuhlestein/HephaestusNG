"""
Phase 5: Documentation Review

Reviews all documentation produced by the pipeline for accuracy, completeness,
consistency, and quality. Ensures docs match the actual implementation, fixes
broken links, clarifies ambiguous language, and fills documentation gaps.
"""

from src.sdk.models import Phase

PHASE_5_DOC_REVIEW = Phase(
    id=5,
    name="doc_review",
    description="""Review and fix all project documentation for accuracy, completeness, and quality.

Compares documentation against the actual implementation, fixes inaccuracies,
fills gaps, ensures consistency, and produces a documentation quality report.
This phase runs after adversarial code review so it reviews docs that reflect
the post-review state of the code.""",
    done_definitions=[
        "All documentation files identified and read",
        "Requirements doc compared against implementation",
        "Architecture doc compared against actual code structure",
        "README and setup instructions verified against project state",
        "API documentation checked against actual endpoints/interfaces",
        "Inline docstrings and comments reviewed for accuracy",
        "Broken links, stale references, and outdated content fixed",
        "Inconsistencies between docs and code corrected",
        "Documentation gaps identified and filled",
        "doc_review_report.md created with findings and fixes applied",
        "Memory saved with documentation findings",
        "Task marked as done",
    ],
    working_directory=None,
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE A DOCUMENTATION REVIEWER - VERIFY AND FIX ALL DOCS
════════════════════════════════════════════════════════════════════════

YOUR MISSION: Review every doc against the implementation and FIX issues

═══════════════════════════════════════════════════════════════════════
STEP 1: READ ALL DOCUMENTATION AND CODE
═══════════════════════════════════════════════════════════════════════

Read:
- Your task description for "Docs Path:" and "Project Path:" locations
- requirements_analysis.md (from Docs Path)
- architecture.md (from Docs Path)
- review_report.md (from Docs Path) - What was changed during code review?
- README or any top-level documentation in Project Path
- All source code files in Project Path (to verify docs match)

═══════════════════════════════════════════════════════════════════════
STEP 2: REQUIREMENTS DOC ACCURACY
═══════════════════════════════════════════════════════════════════════

Compare requirements_analysis.md against the implementation:
- Are all listed requirements actually implemented?
- Are there implemented features not in the requirements?
- Are acceptance criteria accurate given what was built?
- Do non-functional requirements match the implementation?
- Are integration points accurately described?

Fix any discrepancies directly in the document.

═══════════════════════════════════════════════════════════════════════
STEP 3: ARCHITECTURE DOC ACCURACY
═══════════════════════════════════════════════════════════════════════

Compare architecture.md against the actual code:
- Does the described module structure match the file layout?
- Are component responsibilities accurately described?
- Do data flow diagrams match actual code paths?
- Are API contracts accurate (request/response formats)?
- Are database schemas accurate?
- Do dependency descriptions match imports?
- Are design patterns actually used as described?

Fix any discrepancies directly in the document.

═══════════════════════════════════════════════════════════════════════
STEP 4: README AND SETUP DOCS
═══════════════════════════════════════════════════════════════════════

Verify setup/usage documentation:
- Are installation steps correct and complete?
- Are environment variables documented?
- Are configuration options accurate?
- Do example commands actually work?
- Are dependencies listed correctly?
- Is the project description accurate?

Fix any issues found.

═══════════════════════════════════════════════════════════════════════
STEP 5: API AND INTERFACE DOCS
═══════════════════════════════════════════════════════════════════════

Check API/interface documentation:
- Do endpoint URLs match the actual routes?
- Do request/response formats match the code?
- Are error codes and messages documented correctly?
- Are authentication requirements documented?
- Are rate limits or constraints noted?
- Do function/method signatures match docstrings?

Fix any issues found.

═══════════════════════════════════════════════════════════════════════
STEP 6: DOCSTRINGS AND INLINE COMMENTS
═══════════════════════════════════════════════════════════════════════

Review inline documentation:
- Do docstrings describe what the function ACTUALLY does?
- Are parameter descriptions accurate?
- Are return value descriptions correct?
- Do comments explain WHY, not just WHAT?
- Are there misleading or stale comments?
- Are complex algorithms explained?
- Do type hints match actual types?

Fix inaccurate docstrings and comments directly in the code.

═══════════════════════════════════════════════════════════════════════
STEP 7: CONSISTENCY AND QUALITY
═══════════════════════════════════════════════════════════════════════

Check cross-document consistency:
- Do all docs use consistent terminology?
- Are naming conventions consistent across docs and code?
- Do docs reference the correct file paths?
- Are cross-references between docs valid?
- Is formatting consistent (headings, lists, code blocks)?
- Are there broken links or stale references?

Fix consistency issues.

═══════════════════════════════════════════════════════════════════════
STEP 8: CREATE DOC REVIEW REPORT
═══════════════════════════════════════════════════════════════════════

Write doc_review_report.md with:

# Documentation Review Report

## Summary
- Documents reviewed: [list]
- Issues found: [count]
- Issues FIXED: [count]
- Overall documentation quality: [EXCELLENT/GOOD/NEEDS_WORK/POOR]

## Requirements Documentation
- Accuracy: [score/assessment]
- Issues found and fixed: [list]

## Architecture Documentation
- Accuracy: [score/assessment]
- Issues found and fixed: [list]

## API/Interface Documentation
- Accuracy: [score/assessment]
- Issues found and fixed: [list]

## README/Setup Documentation
- Accuracy: [score/assessment]
- Issues found and fixed: [list]

## Inline Documentation (docstrings/comments)
- Quality: [score/assessment]
- Issues found and fixed: [list]

## Cross-Document Consistency
- Issues found and fixed: [list]

## Documentation Gaps Identified
- [List any missing documentation that should exist]

## Positive Observations
- [What documentation was done well]

═══════════════════════════════════════════════════════════════════════
STEP 9: FIX ALL DOCUMENTATION ISSUES (MANDATORY)
═══════════════════════════════════════════════════════════════════════

For EVERY documentation issue you find, you MUST fix it:

1. Read the affected file
2. Understand the discrepancy
3. Write the fix directly in the file
4. Verify the fix is correct
5. Document what you changed in the review report

DO NOT just report issues - FIX THEM. You have write access to all files.

═══════════════════════════════════════════════════════════════════════
STEP 10: SAVE TO MEMORY
═══════════════════════════════════════════════════════════════════════

Save documentation findings to memory:
- Common documentation anti-patterns found
- Documentation quality standards to maintain
- Areas that need better documentation practices

═══════════════════════════════════════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════════════════════════════════════

DO:
- Read EVERY documentation file thoroughly
- Compare docs against ACTUAL code, not just requirements
- Fix inaccuracies directly in the documents
- Check cross-references between documents
- Verify setup instructions are complete and correct
- Ensure docstrings match function behavior

DO NOT:
- Skip reading the actual source code
- Accept "close enough" documentation
- Ignore stale or outdated content
- Leave broken cross-references unfixed
- Add documentation for features not yet implemented
- Remove documentation for features that ARE implemented but undocumented
""",
    outputs=[
        "doc_review_report.md with findings and fixes",
        "Updated requirements_analysis.md (if inaccuracies found)",
        "Updated architecture.md (if inaccuracies found)",
        "Fixed docstrings and inline comments in source code",
        "Fixed README/setup documentation",
    ],
    next_steps=[
        "Security review will verify documentation of security controls",
        "QA will validate that documented setup steps actually work",
        "Product validation will check docs match the final product",
    ],
)
