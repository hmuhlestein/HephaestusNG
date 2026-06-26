"""
Autopilot Multi-Agent Workflow - Python Phase Definitions

A fully automated pipeline that takes design documents and iterates through:
1. Product Requirements Extraction (context-aware)
2. Architecture & Design
3. Development
4. Adversarial Code Review
5. Documentation Review
6. Security Review
7. QA Testing & Validation
8. Product Validation (final spec check)
9. Git Commit & Push
10. Forensics Analysis (pipeline self-improvement)

The workflow loops until the original intent is satisfied or a hard stop
condition is met (hard error, impasse, or major architectural issue).

Designed to run continuously, picking designs from a queue and processing
them through the full pipeline until complete.
"""

import hashlib
import re
from pathlib import Path
from src.autopilot.phase_1_product_requirements import PHASE_1_PRODUCT_REQUIREMENTS
from src.autopilot.phase_2_architecture import PHASE_2_ARCHITECTURE
from src.autopilot.phase_3_development import PHASE_3_DEVELOPMENT
from src.autopilot.phase_4_adversarial_review import PHASE_4_ADVERSARIAL_REVIEW
from src.autopilot.phase_5_doc_review import PHASE_5_DOC_REVIEW
from src.autopilot.phase_6_security_review import PHASE_6_SECURITY_REVIEW
from src.autopilot.phase_7_qa_validation import PHASE_7_QA_VALIDATION
from src.autopilot.phase_8_product_validation import PHASE_8_PRODUCT_VALIDATION
from src.autopilot.phase_9_git_commit_push import PHASE_9_GIT_COMMIT_PUSH
from src.autopilot.phase_10_forensics import PHASE_10_FORENSICS

from src.sdk.models import WorkflowConfig, LaunchTemplate, LaunchParameter

AUTOPILOT_PHASES = [
    PHASE_1_PRODUCT_REQUIREMENTS,
    PHASE_2_ARCHITECTURE,
    PHASE_3_DEVELOPMENT,
    PHASE_4_ADVERSARIAL_REVIEW,
    PHASE_5_DOC_REVIEW,
    PHASE_6_SECURITY_REVIEW,
    PHASE_7_QA_VALIDATION,
    PHASE_8_PRODUCT_VALIDATION,
    PHASE_10_FORENSICS,       # forensics runs before commit so worktree is still valid
    PHASE_9_GIT_COMMIT_PUSH,  # commit/merge last — removes the worktree
]

# Session role mapping — determines which phases share a persistent session.
# Phases with the same session_role reuse the same pi session, preserving
# full conversational context across gotos and the architect review (§10.1.1).
# Key = phase name, Value = session role slug.
SESSION_ROLES = {
    "product_requirements": "product-requirements",
    "architecture_design": "architect",
    "development": "developer",
    "adversarial_review": "architect",  # reuses architect session (§10.1.1)
    "doc_review": "doc-reviewer",
    "security_review": "security-reviewer",
    "qa_validation": "qa",
    "product_validation": "product-requirements",  # reuses requirements session
    "git_commit_push": "git",
    "forensics_analysis": "forensics",
}


def get_session_id(project_id: str, design_slug: str, phase_name: str) -> str:
    """Generate a deterministic session ID for a phase.

    Same project + design + role = same session. This means:
    - Goto back to development → developer session resumes with full memory.
    - Architect re-invoked for adversarial review → architect session resumes.
    - Any phase retry → same session, agent picks up where it left off.

    Pi handles storage internally — we just pass the ID via --session-id.
    """
    role = SESSION_ROLES.get(phase_name, phase_name)
    safe = lambda s: re.sub(r'[^a-z0-9\-_]', '', s.lower().replace(' ', '-'))[:30]
    # Stable hash suffix prevents collisions between similar names
    # e.g. 'my-proj-add-calc' vs 'my-proj-add-calculator'
    raw = f"{project_id}:{design_slug}:{role}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"hephaestus-{safe(project_id)}-{safe(design_slug)}-{safe(role)}-{h}"

