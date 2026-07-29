"""Centralized cost derivation and rollup for Hephaestus entities.

This module provides the single source of truth for deriving cost totals
from the cost_entries ledger, following the same self-healing pattern as
status_derivation.py.

Usage:
    from src.core.cost_derivation import record_cost, derive_task_cost

    # Record a new cost entry (triggers rollup)
    entry = record_cost(db, task_id="...", cost_usd=0.05, source="pi", ...)

    # Derive task cost from entries (self-healing)
    cost = derive_task_cost(db, task_id="...", write_back=True)
"""

import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core.database import (
    AutopilotDesign,
    AutopilotProject,
    CostEntry,
    Feature,
    Task,
    Workflow,
)

logger = logging.getLogger(__name__)


def record_cost(
    db: Session,
    cost_usd: float,
    source: str,
    task_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
    model: Optional[str] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    raw_usage: Optional[dict] = None,
) -> CostEntry:
    """Record a new cost entry and trigger rollup.

    This is the primary entry point for recording costs. It creates the
    CostEntry row and then derives/updates cost_total_usd on the related
    entities (Task, Feature, AutopilotDesign, AutopilotProject).

    Args:
        db: Database session
        cost_usd: Cost in dollars
        source: Cost source ('pi', 'claude_code', 'opencode', 'codex', 'openrouter_direct')
        task_id: Optional task ID
        agent_id: Optional agent ID
        workflow_id: Optional workflow ID (auto-derived from task if not provided)
        model: Optional model name (e.g. "anthropic/claude-sonnet-4")
        input_tokens: Input token count
        output_tokens: Output token count
        cache_read_tokens: Cache read token count
        cache_write_tokens: Cache write token count
        reasoning_tokens: Reasoning token count
        raw_usage: Raw usage data for debugging

    Returns:
        The created CostEntry
    """
    # Validate cost_usd
    if cost_usd < 0:
        raise ValueError("cost_usd must be non-negative")
    if cost_usd > 1000.0:
        logger.warning(f"[COST] Capping unusually high cost ${cost_usd:.2f} to $1000")
        cost_usd = 1000.0

    # Auto-derive workflow_id from task if not provided
    if workflow_id is None and task_id is not None:
        task = db.query(Task).filter_by(id=task_id).first()
        if task:
            workflow_id = task.workflow_id

    entry = CostEntry(
        id=f"cost-{uuid.uuid4().hex[:8]}",
        task_id=task_id,
        agent_id=agent_id,
        workflow_id=workflow_id,
        source=source,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        cost_usd=cost_usd,
        recorded_at=datetime.utcnow(),
        raw_usage=raw_usage,
    )

    db.add(entry)
    db.flush()  # Get the ID without committing

    # Trigger rollup derivation
    if task_id:
        derive_task_cost(db, task_id, write_back=True)
    if workflow_id:
        derive_workflow_cost(db, workflow_id, write_back=True)

    return entry


def derive_task_cost(db: Session, task_id: str, write_back: bool = True) -> float:
    """Derive task cost from its cost_entries.

    This is the SINGLE source of truth for task cost. All code paths
    that need to know a task's cost should call this function instead
    of reading Task.cost_total_usd directly.

    Args:
        db: Database session
        task_id: Task ID to derive cost for
        write_back: If True, update Task.cost_total_usd in DB when it disagrees

    Returns:
        Derived cost in USD
    """
    task = db.query(Task).filter_by(id=task_id).first()
    if not task:
        logger.warning(f"Task {task_id} not found for cost derivation")
        return 0.0

    # Sum cost entries for this task
    total = db.query(func.sum(CostEntry.cost_usd)).filter(CostEntry.task_id == task_id).scalar() or 0.0

    # Self-heal: write back to DB if cost disagrees (no commit — caller handles)
    if write_back and abs(total - task.cost_total_usd) > 0.0001:
        logger.info(f"[COST-HEAL] Task {task_id[:8]} cost: ${task.cost_total_usd:.4f} -> ${total:.4f}")
        task.cost_total_usd = total

    return total


