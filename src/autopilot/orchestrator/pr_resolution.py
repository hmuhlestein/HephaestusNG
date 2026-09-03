"""§3.3: resolve a git_expert task left `in_progress` awaiting an open
PR's CI/review outcome (verify_git_expert_merged_and_pushed's "pending"
branch, src/services/task_completion/verification.py).

Mirrors arbitration.py's _maybe_resolve_arbitration shape deliberately:
a task sits done-but-unresolved pending an external check, resolved later
by a periodic sweep tick rather than by spinning up a fresh agent every
tick just to ask "are we done yet". Called every sweep tick alongside
_maybe_resolve_arbitration -- see _run_phase_advancement_sweep_once.
"""

import logging
from typing import TYPE_CHECKING

from src.core.database import Feature, Phase, PhaseExecution, Task, get_db, utc_now

if TYPE_CHECKING:
    from src.autopilot.orchestrator import OrchestratorLogger

logger = logging.getLogger(__name__)


def _resolve_pending_pr_status(workflow_id: str, sweep_logger: "OrchestratorLogger") -> None:
    """Find this workflow's git_expert task (if any) left in_progress
    awaiting PR resolution, and either leave it alone (still pending),
    mark it done (CI passing, no unresolved review -- the next sweep
    tick's normal _case_in_progress_complete path advances the phase
    exactly as if the agent itself had just completed it), or mark it
    failed with a real, actionable reason (CI failing / changes
    requested -- picked up by the normal retry/arbitration machinery like
    any other failure).

    Identified structurally, no new column needed: an in_progress
    git_expert task whose Feature.pr_url is already set can only be in
    this state via verify_git_expert_merged_and_pushed's pending branch --
    every other git_expert task either never reached "done" at all (no PR
    yet) or was already resolved to done/failed by that same floor.
    """
    with get_db() as db:
        phase = db.query(Phase).filter_by(workflow_id=workflow_id, name="git_expert").first()
        if not phase:
            return
        execution = db.query(PhaseExecution).filter_by(phase_id=phase.id).first()
        if not execution or execution.status != "in_progress":
            return

        task = (
            db.query(Task)
            .filter_by(phase_id=phase.id, status="in_progress")
            .order_by(Task.created_at.desc())
            .first()
        )
        if not task:
            return

        feature = db.query(Feature).filter_by(workflow_id=workflow_id).first()
        if not feature or not feature.pr_url:
            return  # Not yet at the "PR exists, awaiting resolution" point.

        pr_url = feature.pr_url
        task_id = task.id
        phase_name = phase.name

    from src.services.github_pr_status import get_pr_status

    pr_status = get_pr_status(pr_url)
    if pr_status is None or pr_status.is_pending:
        return  # gh unavailable, or genuinely still pending -- check again next tick.

    with get_db() as db:
        task = db.query(Task).filter_by(id=task_id).first()
        if not task or task.status != "in_progress":
            return  # Resolved by a concurrent tick, or the agent came back and finished it itself.

        if pr_status.needs_work:
            sweep_logger.warning(f"[PR-STATUS] {phase_name}: {pr_status.summary} -- failing task {task_id[:8]} for retry")
            task.status = "failed"
            task.failure_reason = (
                f"{pr_status.summary} -- push additional commits to this SAME branch/PR to "
                "address it (do not open a new PR)."
            )
        else:
            sweep_logger.info(f"[PR-STATUS] {phase_name}: {pr_status.summary} -- marking task {task_id[:8]} done")
            task.status = "done"
            task.completed_at = utc_now()
            task.failure_reason = None
            task.completion_notes = (task.completion_notes or "") + f"\n\n[Resolved by orchestrator PR-status check: {pr_status.summary}]"
        db.commit()