# Orchestrator config for the autopilot workflow
# Defines evaluation points and flow control logic
AUTOPILOT_ORCHESTRATOR_CONFIG = {
    "type": "evaluating",  # Use evaluation-based flow control
    "max_phase_retries": 2,
    "max_total_gotos": 30,  # Safety limit: max 30 GOTO operations per design
    "evaluation_points": [
        # After architecture design - check quality, can retry or continue
        {
            "after_phase": "architecture_design",
            "evaluator": "heuristic",
            "conditions": [
                {"if": "score < 0.4", "action": "goto", "target": "product_requirements", "reason": "Architecture fundamentally flawed, re-examine requirements"},
                {"if": "score < 0.6", "action": "retry", "reason": "Architecture needs improvement"},
                {"if": "score >= 0.6", "action": "continue", "reason": "Architecture approved"},
            ],
            "max_retries": 2,
        },
        # After development - always continue to reviews
        # (reviews will loop back if issues found)
        {
            "after_phase": "development",
            "evaluator": "heuristic",
            "conditions": [
                {"if": "score >= 0.0", "action": "continue", "reason": "Development complete, proceeding to review"},
            ],
            "max_retries": 0,
        },
        # After adversarial review - loops back to development for fixes,
        # then continues to QA. High max_retries lets the architect keep
        # pushing until the code is genuinely clean.
        {
            "after_phase": "adversarial_review",
            "evaluator": "heuristic",
            "conditions": [
                {"if": "score < 0.3", "action": "goto", "target": "architecture_design", "reason": "Major architectural issues found, returning to architecture"},
                {"if": "score < 0.6", "action": "goto", "target": "development", "reason": "Code issues found, returning to development"},
                {"if": "score >= 0.6", "action": "continue", "reason": "Adversarial review passed"},
            ],
            "max_retries": 15,
        },
        # After doc review - can jump to architecture or development
        {
            "after_phase": "doc_review",
            "evaluator": "heuristic",
            "conditions": [
                {"if": "score < 0.3", "action": "goto", "target": "architecture_design", "reason": "Documentation gaps indicate architectural issues"},
                {"if": "score < 0.6", "action": "goto", "target": "development", "reason": "Documentation needs code-level fixes"},
                {"if": "score >= 0.6", "action": "continue", "reason": "Documentation approved"},
            ],
            "max_retries": 2,
        },
        # After security review - can jump to architecture or development
        {
            "after_phase": "security_review",
            "evaluator": "heuristic",
            "conditions": [
                {"if": "score < 0.3", "action": "goto", "target": "architecture_design", "reason": "Security issues require architectural changes"},
                {"if": "score < 0.7", "action": "goto", "target": "development", "reason": "Security issues found, returning to development"},
                {"if": "score >= 0.7", "action": "continue", "reason": "Security review passed"},
            ],
            "max_retries": 2,
        },
        # After QA validation - can jump to architecture or development
        {
            "after_phase": "qa_validation",
            "evaluator": "heuristic",
            "conditions": [
                {"if": "score < 0.3", "action": "goto", "target": "architecture_design", "reason": "Test failures indicate architectural problems"},
                {"if": "score < 0.7", "action": "goto", "target": "development", "reason": "QA failed, returning to development"},
                {"if": "score >= 0.7", "action": "continue", "reason": "QA passed"},
            ],
            "max_retries": 2,
        },
        # After product validation - can jump back if not validated
        {
            "after_phase": "product_validation",
            "evaluator": "heuristic",
            "conditions": [
                {"if": "score < 0.3", "action": "goto", "target": "architecture_design", "reason": "Product validation failed, needs architectural review"},
                {"if": "score < 0.7", "action": "goto", "target": "development", "reason": "Product validation failed, returning to development"},
                {"if": "score >= 0.7", "action": "continue", "reason": "Product validated, proceeding to commit"},
            ],
            "max_retries": 2,
        },
        # Forensics runs before commit — worktree is still valid at this point
        {
            "after_phase": "forensics_analysis",
            "evaluator": "heuristic",
            "conditions": [
                {"if": "score >= 0.0", "action": "continue", "reason": "Forensics complete, proceeding to commit"},
            ],
            "max_retries": 0,
        },
        # Git commit is the final phase — merges to main; worktree is kept for UI
        {
            "after_phase": "git_commit_push",
            "evaluator": "heuristic",
            "conditions": [
                {"if": "score >= 0.0", "action": "continue", "reason": "Git commit complete"},
            ],
            "max_retries": 0,
        },
    ],
}

