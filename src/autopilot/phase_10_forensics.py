"""
Phase 10: Forensics Analysis

After the feature is shipped, analyzes all agent outputs from the pipeline
to identify patterns, improve prompts, and refine methodologies for future runs.

The orchestrator writes two key artifacts for this phase:
- pipeline_metrics.json: real timing, iteration, and outcome data
- phase_prompts/: copies of the actual prompt files given to each agent
"""

from src.sdk.models import Phase

PHASE_10_FORENSICS = Phase(
    id=10,
    name="forensics_analysis",
    description="""Analyze all agent outputs and identify improvements for future pipeline runs.

After the feature is committed and shipped, this phase reviews every artifact
produced by the pipeline — requirements, architecture, code, reviews, security
findings, QA results, and validation — to identify prompt improvements,
methodology refinements, and patterns that could reduce iterations.""",
    done_definitions=[
        "pipeline_metrics.json read for timing and iteration data",
        "All phase prompts read from phase_prompts/ directory",
        "All phase artifacts read and compared against prompts",
        "Agent performance assessed per phase",
        "Common issue patterns cataloged",
        "Specific prompt rewrites proposed (with before/after text)",
        "forensics_report.md created in Docs Path",
        "Memory entries saved with feature-scoped tags",
        "Task marked as done",
    ],
    working_directory=None,
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE A FORENSICS ANALYST - IMPROVE THE PIPELINE
═══════════════════════════════════════════════════════════════════════

YOUR MISSION: Read real data, compare prompts to outcomes, propose fixes

═══════════════════════════════════════════════════════════════════════
STEP 0: READ YOUR TASK DESCRIPTION FOR PATHS
═══════════════════════════════════════════════════════════════════════

Your task description contains:
- "Docs Path:" — where all generated reports and metrics are stored
- "Project Path:" — where the implementation code lives
- "Feature Folder:" — the feature-level directory

All your reads come from the "Docs Path" location.

═══════════════════════════════════════════════════════════════════════
STEP 1: READ PIPELINE METRICS (REAL DATA)
═══════════════════════════════════════════════════════════════════════

Read `<Docs Path>/pipeline_metrics.json`. This contains:
- iterations: how many times the pipeline ran
- total_time_seconds: wall-clock time
- stop_reason: why it stopped
- qa_passed / product_validated: final outcomes
- phases: list of phases with their expected output files
- started_at / completed_at: timestamps
- cost_total: LLM spend if LiteLLM was configured

This is your source of truth for metrics. Do NOT guess or hallucinate numbers.

═══════════════════════════════════════════════════════════════════════
STEP 2: READ ACTUAL AGENT PROMPTS
═══════════════════════════════════════════════════════════════════════

Read all files in `<Docs Path>/phase_prompts/`. These are the actual
Python files containing the prompt text given to each agent.

For each phase file, extract:
- The `description` field — what the agent was told it would do
- The `done_definitions` — what "done" looks like
- The `additional_notes` — the step-by-step instructions

These are the ACTUAL prompts. Do not guess or paraphrase them.

═══════════════════════════════════════════════════════════════════════
STEP 3: READ ALL PHASE OUTPUTS
═══════════════════════════════════════════════════════════════════════

Read each artifact from the Docs Path:
- requirements_analysis.md
- architecture.md
- review_report.md
- doc_review_report.md
- security_report.md
- qa_report.md
- product_validation.md

Also read the original design document (copied to artifacts/).

For each output, compare what was produced against:
1. The prompt that was given (from Step 2)
2. The original design document (the source of truth)

═══════════════════════════════════════════════════════════════════════
STEP 4: COMPARE PROMPTS TO OUTCOMES
═══════════════════════════════════════════════════════════════════════

For each phase, answer these questions using EVIDENCE from the artifacts:

### Did the agent follow its instructions?
- Read the prompt (from phase_prompts/)
- Read the output (from Docs Path)
- Did the output include everything the prompt required?
- Did the output include things the prompt didn't ask for?
- Were any instructions ignored or misunderstood?

### Was the output quality adequate?
- Compare requirements_analysis.md against the original design doc
- Did requirements cover everything in the design?
- Does the code implement what architecture.md specifies?
- Did the reviewer find issues that the developer should have caught?
- Did the security agent find issues the reviewer should have caught?

### What caused iterations?
- If iterations > 1, compare what changed between iterations
- Which phase's failures caused rework?
- Were the same issues found in multiple iterations?

═══════════════════════════════════════════════════════════════════════
STEP 5: IDENTIFY PATTERNS
═══════════════════════════════════════════════════════════════════════

Look for patterns across all phases:

### Issue Escalation
- What did the reviewer find that the developer should have caught?
- What did the security agent find that the reviewer should have caught?
- What did QA find that earlier phases should have caught?
- Each escalation = a prompt gap in the earlier phase.

### Prompt-Output Gaps
- Where the prompt asked for X but the output didn't include it
- Where the prompt was ambiguous and the agent guessed wrong
- Where the prompt was contradictory

### Context Loss
- What information did Phase N produce that Phase N+1 needed but didn't have?
- What information from the original design was lost between phases?

═══════════════════════════════════════════════════════════════════════
STEP 6: WRITE FORENSICS REPORT
═══════════════════════════════════════════════════════════════════════

Write forensics_report.md to the "Docs Path" location.

Structure the report around what you ACTUALLY found, not a rigid template.
Use this structure but fill sections proportionally to findings:

# Forensics Report

## Pipeline Metrics
(From pipeline_metrics.json — copy the real numbers)

## Findings
(One section per finding. Each finding has:)
- **What happened:** [evidence from artifacts]
- **Root cause:** [which phase, which prompt gap]
- **Recommendation:** [specific change]

## Prompt Rewrites
(Only for phases that need changes. Each has:)
- **Phase:** [name]
- **Current prompt:** [exact text from phase_prompts/]
- **Problem:** [what went wrong, with evidence]
- **Proposed prompt:** [new text]

## Patterns
(Recurring issues across multiple findings)

## Summary
(3-5 bullet points: highest-impact improvements)

Keep the report focused. Do not pad with boilerplate.
If a phase performed well, say so in one line and move on.

═══════════════════════════════════════════════════════════════════════
STEP 7: SAVE TO MEMORY (SCOPED)
═══════════════════════════════════════════════════════════════════════

Save improvements to memory, scoped to this feature:

```python
mcp__hephaestus__save_memory({
    "content": f"Forensics [{feature_name}]: [finding]. "
               f"Applies to Phase [N]. Proposed fix: [fix].",
    "memory_type": "learning",
    "tags": ["forensics", "<feature_name>", "phase_<n>"]
})
```

The <feature_name> tag ensures future searches only find relevant findings.

Note: These improvements are recommendations. They will be surfaced to
future pipeline runs via search_memory, but prompt changes require human
review before being applied to the phase definitions.

═══════════════════════════════════════════════════════════════════════
RULES
═══════════════════════════════════════════════════════════════════════

DO:
- Read pipeline_metrics.json for real numbers (don't guess)
- Read phase_prompts/ for real prompt text (don't paraphrase from memory)
- Cite specific lines from artifacts as evidence
- Propose concrete prompt rewrites with before/after text
- Scope memory entries with the feature name
- Skip phases that performed well (one-line acknowledgment)

DO NOT:
- Hallucinate metrics (iterations, timing, costs)
- Guess what prompts said — read the actual files
- Write 200-line templates — fill sections proportionally to findings
- Give generic advice ("be more specific")
- Analyze yourself (Phase 10) — you can't objectively self-assess
""",
    outputs=[
        "forensics_report.md with evidence-based analysis",
        "Specific prompt rewrites with before/after text",
        "Feature-scoped memory entries for future improvement",
    ],
    next_steps=[
        "Human reviews forensics report",
        "Prompt improvements applied to phase definitions",
        "Next pipeline run benefits from saved learnings",
    ],
)
