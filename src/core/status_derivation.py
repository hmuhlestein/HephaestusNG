"""Centralized status derivation for Hephaestus entities.

This module provides the single source of truth for deriving entity statuses
from their child entities, addressing the H-3 finding in ARCHITECTURE_REVIEW.md.

Usage:
    from src.core.status_derivation import derive_feature_status, derive_design_status
    
    # Derive feature status from tasks
    feature_status = derive_feature_status(db, feature_id)
    
    # Derive design status from features
    design_status = derive_design_status(db, design_id)
"""

import logging

from sqlalchemy.orm import Session

from src.core.constants import DIAGNOSTIC_TASK_PREFIX
from src.core.database import (
    AutopilotDesign,
    Feature,
    FeatureStatus,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
)

logger = logging.getLogger(__name__)


def derive_feature_status(db: Session, feature_id: str, write_back: bool = True) -> str:
    """Derive feature status from its tasks.
    
    This is the SINGLE source of truth for feature status. All code paths
    that need to know a feature's status should call this function instead
    of reading Feature.status directly.
    
    Args:
        db: Database session
        feature_id: Feature ID to derive status for
        write_back: If True, update Feature.status in DB when it disagrees
        
    Returns:
        Derived status string: "pending", "active", "completed", "failed", "paused", "skipped"
    """
    feature = db.query(Feature).filter_by(id=feature_id).first()
    if not feature:
        logger.warning(f"Feature {feature_id} not found")
        return "unknown"
    
    # Respect paused status - user explicitly paused
    if feature.status == FeatureStatus.PAUSED:
        return FeatureStatus.PAUSED
    
    # Respect skipped status
    if feature.status == FeatureStatus.SKIPPED:
        return FeatureStatus.SKIPPED

    # A feature with no workflow yet has, by definition, no tasks of its own
    # -- must return early instead of falling through to the query below.
    # Task.workflow_id == feature.workflow_id becomes Task.workflow_id IS
    # NULL when feature.workflow_id is None, which matches every OTHER
    # task in the system with a null workflow_id (e.g. stray SDK/API test
    # tasks created without one) rather than "no tasks for this feature".
    # Observed live: 4 leftover test tasks with workflow_id IS NULL and
    # status="failed" made every not-yet-started feature (workflow_id still
    # None) derive -- and self-heal write back -- status "failed" before
    # any of them had ever actually run.
    if not feature.workflow_id:
        return feature.status

    # Get all non-diagnostic tasks for this feature's workflow
    tasks = (
        db.query(Task)
        .filter(
            Task.workflow_id == feature.workflow_id,
            ~Task.raw_description.like(f"{DIAGNOSTIC_TASK_PREFIX}%")
        )
        .all()
    )
    
    if not tasks:
        # No tasks yet - use DB status (set by orchestrator before tasks exist)
        return feature.status
    
    # Derive from task statuses
    task_statuses = {t.status for t in tasks}
    
    if task_statuses == {TaskStatus.DONE}:
        derived = FeatureStatus.COMPLETED
    elif TaskStatus.IN_PROGRESS in task_statuses or TaskStatus.ASSIGNED in task_statuses:
        derived = FeatureStatus.ACTIVE
    elif TaskStatus.FAILED in task_statuses:
        # Check if ALL tasks failed (vs mixed)
        if task_statuses == {TaskStatus.FAILED}:
            derived = FeatureStatus.FAILED
        else:
            # Mix of failed and other statuses
            derived = FeatureStatus.ACTIVE  # Still active - has work to do
    elif task_statuses == {TaskStatus.PENDING}:
        # No task started yet
        derived = FeatureStatus.PENDING
    elif TaskStatus.DONE in task_statuses:
        # Some done, some pending - still active
        derived = FeatureStatus.ACTIVE
    else:
        # Fallback to DB status
        derived = feature.status
    
    # Self-heal: write back to DB if status disagrees
    if write_back and derived != feature.status:
        logger.info(
            f"[STATUS-HEAL] Feature {feature_id[:8]} status: "
            f"{feature.status} -> {derived}"
        )
        feature.status = derived
        db.commit()
    
    return derived