def derive_workflow_cost(db: Session, workflow_id: str, write_back: bool = True) -> float:
    """Derive workflow cost from its cost_entries.

    Args:
        db: Database session
        workflow_id: Workflow ID to derive cost for
        write_back: If True, persist Workflow.cost_total_usd and roll up

    Returns:
        Derived cost in USD
    """
    workflow = db.query(Workflow).filter_by(id=workflow_id).first()
    if not workflow:
        logger.warning(f"Workflow {workflow_id} not found for cost derivation")
        return 0.0

    # Sum cost entries for this workflow
    total = db.query(func.sum(CostEntry.cost_usd)).filter(CostEntry.workflow_id == workflow_id).scalar() or 0.0

    # Self-heal: write back to DB if cost disagrees (no commit — caller handles)
    if write_back and abs(total - workflow.cost_total_usd) > 0.0001:
        logger.info(f"[COST-HEAL] Workflow {workflow_id[:8]} cost: ${workflow.cost_total_usd:.4f} -> ${total:.4f}")
        workflow.cost_total_usd = total

    # Roll up to feature/design/project
    if workflow.feature_id:
        derive_feature_cost(db, workflow.feature_id, write_back=write_back)
    if workflow.design_id:
        derive_design_cost(db, workflow.design_id, write_back=write_back)
    if workflow.project_id:
        derive_project_cost(db, workflow.project_id, write_back=write_back)

    return total


def derive_feature_cost(db: Session, feature_id: str, write_back: bool = True) -> float:
    """Derive feature cost from its workflows' cost_entries.

    Args:
        db: Database session
        feature_id: Feature ID to derive cost for
        write_back: If True, update Feature.cost_total_usd in DB when it disagrees

    Returns:
        Derived cost in USD
    """
    feature = db.query(Feature).filter_by(id=feature_id).first()
    if not feature:
        logger.warning(f"Feature {feature_id} not found for cost derivation")
        return 0.0

    # Sum cost entries for all workflows associated with this feature
    # via the workflow's feature_id
    total = db.query(func.sum(CostEntry.cost_usd)).join(Workflow, CostEntry.workflow_id == Workflow.id).filter(Workflow.feature_id == feature_id).scalar() or 0.0

    # Self-heal: write back to DB if cost disagrees (no commit — caller handles)
    if write_back and abs(total - feature.cost_total_usd) > 0.0001:
        logger.info(f"[COST-HEAL] Feature {feature_id[:8]} cost: ${feature.cost_total_usd:.4f} -> ${total:.4f}")
        feature.cost_total_usd = total

    return total


def derive_design_cost(db: Session, design_id: str, write_back: bool = True) -> float:
    """Derive design cost from its features' cost_entries.

    Args:
        db: Database session
        design_id: Design ID to derive cost for
        write_back: If True, update AutopilotDesign.cost_total_usd in DB when it disagrees

    Returns:
        Derived cost in USD
    """
    design = db.query(AutopilotDesign).filter_by(id=design_id).first()
    if not design:
        logger.warning(f"Design {design_id} not found for cost derivation")
        return 0.0

    # Sum cost entries for all features' workflows associated with this design
    # Primary path: through Feature
    via_feature = (
        db.query(func.sum(CostEntry.cost_usd)).join(Workflow, CostEntry.workflow_id == Workflow.id).join(Feature, Workflow.feature_id == Feature.id).filter(Feature.design_id == design_id).scalar()
        or 0.0
    )
    # Direct path: workflows linked to design without feature (Phase 0, etc.)
    direct = db.query(func.sum(CostEntry.cost_usd)).join(Workflow, CostEntry.workflow_id == Workflow.id).filter(Workflow.design_id == design_id, Workflow.feature_id.is_(None)).scalar() or 0.0
    total = via_feature + direct

    # Self-heal: write back to DB if cost disagrees (no commit — caller handles)
    if write_back and abs(total - design.cost_total_usd) > 0.0001:
        logger.info(f"[COST-HEAL] Design {design_id[:8]} cost: ${design.cost_total_usd:.4f} -> ${total:.4f}")
        design.cost_total_usd = total

    return total


