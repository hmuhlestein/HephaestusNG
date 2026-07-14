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
from pathlib import Path

from src.workflow_engine.yaml_loader import (
    build_phase_list,
    load_launch_template,
    load_workflow_config,
    load_workflow_from_dir,
)

_WORKFLOW_DIR = (
    Path(__file__).parent.parent.parent / "config" / "workflows" / "autopilot"
)

# Load config once at module level
_cfg = load_workflow_from_dir(_WORKFLOW_DIR)

# Session role mapping — loaded from YAML.
# Phases with the same session_role reuse the same pi session, preserving
# full conversational context across gotos and the architect review (§10.1.1).
# Key = phase name, Value = session role slug.
SESSION_ROLES = _cfg["session_roles"]


def get_session_id(
    project_id: str, design_slug: str, phase_name: str, model: str = ""
) -> str:
    """Generate a deterministic session ID for a phase.

    Same project + design + role + model = same session. This means:
    - Goto back to development → developer session resumes with full memory.
    - Architect re-invoked for adversarial review → architect session resumes.
    - Any phase retry → same session, agent picks up where it left off.

    Pi handles storage internally — we just pass the ID via --session-id.

    model is part of the hash, not just informational: pi's --session-id
    resume permanently pins whatever model the session was FIRST created
    with -- a --model flag passed on a later resume is silently ignored
    (confirmed live: a session's own file had exactly one modelId entry,
    recorded at creation, unchanged across 343 subsequent turns). Without
    the model in this hash, changing the configured model (e.g. switching
    off a model whose output-token ceiling turned out too small) has zero
    effect on any EXISTING session for a role that's already been used --
    every goto back to that role keeps resuming the stale session on the
    old model forever, silently, no matter what config says now. Folding
    the model in means a model change naturally produces a different
    session_id, so pi treats it as a fresh session instead of resuming the
    pinned one -- continuity is preserved exactly as long as the model
    doesn't change, which is the common case.
    """
    role = SESSION_ROLES.get(phase_name, phase_name)
    def safe(s):
        return re.sub(r"[^a-z0-9\-_]", "", s.lower().replace(" ", "-"))[:30]
    # Stable hash suffix prevents collisions between similar names
    # e.g. 'my-proj-add-calc' vs 'my-proj-add-calculator'
    raw = f"{project_id}:{design_slug}:{role}:{model}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"hephaestus-{safe(project_id)}-{safe(design_slug)}-{safe(role)}-{h}"


# Orchestrator config — loaded from YAML
AUTOPILOT_ORCHESTRATOR_CONFIG = _cfg["orchestrator"]

# Workflow config — assembled from YAML
AUTOPILOT_WORKFLOW_CONFIG = load_workflow_config(_cfg)

AUTOPILOT_LAUNCH_TEMPLATE = load_launch_template(_cfg)

# Execution order defined in workflow.yaml (execution_order field).
AUTOPILOT_PHASES = build_phase_list(_cfg)

__all__ = [
    "AUTOPILOT_PHASES",
    "AUTOPILOT_WORKFLOW_CONFIG",
    "AUTOPILOT_LAUNCH_TEMPLATE",
    "AUTOPILOT_ORCHESTRATOR_CONFIG",
    "SESSION_ROLES",
    "get_session_id",
]
