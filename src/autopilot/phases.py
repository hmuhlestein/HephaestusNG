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
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from src.workflow_engine.yaml_loader import (
    build_phase_list,
    load_launch_template,
    load_workflow_config,
    load_workflow_from_dir,
)

logger = logging.getLogger(__name__)

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
    project_id: str, design_slug: str, phase_name: str, model: str = "", workflow_id: str = ""
) -> str:
    """Generate a deterministic session ID for a phase.

    Same project + design + role + model + workflow = same session. This means:
    - Goto back to development → developer session resumes with full memory.
    - Architect re-invoked for adversarial review → architect session resumes.
    - Any phase retry → same session, agent picks up where it left off.

    workflow_id is part of the hash so a NEW workflow never resumes a
    PRIOR workflow's session for the same project+design+phase+model --
    e.g. delete_feature wipes a stuck workflow's DB rows and a fresh
    workflow later redoes the same design. Without workflow_id, the two
    attempts hashed identically, so the new agent silently resumed the
    deleted workflow's live CLI session -- replaying its stale
    conversation tail (old task IDs, already-resolved complete_my_task
    calls) into what should have been a brand new agent (observed live:
    workflow e35be066's product_requirements session resurfaced under
    workflow e9019930 after e35be066 was deleted via delete_feature).

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
    raw = f"{project_id}:{design_slug}:{workflow_id}:{role}:{model}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"hephaestus-{safe(project_id)}-{safe(design_slug)}-{safe(role)}-{h}"


def capture_workflow_session_info(db, workflow_ids: List[str]) -> List[Dict[str, Any]]:
    """Snapshot what cleanup_workflow_sessions() needs before workflow_ids'
    Phase/Workflow rows are deleted.

    Every "delete these workflows" call site (delete_feature, RepairService.
    rerun, remove_project_design) needs this same snapshot-then-cleanup
    two-step -- captured here once instead of duplicated at each site, same
    reasoning as the worktree-cleanup lists those call sites already build
    for themselves. MUST be called with `db` still holding the Workflow and
    Phase rows for workflow_ids (i.e. before their DELETE statements run) --
    working_directory, launch_params, and each phase's name/cli_model are
    gone once those rows are.
    """
    from src.core.database import Phase, Workflow

    infos = []
    for wf in db.query(Workflow).filter(Workflow.id.in_(workflow_ids)).all():
        if not wf.working_directory:
            continue
        launch_params = wf.launch_params if isinstance(wf.launch_params, dict) else {}
        phase_role_models = [
            (p.name, p.cli_model)
            for p in db.query(Phase.name, Phase.cli_model).filter(Phase.workflow_id == wf.id).all()
        ]
        if phase_role_models:
            infos.append({
                "workflow_id": wf.id,
                "working_directory": wf.working_directory,
                "launch_params": launch_params,
                "phase_role_models": phase_role_models,
            })
    return infos


def cleanup_workflow_sessions(session_infos: List[Dict[str, Any]]) -> int:
    """Best-effort removal of orphaned CLI session files for deleted workflows.

    get_session_id is scoped by workflow_id, so no FUTURE workflow can ever
    be handed one of these sessions again -- but the session file(s) on disk
    still exist as dangling artifacts of a now-deleted workflow, carrying a
    conversation (old task IDs, already-resolved complete_my_task calls)
    that has nothing to do with whatever redoes this design next. Call this
    with capture_workflow_session_info()'s output AFTER the workflow/phase
    delete has committed -- pure filesystem work, no DB access. Never
    raises: logs and continues past any single workflow it can't resolve so
    one bad entry doesn't block cleanup of the rest. Returns the number of
    session files removed.
    """
    from src.services.cost_collection_service import (
        _discover_claude_session_file,
        _discover_session_file,
    )

    removed = 0
    for info in session_infos:
        workflow_id = info["workflow_id"]
        launch_params = info["launch_params"]
        project_id = launch_params.get("project_id") or launch_params.get("project_path", "")
        design_slug = (
            launch_params.get("design_slug")
            or launch_params.get("design_id")
            or launch_params.get("feature_id", "")
        )
        if not project_id or not design_slug:
            continue
        try:
            for phase_name, model in info["phase_role_models"]:
                session_id = get_session_id(
                    project_id, design_slug, phase_name, model=model or "", workflow_id=workflow_id
                )
                for discover in (_discover_session_file, _discover_claude_session_file):
                    session_file = discover(session_id, info["working_directory"])
                    if session_file:
                        session_file.unlink(missing_ok=True)
                        removed += 1
        except Exception as e:
            logger.warning(f"[SESSION-CLEANUP] Failed to clean up CLI sessions for workflow {workflow_id}: {e}")
    return removed


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
    "capture_workflow_session_info",
    "cleanup_workflow_sessions",
]
