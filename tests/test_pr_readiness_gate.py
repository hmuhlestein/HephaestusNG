"""The human review gate must not open on a PR that is not mergeable.

It used to open the moment every phase completed. git_expert opens the PR
and reports done while CI is still queued, so "all phases complete" says
nothing about the branch. Observed live: a feature paused for review with
four failing checks and seven unread review comments, described to the
human only as "awaiting human review and merge approval".

The repair path was already present and the pause is what killed it:
_resolve_pending_pr_status polls the PR every sweep tick and fails the
git_expert task with the failing checks named, which _retry_failed_tasks
re-dispatches git_expert to fix -- but only while the workflow is ACTIVE.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.services.github_pr_status import PRStatus
from src.services.pr_readiness import finish_merged_workflow, evaluate_review_gate


def _status(**kw):
    base = dict(
        url="https://github.com/o/r/pull/1", state="OPEN", ci_conclusion="passing",
        review_decision=None, failing_checks=[], summary="", merge_state="CLEAN",
    )
    base.update(kw)
    return PRStatus(**base)


def _session(pr_url="https://github.com/o/r/pull/1", retry_count=0):
    """A session whose Feature/Phase/Task lookups return usable rows."""
    feature = MagicMock(); feature.pr_url = pr_url
    phase = MagicMock(); phase.id = "phase-1"
    counts = retry_count if isinstance(retry_count, (list, tuple)) else [retry_count]
    tasks = []
    for n in counts:
        t = MagicMock(); t.retry_count = n
        tasks.append(t)
    task = tasks[-1]

    wf = MagicMock(); wf.status = "active"; wf.status_reason = "x"
    session = MagicMock()
    session._feature, session._wf = feature, wf  # handles for assertions

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", "")
        if name == "Feature":
            q.filter_by.return_value.filter.return_value.first.return_value = (
                feature if pr_url else None
            )
            q.filter_by.return_value.first.return_value = feature if pr_url else None
        elif name == "Workflow":
            q.filter_by.return_value.first.return_value = wf
        elif name == "Phase":
            q.filter_by.return_value.first.return_value = phase
        else:
            q.filter.return_value.order_by.return_value.first.return_value = task
            q.filter.return_value.all.return_value = tasks
        return q

    session.query.side_effect = _query
    return session


class TestGateHoldsWhileTheToolCanStillFixIt:
    def test_red_ci_with_retries_left_keeps_the_workflow_active(self):
        """The whole point: stay active so the poll/fix loop can run."""
        st = _status(ci_conclusion="failing", failing_checks=["Backend: Lint & Format"])
        with patch("src.services.github_pr_status.get_pr_status", return_value=st), \
             patch("src.autopilot.spec.get_max_task_retries", return_value=5):
            d = evaluate_review_gate(_session(retry_count=0), "wf-1")
        assert d.should_pause is False

    def test_changes_requested_with_retries_left_also_holds(self):
        st = _status(review_decision="CHANGES_REQUESTED", merge_state="CLEAN")
        with patch("src.services.github_pr_status.get_pr_status", return_value=st), \
             patch("src.autopilot.spec.get_max_task_retries", return_value=5):
            d = evaluate_review_gate(_session(retry_count=1), "wf-1")
        assert d.should_pause is False


class TestGateOpensWhenItShould:
    def test_a_mergeable_pr_opens_the_gate_cleanly(self):
        with patch("src.services.github_pr_status.get_pr_status", return_value=_status()):
            d = evaluate_review_gate(_session(), "wf-1")
        assert d.should_pause is True
        assert d.stuck is False

    def test_retries_exhausted_opens_the_gate_and_names_the_blockers(self):
        """Never leave a human waiting on a PR nobody said was broken."""
        st = _status(ci_conclusion="failing",
                     failing_checks=["Backend: Lint & Format", "gherkin-audit: Reproducibility Gate"])
        with patch("src.services.github_pr_status.get_pr_status", return_value=st), \
             patch("src.autopilot.spec.get_max_task_retries", return_value=5):
            d = evaluate_review_gate(_session(retry_count=5), "wf-1")
        assert d.should_pause is True
        assert d.stuck is True
        assert "Backend: Lint & Format" in d.status_reason
        assert "gherkin-audit: Reproducibility Gate" in d.status_reason

    def test_a_blocker_no_agent_can_fix_opens_the_gate_immediately(self):
        """BLOCKED means missing approvals or a never-reporting check.
        Another commit will not help, so do not burn retries on it."""
        st = _status(merge_state="BLOCKED")
        with patch("src.services.github_pr_status.get_pr_status", return_value=st), \
             patch("src.autopilot.spec.get_max_task_retries", return_value=5):
            d = evaluate_review_gate(_session(retry_count=0), "wf-1")
        assert d.should_pause is True
        assert d.stuck is True

    def test_no_pr_opens_the_gate(self):
        with patch("src.services.github_pr_status.get_pr_status", return_value=_status()):
            d = evaluate_review_gate(_session(pr_url=None), "wf-1")
        assert d.should_pause is True


class TestGateFailsClosed:
    """A gate that fails open strands a human on a PR nobody flagged; one
    that fails closed just asks for a look sooner than needed."""

    def test_unknown_pr_status_opens_the_gate(self):
        with patch("src.services.github_pr_status.get_pr_status", return_value=None):
            d = evaluate_review_gate(_session(), "wf-1")
        assert d.should_pause is True

    def test_a_raising_lookup_opens_the_gate(self):
        with patch("src.services.github_pr_status.get_pr_status",
                   side_effect=RuntimeError("gh exploded")):
            d = evaluate_review_gate(_session(), "wf-1")
        assert d.should_pause is True


class TestReopeningTheTaskSoARepairPathCanAct:
    """Holding the gate is necessary but not sufficient.

    Both repair paths match specific states, and a finished git_expert
    matches neither:

        _resolve_pending_pr_status : in_progress AND assigned_agent_id NULL
        _retry_failed_tasks        : failed
        a finished git_expert      : done, agent set, execution completed

    So a PR that goes red AFTER the task legitimately completed is
    unreachable -- the gate refuses to ask a human (correctly) and nothing
    fixes it either. Observed live on PR #1171: four failing checks, the
    workflow held open, no agent ever dispatched. IDB-2482.
    """

    def _db(self, task_status="done", exec_status="completed"):
        from src.core.database import PhaseExecution, Task

        phase = MagicMock(); phase.id = "phase-1"
        task = MagicMock()
        task.status = task_status
        task.assigned_agent_id = "agent-1"
        task.completed_at = "2026-09-29"
        execution = MagicMock(); execution.status = exec_status; execution.completed_at = None

        session = MagicMock()

        def _query(model):
            q = MagicMock()
            n = getattr(model, "__name__", "")
            if n == "Phase":
                q.filter_by.return_value.first.return_value = phase
            elif n == "PhaseExecution":
                q.filter_by.return_value.first.return_value = execution
            else:
                q.filter.return_value.order_by.return_value.first.return_value = task
            return q

        session.query.side_effect = _query
        return session, task, execution

    def test_a_completed_task_is_reopened_as_failed_with_the_blockers(self):
        from src.services.pr_readiness import reopen_git_expert_for_pr_fix

        session, task, _ = self._db()
        blockers = "failing checks: Backend: Lint & Format, gherkin-audit: Reproducibility Gate"
        with patch("src.autopilot.orchestrator.phase_transitions.transition_phase_execution"):
            assert reopen_git_expert_for_pr_fix(session, "wf-1", blockers) is True

        assert task.status == "failed", "must land in the state _retry_failed_tasks consumes"
        assert "Backend: Lint & Format" in task.failure_reason
        assert "SAME branch/PR" in task.failure_reason
        assert task.assigned_agent_id is None, "a stale agent id would block re-dispatch"

    def test_the_phase_execution_is_reopened_too(self):
        """Left completed, _advance_phases marches past it by order and the
        reopened task is never dispatched."""
        from src.services.pr_readiness import reopen_git_expert_for_pr_fix

        session, _, execution = self._db()
        with patch("src.autopilot.orchestrator.phase_transitions.transition_phase_execution") as t:
            reopen_git_expert_for_pr_fix(session, "wf-1", "failing checks: x")
        assert t.called, "phase execution must be reopened alongside the task"

    def test_a_task_already_actionable_is_left_alone(self):
        """failed / parked-in_progress are states a repair path already
        handles -- touching them would be churn."""
        from src.services.pr_readiness import reopen_git_expert_for_pr_fix

        for state in ("failed", "in_progress", "pending"):
            session, task, _ = self._db(task_status=state)
            assert reopen_git_expert_for_pr_fix(session, "wf-1", "x") is False
            assert task.status == state

    def test_the_gate_carries_the_blockers_for_the_reopen(self):
        """The hold decision and the reopen must use the same text, or the
        agent is told something different from what the gate decided on."""
        st = _status(ci_conclusion="failing", failing_checks=["Backend: Lint & Format"])
        with patch("src.services.github_pr_status.get_pr_status", return_value=st), \
             patch("src.autopilot.spec.get_max_task_retries", return_value=5):
            d = evaluate_review_gate(_session(retry_count=0), "wf-1")
        assert d.should_pause is False
        assert "Backend: Lint & Format" in d.blockers


class TestTheRetryBudgetCannotBeResetByANewTask:
    """Each reopen/retry cycle can create a fresh Task row starting at
    retry_count=0. Reading only the newest made the budget reset every
    round and the gate hold forever -- the unbounded-spend outcome the cap
    exists to prevent. Observed live: three git_expert tasks at 5, 1 and 0
    retries, with the gate reporting a full budget off the last one.
    IDB-2482."""

    FAILING = dict(ci_conclusion="failing", failing_checks=["gherkin-audit: Reproducibility Gate"])

    def test_attempts_are_summed_across_every_task(self):
        """5+1, 1+1, 0+1 = 8 attempts against a cap of 5 -> exhausted."""
        with patch("src.services.github_pr_status.get_pr_status", return_value=_status(**self.FAILING)), \
             patch("src.autopilot.spec.get_max_task_retries", return_value=5):
            d = evaluate_review_gate(_session(retry_count=[5, 1, 0]), "wf-1")
        assert d.should_pause is True
        assert d.stuck is True

    def test_a_brand_new_task_does_not_restore_headroom(self):
        """The exact live shape: an exhausted task plus a fresh one."""
        with patch("src.services.github_pr_status.get_pr_status", return_value=_status(**self.FAILING)), \
             patch("src.autopilot.spec.get_max_task_retries", return_value=5):
            d = evaluate_review_gate(_session(retry_count=[5, 0]), "wf-1")
        assert d.should_pause is True, "a new task must not hand back a spent budget"

    def test_genuine_headroom_still_holds_the_gate(self):
        """Two attempts used of five -- the loop should still get to run."""
        with patch("src.services.github_pr_status.get_pr_status", return_value=_status(**self.FAILING)), \
             patch("src.autopilot.spec.get_max_task_retries", return_value=5):
            d = evaluate_review_gate(_session(retry_count=[1]), "wf-1")
        assert d.should_pause is False


class TestGateFinishesAMergedPR:
    """Observed live: a human merged the PR while the tool was stopped.
    GitHub reports mergeStateStatus UNKNOWN for a merged PR, so
    ready_to_merge and needs_work are both False -- which the stuck arm
    read as 'awaiting human review -- PR is not mergeable yet' about a PR
    already on main."""

    def test_merged_pr_is_reported_merged_not_stuck(self):
        st = _status(state="MERGED", merge_state="UNKNOWN")
        with patch("src.services.github_pr_status.get_pr_status", return_value=st):
            d = evaluate_review_gate(_session(), "wf-1")
        assert d.merged is True
        assert d.stuck is False
        # Fail-closed default for a consumer that never learned about
        # `merged`: pausing is wrong-but-harmless, the hold path is not.
        assert d.should_pause is True

    def test_closed_without_merge_is_stuck_not_merged(self):
        st = _status(state="CLOSED", merge_state="UNKNOWN")
        with patch("src.services.github_pr_status.get_pr_status", return_value=st):
            d = evaluate_review_gate(_session(), "wf-1")
        assert d.merged is False
        assert d.stuck is True
        assert "closed without being merged" in d.status_reason

    def test_finish_completes_workflow_and_feature_when_every_phase_is_done(self):
        s = _session()
        with patch("src.core.status_derivation.derive_workflow_status", return_value="completed"):
            assert finish_merged_workflow(s, "wf-1") is True
        assert s._wf.status == "completed"
        assert s._wf.status_reason is None
        assert s._feature.status == "completed"
        s.commit.assert_called_once()

    def test_finish_leaves_workflow_alone_while_a_phase_is_still_open(self):
        """A merged PR must not mark a half-finished workflow complete."""
        s = _session()
        with patch("src.core.status_derivation.derive_workflow_status", return_value="active"):
            assert finish_merged_workflow(s, "wf-1") is False
        assert s._wf.status == "active"
        s.commit.assert_not_called()
