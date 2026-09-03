"""Tests for _resolve_pending_pr_status -- §3.3's periodic sweep-tick
resolver for a git_expert task left `in_progress` awaiting an open PR's
CI/review outcome (verify_git_expert_merged_and_pushed's pending branch).
Mirrors TestMaybeResolveArbitration's shape in test_advance_phases.py --
same "seed real DB state, patch only the external call, confirm the
sweep-tick consumer resolves it" pattern.
"""

from unittest.mock import MagicMock, patch

from src.core.database import (
    Feature,
    Phase,
    PhaseExecution,
    Task,
    Workflow,
    WorkflowDefinition,
)
from src.services.github_pr_status import PRStatus


def _seed(db_manager, feature_pr_url="https://github.com/o/r/pull/1", task_status="in_progress"):
    with db_manager.session_scope() as session:
        session.add(WorkflowDefinition(id="autopilot", name="Autopilot"))
        session.add(Workflow(
            id="wf-1", name="Autopilot", status="active",
            phases_folder_path="/tmp", definition_id="autopilot",
        ))
        session.add(Phase(
            id="phase-1", workflow_id="wf-1", order=1, name="git_expert",
            description="d", done_definitions=["x"],
        ))
        session.add(PhaseExecution(id="exec-1", phase_id="phase-1", status="in_progress"))
        session.add(Task(
            id="task-1", workflow_id="wf-1", phase_id="phase-1",
            raw_description="r", done_definition="d", status=task_status,
        ))
        session.add(Feature(
            id="feat-1", design_id="does-not-matter", feature_key="x",
            name="X", scope="s", status="active", workflow_id="wf-1",
            pr_url=feature_pr_url,
        ))


class TestResolvePendingPRStatus:
    def test_still_pending_leaves_task_untouched(self, db_manager):
        from src.autopilot.orchestrator.pr_resolution import _resolve_pending_pr_status

        _seed(db_manager)
        fake_status = PRStatus(
            url="https://github.com/o/r/pull/1", state="OPEN",
            ci_conclusion="pending", review_decision=None, summary="CI is still running",
        )
        with patch("src.services.github_pr_status.get_pr_status", return_value=fake_status):
            _resolve_pending_pr_status("wf-1", MagicMock())

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "in_progress"
            assert task.failure_reason is None

    def test_gh_lookup_failure_leaves_task_untouched(self, db_manager):
        """A transient gh failure must not resolve the task either way --
        checked again next tick, exactly like still-pending."""
        from src.autopilot.orchestrator.pr_resolution import _resolve_pending_pr_status

        _seed(db_manager)
        with patch("src.services.github_pr_status.get_pr_status", return_value=None):
            _resolve_pending_pr_status("wf-1", MagicMock())

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "in_progress"

    def test_ci_now_passing_marks_the_task_done(self, db_manager):
        from src.autopilot.orchestrator.pr_resolution import _resolve_pending_pr_status

        _seed(db_manager)
        fake_status = PRStatus(
            url="https://github.com/o/r/pull/1", state="OPEN",
            ci_conclusion="passing", review_decision=None, summary="CI passing, no changes requested",
        )
        with patch("src.services.github_pr_status.get_pr_status", return_value=fake_status):
            _resolve_pending_pr_status("wf-1", MagicMock())

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "done"
            assert task.completed_at is not None
            assert task.failure_reason is None
            assert "CI passing" in (task.completion_notes or "")

    def test_ci_now_failing_marks_the_task_failed_with_a_real_reason(self, db_manager):
        """This is what makes the task visible to _maybe_retry_failed_tasks
        and eventually _trigger_arbitration -- the exact same machinery
        fixed earlier this session, now genuinely reachable for a CI/review
        failure instead of a mechanical pause with no way back."""
        from src.autopilot.orchestrator.pr_resolution import _resolve_pending_pr_status

        _seed(db_manager)
        fake_status = PRStatus(
            url="https://github.com/o/r/pull/1", state="OPEN",
            ci_conclusion="failing", review_decision=None,
            failing_checks=["go test"], summary="CI check(s) failed: go test",
        )
        with patch("src.services.github_pr_status.get_pr_status", return_value=fake_status):
            _resolve_pending_pr_status("wf-1", MagicMock())

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "failed"
            assert "go test" in task.failure_reason
            assert "same" in task.failure_reason.lower()

    def test_changes_requested_marks_the_task_failed(self, db_manager):
        from src.autopilot.orchestrator.pr_resolution import _resolve_pending_pr_status

        _seed(db_manager)
        fake_status = PRStatus(
            url="https://github.com/o/r/pull/1", state="OPEN",
            ci_conclusion="passing", review_decision="CHANGES_REQUESTED",
            summary="a reviewer requested changes on this PR",
        )
        with patch("src.services.github_pr_status.get_pr_status", return_value=fake_status):
            _resolve_pending_pr_status("wf-1", MagicMock())

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "failed"
            assert "requested changes" in task.failure_reason

    def test_no_pr_url_yet_is_a_noop(self, db_manager):
        """A git_expert task can be in_progress for perfectly ordinary
        reasons (an agent is genuinely still working) before it ever
        reaches the pending-PR state -- Feature.pr_url unset is exactly
        how this function tells the two apart without a new column."""
        from src.autopilot.orchestrator.pr_resolution import _resolve_pending_pr_status

        _seed(db_manager, feature_pr_url=None)
        with patch("src.services.github_pr_status.get_pr_status") as mock_get:
            _resolve_pending_pr_status("wf-1", MagicMock())

        mock_get.assert_not_called()
        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "in_progress"

    def test_no_in_progress_task_is_a_noop(self, db_manager):
        from src.autopilot.orchestrator.pr_resolution import _resolve_pending_pr_status

        _seed(db_manager, task_status="done")
        with patch("src.services.github_pr_status.get_pr_status") as mock_get:
            _resolve_pending_pr_status("wf-1", MagicMock())

        mock_get.assert_not_called()

    def test_phase_execution_not_in_progress_is_a_noop(self, db_manager):
        """The phase itself already advanced past git_expert (or was never
        the current phase) -- nothing to resolve here regardless of task
        status."""
        from src.autopilot.orchestrator.pr_resolution import _resolve_pending_pr_status

        _seed(db_manager)
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.status = "completed"

        with patch("src.services.github_pr_status.get_pr_status") as mock_get:
            _resolve_pending_pr_status("wf-1", MagicMock())

        mock_get.assert_not_called()


