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
    Phase,
    PhaseExecution,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
)

logger = logging.getLogger(__name__)


def _apply_derived_status(
    db: Session, entity, derived: str, entity_label: str, entity_id: str,
    write_back: bool, on_change=None,
) -> None:
    """Shared self-heal write-back for derive_feature_status/derive_design_
    status/derive_workflow_status: log + write entity.status + commit when
    write_back is True and derived disagrees with the current DB value.
    on_change(entity, derived), if given, runs before commit (e.g.
    derive_design_status's extra design.error set/clear)."""
    if write_back and derived != entity.status:
        logger.info(
            f"[STATUS-HEAL] {entity_label} {entity_id[:8]} status: "
            f"{entity.status} -> {derived}"
        )
        entity.status = derived
        if on_change:
            on_change(entity, derived)
        db.commit()


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

    # Respect paused status - user explicitly paused -- but only while the
    # underlying workflow is still actually paused. A workflow can resume
    # through paths that never call resume_feature (the self-heal auto-
    # resume sweep, a direct DB/admin resume): trusting this cached flag
    # unconditionally left the feature reporting "paused" forever even
    # after its workflow was back to dispatching real tasks. Mirrors the
    # same live-workflow guard derive_design_status already applies to its
    # own cached "paused" flag, just inverted (design-level: stay paused
    # only if a workflow is STILL active-or-paused; feature-level: stay
    # paused only if ITS workflow is still paused).
    if feature.status == FeatureStatus.PAUSED:
        # Local import, not the module-level one: derive_feature_status
        # also re-imports Workflow further down (see below), which makes
        # every reference to the bare name local to the whole function
        # body in Python's scoping -- referencing the module-level import
        # here would raise UnboundLocalError before that later import runs.
        from src.core.database import Workflow as _Workflow

        wf_still_paused = (
            feature.workflow_id
            and db.query(_Workflow.status).filter_by(id=feature.workflow_id).scalar() == "paused"
        )
        if wf_still_paused:
            return FeatureStatus.PAUSED
        # else: workflow moved on without going through resume_feature --
        # fall through to real derivation below instead of trusting the
        # stale cached value.

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

    # Derive from task statuses. DUPLICATED tasks are excluded entirely --
    # every branch below is an exact-match or membership check against a
    # specific set of "real work remains" statuses (DONE/IN_PROGRESS/
    # ASSIGNED/FAILED/PENDING), and DUPLICATED was never one of them
    # despite being a real terminal status (TaskStatus.TERMINAL includes
    # it). Left in, a single duplicated task permanently breaks every
    # `task_statuses == {DONE}` exact-set check for that feature (the set
    # becomes {DONE, DUPLICATED}, matching nothing below), so a feature
    # with real, fully-resolved work sits stuck at whatever it derived to
    # before the duplicate was marked -- invisible in every branch, not
    # done, not failed, not active for a real reason. Observed live: three
    # debris tasks resolved as "duplicated" (superseded by sibling tasks
    # that had already done the real work) kept their feature showing
    # "active" indefinitely.
    task_statuses = {t.status for t in tasks} - {TaskStatus.DUPLICATED}
    if not task_statuses:
        # Every task was a duplicate -- nothing left to derive from,
        # preserve whatever the DB already has rather than claim a status
        # (e.g. COMPLETED) that no task's real work ever earned.
        return feature.status

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
        # If workflow is failed, the feature should be failed too
        # If workflow is paused mid-pipeline (real work still to resume),
        # keep active so retry/resume logic can pick it back up. But
        # paused_by="review" is a different shape of "paused" entirely --
        # it's only ever set once EVERY phase has already reached
        # "completed" (_pause_feature_for_review), so there is no
        # "remaining work" for retry/resume logic to pick up; deriving
        # ACTIVE here is what a human review is waiting on, not what's
        # actually true. The frontend's Review button gates on
        # feature.status in {"completed", "paused"} (DesignQueuePanel.tsx),
        # so deriving ACTIVE instead of PAUSED silently hid the button even
        # though review_pending (read straight from workflow.paused_by)
        # correctly reported the review as pending. Observed live: feature
        # feat-e1d649cf's Review button never appeared despite the
        # workflow sitting paused_by="review" for hours.
        if wf and wf.status == "failed":
            derived = FeatureStatus.FAILED
        elif wf and wf.paused_by == "review":
            derived = FeatureStatus.PAUSED
        else:
            derived = FeatureStatus.ACTIVE
    elif task_statuses == {TaskStatus.DONE}:
        # All existing tasks are done, but that doesn't mean the feature
        # is done — the workflow may only have completed a few of its
        # phases. Check that every PhaseExecution is actually "completed"
        # (or legitimately "skipped" -- e.g. architectural_review/
        # adversarial_review/security_review can be conditionally skipped;
        # the workflow-level status derivation below already treats
        # "skipped" as terminal too, see its own PhaseExecution.status.notin_
        # check -- excluding it here disagreed with that and caused this
        # feature to flip back to "active" on every self-heal poll right
        # after review_feature's approve handler had just set it
        # "completed", flapping forever and never settling on Done)
        # before declaring the feature done. Observed live: tech-debt
        # feature with 13 phases, only 2 had tasks (all done), derived
        # "completed" while stuck at scope_review.
        from src.core.database import Phase as _Ph
        from src.core.database import PhaseExecution as _PE
        incomplete_phases = (
            db.query(_PE)
            .join(_Ph, _PE.phase_id == _Ph.id)
            .filter(
                _Ph.workflow_id == feature.workflow_id,
                _PE.status.notin_(["completed", "skipped"]),
            )
            .count()
        )
        if incomplete_phases > 0:
            derived = FeatureStatus.ACTIVE
        else:
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
    _apply_derived_status(db, feature, derived, "Feature", feature_id, write_back)

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
    def _set_design_error(d, d_derived):
        if d_derived == FeatureStatus.FAILED:
            # Surfaced on the design row in the UI -- without this, a design
            # that rolls up to "failed" purely because every task on one of
            # its features failed (no retryable workflow involved, so
            # pick_next_design's own retry-exhaustion message never fires)
            # shows a bare "failed" badge with no explanation.
            failed_names = [
                f.name for f, s in feature_status_map.items() if s == FeatureStatus.FAILED
            ]
            d.error = (
                f"Feature(s) failed: {', '.join(failed_names)}"
                if failed_names
                else "One or more features failed"
            )
        else:
            # Clear a stale message from a previous failure now that the
            # design has healed to a non-failed status.
            d.error = None

    _apply_derived_status(
        db, design, derived, "Design", design_id, write_back, on_change=_set_design_error
    )

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

    # PhaseExecution completeness is the authoritative "did the whole
    # pipeline actually finish" signal for a phase-tracked workflow (the
    # normal autopilot/feature_architect case) -- checked FIRST, before any
    # task-status heuristic, for two reasons a plain task_statuses set
    # can't distinguish on its own:
    #   1. "every task that exists is done" isn't "every phase ran" -- a
    #      phase that hasn't been dispatched yet has ZERO tasks, invisible
    #      to task_statuses entirely. Observed live: a workflow with
    #      product_validation's task "done" but doc_review/forensics_
    #      analysis/git_expert/deploy all still "pending" (zero tasks
    #      ever created) derived "completed" purely because the one task
    #      that existed happened to be done.
    #   2. An old, superseded "failed" Task row from an early attempt that
    #      a later retry fixed doesn't mean the workflow isn't done --
    #      mirrors derive_feature_status's identical, already-proven
    #      protection for the exact same class of stale-history artifact
    #      (see its own "elif wf and wf.status == 'completed'" branch).
    #      Without this, a single harmless leftover "failed" task anywhere
    #      in a long goto/retry history permanently blocks a genuinely
    #      finished workflow from ever deriving "completed" again.
    # Only applies when this workflow actually has Phase rows tracked --
    # a plain task-only workflow (no phase structure at all) falls through
    # to the task-status heuristics below unchanged.
    has_phases = db.query(Phase).filter_by(workflow_id=workflow_id).first() is not None
    if has_phases:
        # Respect a deliberate workflow-level "failed" with no task-level
        # trace -- checked BEFORE phase-completeness, not after. Those
        # exist to rescue a workflow from stale TASK-level failure history
        # (a single old, superseded "failed" task among otherwise-
        # successful ones) -- a real problem, but a different one from a
        # deliberate, workflow-level "failed" decision that has NO
        # corresponding task-level trace at all, like _trigger_arbitration's
        # exhausted-retries cap or an abandoned review-pause mark. Those set
        # wf.status="failed" without ever creating a new task, so every
        # EXISTING task/phase can still legitimately look "done" --
        # checking phase-completeness FIRST would derive "completed" and
        # silently resurrect an intentionally-terminated workflow. This
        # guard used to sit after the completeness check, inside the
        # incomplete-phase branch below -- unreachable in exactly the case
        # that matters, since a workflow failed with no incomplete phase
        # never reached it. Observed live: the design-status poll's
        # write_back=True call flipped a review-gate workflow from
        # "failed" to "completed" every ~10s, resurrecting it behind the
        # user's back and making the Rerun/Recover button (which only
        # matches status in {active, paused, failed}) silently no-op.
        if workflow.status == WorkflowStatus.FAILED:
            return WorkflowStatus.FAILED

        incomplete_phase = (
            db.query(PhaseExecution)
            .join(Phase, PhaseExecution.phase_id == Phase.id)
            .filter(
                Phase.workflow_id == workflow_id,
                PhaseExecution.status.notin_(["completed", "skipped"]),
            )
            .first()
        )
        if not incomplete_phase:
            derived = WorkflowStatus.COMPLETED
            _apply_derived_status(db, workflow, derived, "Workflow", workflow_id, write_back)
            return derived

    # Derive from task statuses. Reaching this point with has_phases True
    # means the block above already found a genuinely incomplete phase --
    # task_statuses == {DONE} here must NOT translate to "completed" (that's
    # exactly the vacuous-truth case #1 above exists to rule out), only to
    # "active" (real, tracked work remains; the tasks that do exist just
    # haven't hit it yet).
    #
    # DUPLICATED excluded for the same reason as derive_feature_status: a
    # single duplicated task (superseded by a sibling that did the real
    # work) otherwise permanently breaks every exact-set check below. An
    # all-duplicated task_statuses falls through to the final `else`
    # fallback (existing DB status), same as today's untouched behavior
    # for any other unrecognized combination.
    task_statuses = {t.status for t in tasks} - {TaskStatus.DUPLICATED}

    if task_statuses == {TaskStatus.DONE}:
        derived = WorkflowStatus.ACTIVE if has_phases else WorkflowStatus.COMPLETED
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
    _apply_derived_status(db, workflow, derived, "Workflow", workflow_id, write_back)

    return derived