def derive_design_status(db: Session, design_id: str, write_back: bool = True) -> str:
    """Derive design status from its features.
    
    This is the SINGLE source of truth for design status.
    
    Args:
        db: Database session
        design_id: Design ID to derive status for
        write_back: If True, update AutopilotDesign.status in DB when it disagrees
        
    Returns:
        Derived status string
    """
    design = db.query(AutopilotDesign).filter_by(id=design_id).first()
    if not design:
        logger.warning(f"Design {design_id} not found")
        return "unknown"
    
    # Respect paused status only if there are active workflows
    # (otherwise a stale "paused" status blocks reruns from taking effect)
    if design.status == WorkflowStatus.PAUSED:
        from src.core.database import Workflow
        has_active_wfs = db.query(Workflow).filter(
            Workflow.design_id == design_id,
            Workflow.status.in_(["active", "paused"]),
        ).first()
        if has_active_wfs:
            return WorkflowStatus.PAUSED
    
    # Get features for this design
    features = db.query(Feature).filter_by(design_id=design_id).all()
    
    if not features:
        # No features yet
        return design.status
    
    # Derive from feature statuses
    feature_statuses = {derive_feature_status(db, f.id, write_back=False) for f in features}
    
    if feature_statuses == {FeatureStatus.COMPLETED}:
        derived = FeatureStatus.COMPLETED
    elif feature_statuses == {FeatureStatus.VALIDATED}:
        # "validated" is a valid Feature status but not a valid
        # AutopilotDesign status (no DB CHECK constraint catches this, but
        # the frontend's StatusBadge has no case for it and silently
        # renders nothing) — every feature individually validated rolls up
        # to a completed design.
        derived = FeatureStatus.COMPLETED
    elif FeatureStatus.ACTIVE in feature_statuses:
        derived = FeatureStatus.ACTIVE
    elif FeatureStatus.FAILED in feature_statuses:
        derived = FeatureStatus.FAILED
    elif feature_statuses == {FeatureStatus.PENDING}:
        derived = FeatureStatus.PENDING
    elif FeatureStatus.COMPLETED in feature_statuses:
        # Some completed, some pending - still active
        derived = FeatureStatus.ACTIVE
    else:
        derived = design.status
    
    # Self-heal: write back to DB if status disagrees
    if write_back and derived != design.status:
        logger.info(
            f"[STATUS-HEAL] Design {design_id[:8]} status: "
            f"{design.status} -> {derived}"
        )
        design.status = derived
        db.commit()
    
    return derived


def derive_workflow_status(db: Session, workflow_id: str, write_back: bool = True) -> str:
    """Derive workflow status from its tasks.
    
    Args:
        db: Database session
        workflow_id: Workflow ID to derive status for
        write_back: If True, update Workflow.status in DB when it disagrees
        
    Returns:
        Derived status string
    """
    workflow = db.query(Workflow).filter_by(id=workflow_id).first()
    if not workflow:
        logger.warning(f"Workflow {workflow_id} not found")
        return "unknown"
    
    # Respect paused status
    if workflow.status == WorkflowStatus.PAUSED:
        return WorkflowStatus.PAUSED
    
    # Get tasks for this workflow
    tasks = db.query(Task).filter_by(workflow_id=workflow_id).all()
    
    if not tasks:
        return workflow.status
    
    # Derive from task statuses
    task_statuses = {t.status for t in tasks}
    
    if task_statuses == {TaskStatus.DONE}:
        derived = WorkflowStatus.COMPLETED
    elif TaskStatus.IN_PROGRESS in task_statuses or TaskStatus.ASSIGNED in task_statuses:
        derived = WorkflowStatus.ACTIVE
    elif TaskStatus.FAILED in task_statuses:
        if task_statuses == {TaskStatus.FAILED}:
            derived = WorkflowStatus.FAILED
        else:
            derived = WorkflowStatus.ACTIVE
    elif task_statuses == {TaskStatus.PENDING}:
        # "pending" isn't a valid Workflow.status (CHECK constraint only
        # allows active/paused/completed/failed) — no tasks have started
        # yet, so trust whatever the DB already has rather than deriving
        # an invalid value that would fail on write-back.
        derived = workflow.status
    elif TaskStatus.DONE in task_statuses:
        derived = WorkflowStatus.ACTIVE
    else:
        derived = workflow.status
    
    # Self-heal
    if write_back and derived != workflow.status:
        logger.info(
            f"[STATUS-HEAL] Workflow {workflow_id[:8]} status: "
            f"{workflow.status} -> {derived}"
        )
        workflow.status = derived
        db.commit()
    
    return derived