class TestPendingPRStateSurvivesStaleAssignedTaskCleanup:
    """Regression found during implementation, not in the original review:
    _clean_stale_assigned_tasks (features.py, runs every sweep tick before
    _resolve_pending_pr_status gets a chance to) matches ANY task with
    status in ("pending", "queued", "blocked", "assigned", "in_progress")
    AND a non-null assigned_agent_id pointing at a terminated agent -- it
    would re-fail a pending-PR task as "Agent terminated unexpectedly"
    within one sweep tick, discarding the real still-pending outcome and
    burning retry budget on nothing, UNLESS the completion floor clears
    assigned_agent_id (not just status) when it leaves the task
    in_progress. Every OTHER completion path is naturally exempt only
    because it sets status to done/failed (outside that query's list)
    before the agent is terminated -- this is the one path that doesn't,
    by design, so it needs the explicit clear."""

    def test_stale_task_cleanup_does_not_refail_a_pending_pr_task(self, db_manager):
        from src.autopilot.orchestrator.features import _clean_stale_assigned_tasks
        from src.core.database import Agent

        _seed(db_manager)
        with db_manager.session_scope() as session:
            session.add(Agent(id="agent-1", system_prompt="p", cli_type="claude", status="terminated"))
            task = session.query(Task).filter_by(id="task-1").first()
            # The exact state verify_git_expert_merged_and_pushed's pending
            # branch leaves behind: in_progress, assigned_agent_id cleared.
            task.status = "in_progress"
            task.assigned_agent_id = None

        _clean_stale_assigned_tasks("wf-1", MagicMock())

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "in_progress"
            assert task.failure_reason is None

    def test_without_clearing_assigned_agent_id_the_task_would_be_refailed(self, db_manager):
        """Characterizes the bug this fix avoids: leaving assigned_agent_id
        pointing at the terminated agent (the pre-fix shape) really does
        get caught and re-failed by the existing detector."""
        from src.autopilot.orchestrator.features import _clean_stale_assigned_tasks
        from src.core.database import Agent

        _seed(db_manager)
        with db_manager.session_scope() as session:
            session.add(Agent(id="agent-1", system_prompt="p", cli_type="claude", status="terminated"))
            task = session.query(Task).filter_by(id="task-1").first()
            task.status = "in_progress"
            task.assigned_agent_id = "agent-1"  # NOT cleared -- the bug scenario

        _clean_stale_assigned_tasks("wf-1", MagicMock())

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "failed"
            assert "terminated unexpectedly" in task.failure_reason
