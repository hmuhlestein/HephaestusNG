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

    # "All existing tasks are done" isn't the same as "the feature is done"
    # -- a workflow that got marked failed, or paused mid-pipeline (e.g. a
    # scope_review<->product_requirements goto loop that never reached
    # architecture_design), can still have every task it DID create sitting
    # at "done". Checking only task_statuses == {DONE} ignores that entirely
    # (observed live: a feature whose workflow paused after only 2 of 12
    # phases ran derived "completed", purely because those phases' tasks
    # happened to succeed -- which then made the feature pipeline treat it
    # as finished and never resume it). Mirrors derive_design_status's
    # existing has_failed_wf check one level up.
    from src.core.database import Workflow
    wf = db.query(Workflow).filter_by(id=feature.workflow_id).first()
    workflow_blocks_completion = bool(wf and wf.status in ("failed", "paused"))

    if task_statuses == {TaskStatus.DONE} and workflow_blocks_completion:
        # Keep active so retry/resume logic can pick it back up, instead of
        # the UI showing a falsely "done" feature.
        derived = FeatureStatus.ACTIVE
    elif task_statuses == {TaskStatus.DONE}:
        derived = FeatureStatus.COMPLETED
    elif wf and wf.status == "completed":
        # The workflow itself is the authoritative "did the whole 12-phase
        # pipeline actually finish" signal -- a phase can genuinely fail on
        # an early attempt and succeed on a later retry within that same
        # phase, leaving an old, superseded "failed" Task row behind
        # forever (nothing ever deletes it -- it's real history). Every
        # branch below this point treats task_statuses as if failed/
        # pending/in_progress entries always mean unfinished work, with no
        # way to distinguish "genuinely stuck" from "already fully done,
        # carrying old failure history." Observed live: a feature whose
        # workflow had long since reached "completed" (all 12 phases done,
        # merged to main) kept getting self-healed back to "active" on
        # every UI poll, purely because one early "development" attempt
        # had failed before a later retry succeeded.
        derived = FeatureStatus.COMPLETED
    elif TaskStatus.IN_PROGRESS in task_statuses or TaskStatus.ASSIGNED in task_statuses:
        derived = FeatureStatus.ACTIVE
    elif TaskStatus.FAILED in task_statuses:
        # Check if ALL tasks failed (vs mixed)
        if task_statuses == {TaskStatus.FAILED}:
            derived = FeatureStatus.FAILED
        elif workflow_blocks_completion:
            # Workflow is paused/failed, so the feature should be failed
            # even if some tasks are done
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

    # Respect pending status — it's an explicit orchestrator state
    # (waiting for first run or queued for retry). Don't override it
    # with derived status, or the retry logic in pick_next_design
    # will fight an infinite loop with status derivation.
    if design.status == FeatureStatus.PENDING:
        return FeatureStatus.PENDING

    # Get features for this design
    features = db.query(Feature).filter_by(design_id=design_id).all()

    if not features:
        # No features yet
        return design.status

    # Derive from feature statuses
    feature_status_map = {f: derive_feature_status(db, f.id, write_back=False) for f in features}
    feature_statuses = set(feature_status_map.values())

    # Consider skipped features as "done" for status derivation
    # (they were intentionally excluded, not left incomplete)
    non_skipped_statuses = feature_statuses - {FeatureStatus.SKIPPED}

    # Check if any workflow for this design has failed -- excludes
    # workflows with NO feature linking to them at all (orphaned, e.g. a
    # failed Feature Architect retry attempt superseded by a later
    # successful one). Unscoped, an orphan like that would keep a design
    # with every real feature actually completed stuck showing "active"
    # forever: pick_next_design clears an orphan's design_id once it
    # processes this design's active-designs loop, but this function is
    # called far more often (every status poll, from read-only callers
    # too) and has no such cleanup step of its own.
    #
    # Deliberately NOT also requiring the linked feature to be
    # "incomplete" (unlike pick_next_design's analogous failed_wf check):
    # this branch only matters when every feature has ALREADY derived to
    # "completed" (see the elif below), and derive_feature_status refuses
    # to call a feature "completed" while ITS OWN linked workflow is
    # "failed" (returns "active" instead, specifically to keep retry
    # logic engaged) -- so a failed workflow linked to a still-incomplete
    # feature could never coexist with non_skipped_statuses == COMPLETED
    # in the first place. Requiring that here would make this branch
    # unreachable for its actual purpose (e.g. a diagnostic task that
    # failed after its feature's own tasks otherwise finished).
    from src.core.database import Workflow
    has_failed_wf = (
        db.query(Workflow)
        .filter(
            Workflow.design_id == design_id,
            Workflow.status == "failed",
            db.query(Feature).filter(Feature.workflow_id == Workflow.id).exists(),
        )
        .first()
        is not None
    )

    if feature_statuses == {FeatureStatus.COMPLETED}:
        derived = FeatureStatus.COMPLETED
    elif feature_statuses == {FeatureStatus.VALIDATED}:
        # "validated" is a valid Feature status but not a valid
        # AutopilotDesign status (no DB CHECK constraint catches this, but
        # the frontend's StatusBadge has no case for it and silently
        # renders nothing) — every feature individually validated rolls up
        # to a completed design.
        derived = FeatureStatus.COMPLETED
    elif non_skipped_statuses == {FeatureStatus.COMPLETED} and has_failed_wf:
        # All non-skipped features are completed BUT a workflow failed
        # (e.g. diagnostic task failed). Keep design active so retry
        # logic in pick_next_design can handle it.
        derived = FeatureStatus.ACTIVE
        logger.info(
            f"Design {design_id[:8]}: features done but has failed workflow — "
            f"keeping active for retry"
        )
    elif non_skipped_statuses == {FeatureStatus.COMPLETED}:
        # All non-skipped features are completed, no failed workflows
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
        if derived == FeatureStatus.FAILED:
            # Surfaced on the design row in the UI -- without this, a design
            # that rolls up to "failed" purely because every task on one of
            # its features failed (no retryable workflow involved, so
            # pick_next_design's own retry-exhaustion message never fires)
            # shows a bare "failed" badge with no explanation.
            failed_names = [
                f.name for f, s in feature_status_map.items() if s == FeatureStatus.FAILED
            ]
            design.error = (
                f"Feature(s) failed: {', '.join(failed_names)}"
                if failed_names
                else "One or more features failed"
            )
        else:
            # Clear a stale message from a previous failure now that the
            # design has healed to a non-failed status.
            design.error = None
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