AUTOPILOT_WORKFLOW_CONFIG = WorkflowConfig(
    has_result=True,
    result_criteria="Feature validated and committed to git, ready for human review",
    on_result_found="do_nothing",
    enable_tickets=True,
    board_config={
        "columns": [
            {"id": "backlog", "name": "Backlog", "order": 1, "color": "#94a3b8"},
            {"id": "requirements", "name": "Requirements", "order": 2, "color": "#3b82f6"},
            {"id": "architecture", "name": "Architecture", "order": 3, "color": "#8b5cf6"},
            {"id": "in-progress", "name": "In Progress", "order": 4, "color": "#f59e0b"},
            {"id": "review", "name": "In Review", "order": 5, "color": "#ec4899"},
            {"id": "doc-review", "name": "Doc Review", "order": 6, "color": "#06b6d4"},
            {"id": "security", "name": "Security", "order": 7, "color": "#ef4444"},
            {"id": "qa", "name": "QA", "order": 8, "color": "#14b8a6"},
            {"id": "validated", "name": "Validated", "order": 9, "color": "#22c55e"},
            {"id": "shipped", "name": "Shipped", "order": 10, "color": "#3b82f6"},
        ],
        "ticket_types": ["infrastructure", "feature", "bug-fix", "security", "integration", "documentation"],
        "default_ticket_type": "feature",
        "initial_status": "backlog",
        "auto_assign": True,
        "require_comments_on_status_change": True,
        "allow_reopen": True,
        "track_time": True,
    },
)

AUTOPILOT_LAUNCH_TEMPLATE = LaunchTemplate(
    parameters=[
        LaunchParameter(
            name="design_document",
            label="Design Document Path",
            type="text",
            required=True,
            description="Path to the design document (PRD, DESIGN.md, etc.)"
        ),
        LaunchParameter(
            name="project_path",
            label="Project Working Directory",
            type="text",
            required=True,
            description="Where to build the feature"
        ),
        LaunchParameter(
            name="project_context",
            label="Project Context (Optional)",
            type="textarea",
            required=False,
            description="Optional: Additional context about the larger project vision"
        ),
    ],
    phase_1_task_prompt="""Phase 1: Product Requirements Extraction

**Design Document:** ./.hephaestus/design.md (copied into your worktree)
**Project Path:** . (your current working directory — an isolated git worktree)

---

## Additional Context
{project_context}

---

## Your Task

You are extracting requirements from the design document.

### STEP 0: Gather Project Context
Before reading the design document:
1. Check for existing requirements_analysis.md, architecture.md
2. Look in features/ directory for previously completed features
3. Read existing source code to understand the current system
4. Search memory for technology decisions and constraints

### STEP 1: Read the Design Document
Read the file at: ./.hephaestus/design.md

### STEP 2: Extract Requirements
- Functional requirements with acceptance criteria
- Non-functional requirements
- Integration points with existing system
- Technology constraints

### STEP 3: Create Requirements Document
Write requirements_analysis.md in ./docs/ (create the directory if needed)

### STEP 4: Save to Memory
Save key decisions and project context.

### STEP 5: Create Phase 2 Task
Create a Phase 2 task with full requirements and context.

### STEP 6: Mark Done
Mark your task as done.
""",
)

__all__ = [
    "AUTOPILOT_PHASES",
    "AUTOPILOT_WORKFLOW_CONFIG",
    "AUTOPILOT_LAUNCH_TEMPLATE",
]