def derive_project_cost(db: Session, project_id: str, write_back: bool = True) -> float:
    """Derive project cost from its designs' cost_entries.

    Also checks budget enforcement after updating.

    Args:
        db: Database session
        project_id: Project ID to derive cost for
        write_back: If True, update AutopilotProject.cost_total_usd in DB when it disagrees

    Returns:
        Derived cost in USD
    """
    project = db.query(AutopilotProject).filter_by(id=project_id).first()
    if not project:
        logger.warning(f"Project {project_id} not found for cost derivation")
        return 0.0

    # Sum cost entries for all designs' features' workflows associated with this project
    # Primary path: through Feature -> Design
    via_feature = (
        db.query(func.sum(CostEntry.cost_usd))
        .join(Workflow, CostEntry.workflow_id == Workflow.id)
        .join(Feature, Workflow.feature_id == Feature.id)
        .join(AutopilotDesign, Feature.design_id == AutopilotDesign.id)
        .filter(AutopilotDesign.project_id == project_id)
        .scalar()
        or 0.0
    )
    # Direct path: workflows linked to project without feature (Phase 0, etc.)
    direct = db.query(func.sum(CostEntry.cost_usd)).join(Workflow, CostEntry.workflow_id == Workflow.id).filter(Workflow.project_id == project_id, Workflow.feature_id.is_(None)).scalar() or 0.0
    total = via_feature + direct

    # Self-heal: write back to DB if cost disagrees (no commit — caller handles)
    if write_back and abs(total - project.cost_total_usd) > 0.0001:
        logger.info(f"[COST-HEAL] Project {project_id[:8]} cost: ${project.cost_total_usd:.4f} -> ${total:.4f}")
        project.cost_total_usd = total

    # Check budget enforcement (caller will commit)
    if write_back:
        _check_budget_enforcement(db, project)

    return total


def _pause_project_workflows(db: Session, project_id: str, paused_by: str, definition_ids: tuple = None) -> int:
    """Thin wrapper around orchestrator.pause_project_workflows, imported
    lazily to avoid a circular import (orchestrator.py imports from this
    module too). Exists as a module-level name here -- rather than an
    inline import inside _check_budget_enforcement -- so callers that only
    need the pause behavior (e.g. budget-enforcement tests) don't have to
    reach into src.autopilot.orchestrator directly."""
    from src.autopilot.orchestrator import pause_project_workflows

    return pause_project_workflows(db, project_id, paused_by=paused_by, definition_ids=definition_ids)


def _check_budget_enforcement(db: Session, project: AutopilotProject) -> None:
    """Check if project has exceeded its budget limit and pause if needed.

    Args:
        db: Database session
        project: The project to check
    """
    if project.cost_limit_usd is None:
        return  # No limit set

    if project.cost_total_usd < project.cost_limit_usd:
        return  # Under budget

    # Over budget - pause active workflows
    logger.warning(f"[BUDGET] Project {project.id[:8]} over budget: ${project.cost_total_usd:.2f} >= ${project.cost_limit_usd:.2f}")
    _pause_project_workflows(db, project.id, paused_by="budget")


def check_budget_before_new_work(db: Session, project_id: str) -> bool:
    """Check if a project is over budget before starting new work.

    Call this before picking new designs or launching new features.

    Args:
        db: Database session
        project_id: Project ID to check

    Returns:
        True if under budget (safe to proceed), False if over budget
    """
    project = db.query(AutopilotProject).filter_by(id=project_id).first()
    if not project:
        return True  # No project found, allow (will fail elsewhere)

    if project.cost_limit_usd is None:
        return True  # No limit set

    if project.cost_total_usd < project.cost_limit_usd:
        return True  # Under budget

    logger.info(f"[BUDGET] Blocking new work for project {project_id[:8]}: ${project.cost_total_usd:.2f} >= ${project.cost_limit_usd:.2f}")
    return False
