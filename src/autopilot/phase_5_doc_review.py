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
    thinking_level="low",  # mostly mechanical checking
    description="""Review and fix all project documentation for accuracy, completeness, and quality.

Compares documentation against the actual implementation, fixes inaccuracies,
fills gaps, ensures consistency, and produces a documentation quality report.
This phase runs after adversarial code review so it reviews docs that reflect
the post-review state of the code.""",
    done_definitions=[
        "Stray files organized into Docs Path (mandatory first step)",
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
        "feature_report.html written to Docs Path (AI-authored HTML feature summary for the UI)",
        "Memory saved with documentation findings",
        "Task marked as done",
    ],
    working_directory=None,
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE A DOCUMENTATION REVIEWER - VERIFY AND FIX ALL DOCS
════════════════════════════════════════════════════════════════════════

YOUR MISSION: Review every doc against the implementation and FIX issues

═══════════════════════════════════════════════════════════════════════
STEP 1: ORGANIZE STRAY FILES INTO DOCS PATH (MANDATORY FIRST STEP)
═══════════════════════════════════════════════════════════════════════

BEFORE reviewing anything, you MUST move misplaced files to the correct location.

Read your task description for "Docs Path:" and "Project Path:" locations.
The "Docs Path" is the ONLY location where generated docs, reports, and
pipeline artifacts belong. Everything else must be code in "Project Path".

Agents before you may have written docs, scripts, reports, or other artifacts
to the WRONG location (project root, relative paths, etc.). Your job is to
sweep them into the correct folder before reviewing.

Run these commands to find and move stray files:
```bash
DOCS="<paste Docs Path from your task description>"
PROJECT="<paste Project Path from your task description>"

# Move misplaced .md files from project root to Docs Path
for f in "$PROJECT"/*.md; do
  [ -f "$f" ] || continue
  BASENAME=$(basename "$f")
  # Skip well-known project files
  case "$BASENAME" in
    README.md|AGENTS.md|CHANGELOG.md|LICENSE*) continue ;;
  esac
  if [ ! -f "$DOCS/$BASENAME" ]; then
    mv "$f" "$DOCS/$BASENAME"
    echo "Moved $BASENAME -> docs/"
  fi
done

# Move misplaced .json files from project root to Docs Path (skip project config)
for f in "$PROJECT"/*.json; do
  [ -f "$f" ] || continue
  BASENAME=$(basename "$f")
  case "$BASENAME" in
    package.json|tsconfig.json|pyproject.json|poetry.lock) continue ;;
  esac
  if [ ! -f "$DOCS/$BASENAME" ]; then
    mv "$f" "$DOCS/$BASENAME"
    echo "Moved $BASENAME -> docs/"
  fi
done

# Move misplaced .txt and .log files from project root to Docs Path
for f in "$PROJECT"/*.txt "$PROJECT"/*.log; do
  [ -f "$f" ] || continue
  BASENAME=$(basename "$f")
  if [ ! -f "$DOCS/$BASENAME" ]; then
    mv "$f" "$DOCS/$BASENAME"
    echo "Moved $BASENAME -> docs/"
  fi
done

# Move misplaced diagnostic .py scripts from project root to Docs Path
for f in "$PROJECT"/*.py; do
  [ -f "$f" ] || continue
  BASENAME=$(basename "$f")
  case "$BASENAME" in
    run_server.py|run_monitor.py|setup.py|conftest.py|__init__.py) continue ;;
  esac
  if [ ! -f "$DOCS/$BASENAME" ]; then
    mv "$f" "$DOCS/$BASENAME"
    echo "Moved $BASENAME -> docs/"
  fi
done

# Move stray directories that look like diagnostic/agent output
for d in evidence plans scripts; do
  if [ -d "$PROJECT/$d" ]; then
    cp -r "$PROJECT/$d" "$DOCS/" 2>/dev/null
    rm -rf "$PROJECT/$d"
    echo "Moved $d/ -> docs/"
  fi
done

echo "Organization complete. Project root should now be clean."
```

After the sweep, verify the project root is clean:
```bash
ls "$PROJECT"/*.md 2>/dev/null
ls "$PROJECT"/*.txt 2>/dev/null
```

Only legitimate project files (README.md, AGENTS.md, etc.) should remain.

═══════════════════════════════════════════════════════════════════════
STEP 2: READ ALL DOCUMENTATION AND CODE
═══════════════════════════════════════════════════════════════════════

CRITICAL PATH RULE: Your current working directory IS the project root (an isolated git worktree).
- Write ALL code and tests inside your working directory (e.g. ./src, ./tests).
- "Project Path" = your working directory (.).  "Docs Path" = ./docs/ (create it if missing).
- Read the design document and prior inputs from ./.hephaestus/ (design.md, context.md, qa_spec.json).
- Do NOT use absolute paths outside your working directory. Do NOT write into ./.hephaestus/ (it is never merged to main).
- ALL docs/reports go in "Docs Path:" — not the project root.
- Code fixes go in "Project Path:" (src/, tests/, etc.).
- Your task description contains the exact paths — copy them exactly.

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

**Staleness check:** scan for forward-looking language that is now out of date —
phrases like "no source code exists", "to be created", "not yet implemented",
"will be added", or "planned". For each hit, verify against the current filesystem
and update the text to reflect what actually exists.

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
STEP 9: WRITE HTML FEATURE REPORT (MANDATORY)
═══════════════════════════════════════════════════════════════════════

Write `<Docs Path>/feature_report.html` — a polished, human-readable HTML
summary of what was built. This is what stakeholders see in the UI.

You have read ALL the artifacts. Use that knowledge to write something
genuinely useful: not a status table, but a clear narrative. Include:

- **What it does**: one paragraph plain-English description of the feature
- **Why it was built**: the original problem / design motivation
- **How it works**: key technical decisions, module structure, data flow
- **Quality signals**: test coverage, security findings, doc quality
- **What changed**: any architectural pivots from adversarial review
- **Known limitations or follow-up work** (from forensics if available)

The HTML must be self-contained (no external CSS/JS). Use inline styles.
Keep it professional — dark header, clean card layout, readable typography.
Write it as if presenting to a technical lead who wasn't in the room.

Save to: `<Docs Path>/feature_report.html`

═══════════════════════════════════════════════════════════════════════
STEP 10: FIX ALL DOCUMENTATION ISSUES (MANDATORY)
═══════════════════════════════════════════════════════════════════════

For EVERY documentation issue you find, you MUST fix it:

1. Read the affected file
2. Understand the discrepancy
3. Write the fix directly in the file
4. Verify the fix is correct
5. Document what you changed in the review report

DO NOT just report issues - FIX THEM. You have write access to all files.

═══════════════════════════════════════════════════════════════════════
STEP 11: SAVE TO MEMORY
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
