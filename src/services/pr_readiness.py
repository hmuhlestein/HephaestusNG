"""Is this feature's PR actually ready for a human to review and merge?

The review gate used to open the moment every phase completed, regardless of
what the PR looked like. That is too early: `git_expert` opens the PR and
reports done while CI is still queued, so "all phases complete" says nothing
about whether the branch is mergeable. Observed live: a feature paused for
review with four failing checks (lint, two repo-local audit gates, an E2E
suite) and seven unread review comments, presented to the human as simply
"awaiting review".

Worse, the machinery to fix exactly that was already present and simply
never got to run. _resolve_pending_pr_status polls the PR every sweep tick,
and when CI is red it fails the git_expert task with the failing checks
named, which _retry_failed_tasks then re-dispatches git_expert to fix. That
loop only runs while the workflow is ACTIVE -- and pausing for review is
precisely what ends it. The gate short-circuited its own repair path.

So the gate now asks this module first. While the PR is fixable and the
retry budget holds, the workflow stays active and the existing poll/fix loop
keeps working. The human is asked only once there is something worth
looking at, or once the tool has genuinely run out of road -- in which case
they are told which checks are red rather than left waiting on a PR nobody
mentioned was broken.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewGateDecision:
    """Whether to open the human review gate, and what to tell them."""

    should_pause: bool
    status_reason: str
    # Why the PR is not mergeable, when it isn't. Carried so a caller that
    # decides to hold can hand the same text to reopen_git_expert_for_pr_fix
    # rather than recomputing it and risking the two disagreeing.
    blockers: str = ""
    # True when we are pausing despite the PR not being mergeable, because
    # the tool has exhausted its own attempts. Distinct from a clean pause
    # so the caller can log it differently and the UI can flag it.
    stuck: bool = False
    # True when the PR is already MERGED. There is nothing to review and
    # nothing to fix: the caller should finish the workflow, not pause it.
    # should_pause is left True alongside it on purpose -- a consumer that
    # never learned about this field falls back to the old behaviour
    # (asking a human), which is wrong but harmless, rather than to the
    # hold path, which would reopen git_expert against a merged PR.
    merged: bool = False


_CLEAN_REASON = "All phases complete -- awaiting human review and merge approval"


def evaluate_review_gate(session, workflow_id: str) -> ReviewGateDecision:
    """Decide whether the final human review gate should open now.

    Returns should_pause=False only when the PR is not mergeable AND the
    tool still has retries left to fix it -- the caller then leaves the
    workflow active so _resolve_pending_pr_status can keep polling and
    re-dispatching git_expert.

    Every failure path errs toward pausing. A gate that fails open leaves a
    human waiting on a PR nobody flagged; a gate that fails closed just asks
    for a look sooner than strictly necessary. Only the second is safe.
    """
    from src.core.database import Feature

    feature = (
        session.query(Feature)
        .filter_by(workflow_id=workflow_id)
        .filter(Feature.pr_url.isnot(None))
        .first()
    )
    if not feature or not feature.pr_url:
        # No PR to judge (review_mode off for git_expert, a design-only
        # workflow, or git_expert never got that far). Nothing to wait for.
        return ReviewGateDecision(True, _CLEAN_REASON)

    try:
        from src.services.github_pr_status import get_pr_status

        status = get_pr_status(feature.pr_url)
    except Exception as e:
        logger.warning(f"[REVIEW-GATE] PR status lookup raised for {feature.pr_url}: {e}")
        return ReviewGateDecision(True, _CLEAN_REASON)

    if status is None:
        # gh unavailable / no PR for this ref. Same contract as everywhere
        # else in this codebase: treat as unknown, never as a rejection.
        logger.warning(f"[REVIEW-GATE] PR status unknown for {feature.pr_url} -- opening the gate")
        return ReviewGateDecision(True, _CLEAN_REASON)

    if status.state == "MERGED":
        # Observed live: a PR merged by a human while the tool was stopped.
        # GitHub reports mergeStateStatus UNKNOWN for a merged PR, so
        # ready_to_merge is False and needs_work is False -- which the
        # arms below read as "stuck" and answered with "awaiting human
        # review -- PR is not mergeable yet" about a PR already on main.
        return ReviewGateDecision(True, "PR merged -- finishing the workflow", merged=True)

    if status.state == "CLOSED":
        # Closed without merging. No commit fixes that; a human decided.
        return ReviewGateDecision(
            True, "Awaiting human review -- the PR was closed without being merged", stuck=True
        )

    if status.ready_to_merge:
        return ReviewGateDecision(True, _CLEAN_REASON)

    # Not mergeable. Is this something the tool can still act on itself?
    blockers = _describe_blockers(status)

    if not status.needs_work:
        # Not ready, but not something another commit fixes either --
        # CI still running, or BLOCKED on a rule no agent can satisfy
        # (missing approvals, a required check that never reports). Waiting
        # longer will not help; hand it to the human with the reason.
        return ReviewGateDecision(
            True, f"Awaiting human review -- PR is not mergeable yet: {blockers}", stuck=True
        )

    retries_left = _git_expert_retries_left(session, workflow_id)
    if retries_left > 0:
        logger.info(
            f"[REVIEW-GATE] Holding the review gate for {feature.pr_url}: {blockers} "
            f"({retries_left} fix attempt(s) left) -- workflow stays active so the "
            "PR-status sweep can keep fixing it"
        )
        return ReviewGateDecision(False, "", blockers=blockers)

    return ReviewGateDecision(
        True,
        f"Awaiting human review -- the tool could not get this PR mergeable: {blockers}",
        stuck=True,
    )


def _describe_blockers(status) -> str:
    """Human-readable summary of why a PR is not mergeable."""
    parts = []
    if status.failing_checks:
        parts.append("failing checks: " + ", ".join(status.failing_checks))
    if status.review_decision == "CHANGES_REQUESTED":
        parts.append("changes requested by a reviewer")
    if status.ci_conclusion == "pending":
        parts.append("CI still running")
    merge_state = getattr(status, "merge_state", None)
    if merge_state and merge_state != "CLEAN":
        parts.append(f"merge state {merge_state}")
    return "; ".join(parts) or "not mergeable"


def _git_expert_retries_left(session, workflow_id: str) -> int:
    """How many more times the tool may re-dispatch git_expert for this
    workflow before the retry machinery gives up on its own.

    Read from the same source _retry_failed_tasks enforces, so the gate
    cannot promise attempts the retry path will refuse to make -- that
    mismatch is what would produce a workflow left active forever with
    nothing acting on it.
    """
    from src.core.database import Phase, Task

    try:
        from src.autopilot.spec import get_max_task_retries

        cap = get_max_task_retries(workflow_id) or 5
    except Exception:
        cap = 5

    phase = (
        session.query(Phase)
        .filter_by(workflow_id=workflow_id, name="git_expert")
        .first()
    )
    if not phase:
        return 0

    # Summed across EVERY git_expert task for this phase, not just the
    # newest. Each reopen/retry cycle can create a fresh Task row starting
    # at retry_count=0, so reading only the latest made the budget reset
    # every round and the gate hold forever -- the unbounded-spend outcome
    # this cap exists to prevent. Observed live: three tasks at 5, 1 and 0
    # retries, with the gate reporting a full budget off the last one.
    #
    # Each task is itself one attempt, so a task that has never been
    # retried still counts as one. Monotonic by construction: creating
    # another task can only increase the total, never restore headroom.
    tasks = session.query(Task).filter(Task.phase_id == phase.id).all()
    if not tasks:
        return 0
    attempts = sum(1 + (t.retry_count or 0) for t in tasks)
    return max(0, cap - attempts)


def reopen_git_expert_for_pr_fix(session, workflow_id: str, blockers: str) -> bool:
    """Put the git_expert task back into a state a repair path will act on.

    Holding the review gate is necessary but not sufficient. Both repair
    paths match on specific states and a completed git_expert matches
    neither:

        _resolve_pending_pr_status : status == in_progress AND
                                     assigned_agent_id IS NULL
        _retry_failed_tasks        : status == failed
        a finished git_expert      : status == done, agent set,
                                     PhaseExecution completed

    So a PR that goes red AFTER the task legitimately completed is
    unreachable: the gate refuses to ask a human (correctly), and nothing
    fixes it either. Observed live -- PR #1171 sat with four failing checks
    and the workflow held open with no agent ever dispatched.

    Marking it failed, with the blockers as failure_reason, is what
    _retry_failed_tasks already consumes -- and with the RETRY banner it now
    passes, the re-dispatched agent is told exactly which checks are red.

    Deliberately NOT done by widening either sweep's query to include
    "done": that status is terminal on purpose. Phase-completion counts,
    dependent promotion and _clean_stale_assigned_tasks all key off it, so
    a background sweep that reopens done work risks double-dispatch well
    beyond this phase. Doing it here keeps the one re-opening in the one
    place that has already decided the PR is not acceptable.

    Returns True if it reopened something. Callers must only invoke this
    when evaluate_review_gate said to hold, so the retry budget -- which
    that function already checked -- still bounds the loop.
    """
    from src.core.database import Phase, PhaseExecution, Task

    phase = (
        session.query(Phase)
        .filter_by(workflow_id=workflow_id, name="git_expert")
        .first()
    )
    if not phase:
        return False

    task = (
        session.query(Task)
        .filter(Task.phase_id == phase.id)
        .order_by(Task.created_at.desc())
        .first()
    )
    if not task or task.status != "done":
        # Already actionable (failed), already parked awaiting CI
        # (in_progress with no agent), or mid-flight with a live agent.
        # Every one of those is a state a repair path already handles, and
        # stealing a task from a working agent is how you get two agents on
        # one branch.
        return False

    task.status = "failed"
    task.failure_reason = (
        f"PR is not mergeable: {blockers} -- push additional commits to this "
        "SAME branch/PR to address it (do not open a new PR)."
    )
    task.assigned_agent_id = None
    task.completed_at = None

    execution = session.query(PhaseExecution).filter_by(phase_id=phase.id).first()
    if execution and execution.status == "completed":
        # The phase has to reopen too, or _advance_phases marches past it
        # by order and the reopened task is never dispatched.
        try:
            from src.autopilot.orchestrator.phase_transitions import (
                transition_phase_execution,
            )

            transition_phase_execution(
                session, phase.id, "in_progress",
                reason="pr_not_mergeable_reopen",
                extra_fields={"completed_at": execution.completed_at},
            )
        except Exception as e:
            logger.warning(f"[REVIEW-GATE] Could not reopen git_expert's execution: {e}")
            execution.status = "in_progress"

    session.commit()
    logger.warning(
        f"[REVIEW-GATE] Reopened git_expert for {workflow_id[:8]} to fix its PR: {blockers}"
    )
    return True


def finish_merged_workflow(session, workflow_id: str) -> bool:
    """Record a PR that is already merged as the workflow's finished state.

    Writes exactly what review_feature's approve path writes after a
    successful `gh pr merge` -- workflow and feature both "completed" -- and
    only when derive_workflow_status agrees every phase is done, so this
    cannot mark a half-finished workflow complete just because its PR is.

    Returns True if it finished the workflow. False means some phase is
    still open; the workflow is left active and the pipeline keeps going.
    """
    from src.core.database import Feature, Workflow
    from src.core.status_derivation import derive_workflow_status

    derived = derive_workflow_status(session, workflow_id, write_back=False)
    if derived != "completed":
        logger.info(
            f"[REVIEW-GATE] PR for {workflow_id[:8]} is merged but the workflow derives "
            f"'{derived}' -- leaving it active"
        )
        return False

    wf = session.query(Workflow).filter_by(id=workflow_id).first()
    feature = session.query(Feature).filter_by(workflow_id=workflow_id).first()
    if wf:
        wf.status = "completed"
        wf.status_reason = None
    if feature:
        feature.status = "completed"
    session.commit()
    logger.info(f"[REVIEW-GATE] PR for {workflow_id[:8]} already merged -- workflow completed")
    return True
