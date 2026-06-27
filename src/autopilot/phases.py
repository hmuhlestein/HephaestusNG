"""
Autopilot Multi-Agent Workflow - Phase Assembly

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

from src.autopilot.phase_loader import (
    build_phase,
    load_autopilot_config,
    load_launch_template,
    load_workflow_config,
)

# Load config once at module level
_cfg = load_autopilot_config()
_default_model = _cfg.get("default_model", "xiaomi/mimo-v2.5")
_default_thinking = _cfg.get("default_thinking_level", "low")

# Index phase configs by id for easy lookup
_phase_cfgs = {pc["id"]: pc for pc in _cfg["phases"]}

# Build all Phase objects — additional_notes is now read from YAML phase_cfg
_phases_by_id = {
    pc["id"]: build_phase(pc, _default_model, _default_thinking)
    for pc in _cfg["phases"]
}

# Session role mapping — loaded from YAML.
# Phases with the same session_role reuse the same pi session, preserving
# full conversational context across gotos and the architect review (§10.1.1).
# Key = phase name, Value = session role slug.
SESSION_ROLES = _cfg["session_roles"]


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


# Orchestrator config — loaded from YAML
AUTOPILOT_ORCHESTRATOR_CONFIG = _cfg["orchestrator"]

# Workflow config — assembled from YAML
AUTOPILOT_WORKFLOW_CONFIG = load_workflow_config(_cfg)

# Launch template — parameters from YAML, phase_1_task_prompt stays in Python (it's a prompt)
_phase_1_task_prompt = """Phase 1: Product Requirements Extraction

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
"""

AUTOPILOT_LAUNCH_TEMPLATE = load_launch_template(_cfg, _phase_1_task_prompt)

# Preserve the forensics-before-git ordering:
# forensics (id=10) runs before git commit (id=9) so the worktree is still valid.
AUTOPILOT_PHASES = [
    _phases_by_id[1],
    _phases_by_id[2],
    _phases_by_id[3],
    _phases_by_id[4],
    _phases_by_id[5],
    _phases_by_id[6],
    _phases_by_id[7],
    _phases_by_id[8],
    _phases_by_id[10],  # forensics runs before commit so worktree is still valid
    _phases_by_id[9],   # commit/merge last — removes the worktree
]

__all__ = [
    "AUTOPILOT_PHASES",
    "AUTOPILOT_WORKFLOW_CONFIG",
    "AUTOPILOT_LAUNCH_TEMPLATE",
    "AUTOPILOT_ORCHESTRATOR_CONFIG",
    "SESSION_ROLES",
    "get_session_id",
]
