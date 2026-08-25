"""Coverage for attempt_recovery's independent recovery strategies (SOLID 2.5).

attempt_recovery fuses four unrelated recovery actions into one function body:
retry failed tasks, clean stale assigned tasks, clean stale git/merge state,
and terminate stale agents. The finding is an SRP one, but the fusion has a
concrete consequence -- a guard belonging to one strategy can short-circuit
the others, because they all share a single function scope and a single set
of `return` statements.

These tests pin each strategy as independently reachable, which is the
property the decomposition has to preserve.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def logger():
    return MagicMock()


@pytest.fixture
def no_db():
    """get_db yielding a session where every query comes back empty."""
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    db.query.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter_by.return_value.first.return_value = None
    db.__enter__ = MagicMock(return_value=db)
    db.__exit__ = MagicMock(return_value=False)
    return db


@contextmanager
def _session_cm(manager):
    """get_db stand-in backed by a real DatabaseManager session."""
    session = manager.get_session()
    try:
        yield session
        session.commit()
    finally:
        session.close()


def _dead_tmux(*args, **kwargs):
    """subprocess.run stand-in: `tmux has-session` fails, git calls succeed."""
    result = MagicMock()
    argv = args[0] if args else []
    if argv and argv[0] == "tmux":
        result.returncode = 1  # session is gone
    else:
        result.returncode = 0
        result.stdout = ""
    return result


class TestStrategyIndependence:
    def test_stale_agents_are_terminated_even_without_a_project_path(
        self, logger, no_db
    ):
        """Regression: a guard belonging to the git-cleanup strategy used to
        `return` out of attempt_recovery entirely when the project path could
        not be resolved -- silently skipping stale-agent termination, an
        unrelated strategy that needs no project path at all.

        A workflow with no working_directory and no PROJECT_PATH in the
        environment would therefore never have its dead agents reaped, and
        the tasks pointing at them stayed stuck behind agents that no longer
        existed.
        """
        from src.autopilot.orchestrator.policy import attempt_recovery

        with (
            patch("src.core.database.get_db", return_value=no_db),
            patch("src.autopilot.orchestrator.policy.get_db", return_value=no_db),
            patch(
                "src.autopilot.orchestrator.phase_transitions.get_tasks",
                return_value=[],
            ),
            patch(
                "src.autopilot.orchestrator.policy.get_agents",
                return_value=[
                    {
                        "id": "agent-dead",
                        "status": "working",
                        "tmux_session_name": "gone",
                    }
                ],
            ),
            patch(
                "src.autopilot.orchestrator.policy.terminate_agent_direct"
            ) as terminate,
            patch(
                "src.autopilot.orchestrator.policy.subprocess.run",
                side_effect=_dead_tmux,
            ),
            patch("src.autopilot.orchestrator.policy.os.getenv", return_value=None),
        ):
            success, msg = attempt_recovery("wf-1", logger)

        terminate.assert_called_once_with("agent-dead")
        assert success is True
        assert "agent-dead"[:8] in msg

    def test_live_agents_are_left_alone(self, logger, no_db):
        """Only agents whose tmux session is actually gone may be reaped --
        terminating merely-"working" agents killed live ones roughly once a
        minute in production."""
        from src.autopilot.orchestrator.policy import attempt_recovery

        def alive_tmux(*args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            return result

        with (
            patch("src.core.database.get_db", return_value=no_db),
            patch("src.autopilot.orchestrator.policy.get_db", return_value=no_db),
            patch(
                "src.autopilot.orchestrator.phase_transitions.get_tasks",
                return_value=[],
            ),
            patch(
                "src.autopilot.orchestrator.policy.get_agents",
                return_value=[
                    {
                        "id": "agent-live",
                        "status": "working",
                        "tmux_session_name": "alive",
                    }
                ],
            ),
            patch(
                "src.autopilot.orchestrator.policy.terminate_agent_direct"
            ) as terminate,
            patch(
                "src.autopilot.orchestrator.policy.subprocess.run",
                side_effect=alive_tmux,
            ),
            patch("src.autopilot.orchestrator.policy.os.getenv", return_value=None),
        ):
            attempt_recovery("wf-1", logger)

        terminate.assert_not_called()

    def test_git_cleanup_failure_does_not_skip_agent_termination(self, logger, no_db):
        """Each strategy is best-effort and independent: the git commands
        blowing up must not prevent dead agents from being reaped."""
        from src.autopilot.orchestrator.policy import attempt_recovery

        def explode_git_but_kill_tmux(*args, **kwargs):
            argv = args[0] if args else []
            if argv and argv[0] == "git":
                raise OSError("git missing")
            result = MagicMock()
            result.returncode = 1  # tmux session gone
            return result

        with (
            patch("src.core.database.get_db", return_value=no_db),
            patch("src.autopilot.orchestrator.policy.get_db", return_value=no_db),
            patch(
                "src.autopilot.orchestrator.phase_transitions.get_tasks",
                return_value=[],
            ),
            patch(
                "src.autopilot.orchestrator.policy.get_agents",
                return_value=[
                    {
                        "id": "agent-dead",
                        "status": "working",
                        "tmux_session_name": "gone",
                    }
                ],
            ),
            patch(
                "src.autopilot.orchestrator.policy.terminate_agent_direct"
            ) as terminate,
            patch(
                "src.autopilot.orchestrator.policy.subprocess.run",
                side_effect=explode_git_but_kill_tmux,
            ),
            patch(
                "src.autopilot.orchestrator.policy.os.getenv",
                return_value="/tmp/proj/.worktrees/wt_does-not-matter",
            ),
        ):
            attempt_recovery("wf-1", logger)

        terminate.assert_called_once_with("agent-dead")


class TestCleanStaleRepoStateWorktreeGuard:
    """_clean_stale_repo_state must never run its destructive git sequence
    (merge --abort / checkout main / clean -fd / reset --hard) against a
    project's primary checkout -- only an isolated .worktrees/ path.
    _resolve_recovery_project_path falls back to the primary repo path for
    any workflow with no live working_directory, and that primary path is
    the one a human's uncommitted work (e.g. a design spec added via the
    dashboard) or another agent's in-progress edits can legitimately be
    sitting in. Confirmed live: an untracked docs/bugfix/*.md spec was
    deleted this way."""

    def test_primary_checkout_path_is_never_cleaned(self, logger, no_db):
        from src.autopilot.orchestrator.policy import attempt_recovery

        calls = []

        def record_and_succeed(*args, **kwargs):
            calls.append(args[0] if args else [])
            result = MagicMock()
            result.returncode = 0
            result.stdout = "M some_dirty_file.py"  # repo looks dirty
            return result

        with (
            patch("src.core.database.get_db", return_value=no_db),
            patch("src.autopilot.orchestrator.policy.get_db", return_value=no_db),
            patch(
                "src.autopilot.orchestrator.phase_transitions.get_tasks",
                return_value=[],
            ),
            patch("src.autopilot.orchestrator.policy.get_agents", return_value=[]),
            patch(
                "src.autopilot.orchestrator.policy.subprocess.run",
                side_effect=record_and_succeed,
            ),
            patch(
                "src.autopilot.orchestrator.policy.os.getenv",
                return_value="/Users/someone/code/some-project",
            ),
        ):
            attempt_recovery("wf-1", logger)

        git_calls = [c for c in calls if c and c[0] == "git"]
        assert git_calls == [], f"expected no git subprocess calls against the primary checkout, got {git_calls}"

    def test_worktree_path_is_still_cleaned(self, logger, no_db):
        from src.autopilot.orchestrator.policy import attempt_recovery

        calls = []

        def record_and_succeed(*args, **kwargs):
            calls.append(args[0] if args else [])
            result = MagicMock()
            result.returncode = 0
            result.stdout = "M some_dirty_file.py"
            return result

        with (
            patch("src.core.database.get_db", return_value=no_db),
            patch("src.autopilot.orchestrator.policy.get_db", return_value=no_db),
            patch(
                "src.autopilot.orchestrator.phase_transitions.get_tasks",
                return_value=[],
            ),
            patch("src.autopilot.orchestrator.policy.get_agents", return_value=[]),
            patch(
                "src.autopilot.orchestrator.policy.subprocess.run",
                side_effect=record_and_succeed,
            ),
            patch(
                "src.autopilot.orchestrator.policy.os.getenv",
                return_value="/Users/someone/code/some-project/.worktrees/wt_feature-abc",
            ),
        ):
            attempt_recovery("wf-1", logger)

        git_calls = [c for c in calls if c and c[0] == "git"]
        assert ["git", "reset", "--hard", "HEAD"] in git_calls


class TestStaleTaskFailureReason:
    """Uses a real DB rather than mocks -- the drift being guarded here is in
    what gets written to the row, which a MagicMock session would happily
    accept either way."""

    def _seed(self, db_path):
        from datetime import datetime

        from src.core.database import Agent, DatabaseManager, Task, Workflow

        manager = DatabaseManager(str(db_path))
        manager.create_tables()
        session = manager.get_session()
        session.add(
            Workflow(
                id="wf-1",
                name="wf",
                phases_folder_path="/tmp",
                status="active",
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            Agent(
                id="agent-1",
                system_prompt="p",
                status="terminated",
                cli_type="test",
                agent_type="phase",
            )
        )
        session.add(
            Task(
                id="task-1",
                raw_description="do it",
                done_definition="done",
                status="in_progress",
                workflow_id="wf-1",
                assigned_agent_id="agent-1",
                failure_reason="required output(s) invalid: docs/spec.md missing",
            )
        )
        session.commit()
        session.close()
        return manager

    def test_a_specific_failure_reason_is_not_clobbered(self, logger, tmp_path):
        """update_task_status' verification records precisely why a "done"
        claim was rejected, and _maybe_retry_failed_tasks feeds that reason
        into the next attempt's prompt. Overwriting it with the generic
        "agent terminated" message costs the retry the only feedback that
        tells it what to fix. features.py's sibling implementation already
        guards this; this copy had drifted.
        """
        from src.autopilot.orchestrator.policy import (
            _fail_tasks_with_terminated_agents,
        )
        from src.core.database import Task

        manager = self._seed(tmp_path / "recovery.db")

        with patch(
            "src.core.database.get_db",
            lambda: _session_cm(manager),
        ):
            _fail_tasks_with_terminated_agents("wf-1", logger)

        session = manager.get_session()
        try:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "failed"
            assert task.failure_reason == (
                "required output(s) invalid: docs/spec.md missing"
            )
        finally:
            session.close()

    def test_a_task_with_no_reason_still_gets_the_generic_one(self, logger, tmp_path):
        from src.autopilot.orchestrator.policy import (
            _fail_tasks_with_terminated_agents,
        )
        from src.core.database import Task

        manager = self._seed(tmp_path / "recovery2.db")
        session = manager.get_session()
        session.query(Task).filter_by(id="task-1").first().failure_reason = None
        session.commit()
        session.close()

        with patch(
            "src.core.database.get_db",
            lambda: _session_cm(manager),
        ):
            _fail_tasks_with_terminated_agents("wf-1", logger)

        session = manager.get_session()
        try:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.failure_reason == "Agent agent-1 terminated unexpectedly"
        finally:
            session.close()


class TestReturnContract:
    def test_reports_no_action_when_nothing_was_recovered(self, logger, no_db):
        from src.autopilot.orchestrator.policy import attempt_recovery

        with (
            patch("src.core.database.get_db", return_value=no_db),
            patch("src.autopilot.orchestrator.policy.get_db", return_value=no_db),
            patch(
                "src.autopilot.orchestrator.phase_transitions.get_tasks",
                return_value=[],
            ),
            patch("src.autopilot.orchestrator.policy.get_agents", return_value=[]),
            patch(
                "src.autopilot.orchestrator.policy.subprocess.run",
                side_effect=_dead_tmux,
            ),
            patch("src.autopilot.orchestrator.policy.os.getenv", return_value=None),
        ):
            success, msg = attempt_recovery("wf-1", logger)

        assert success is False
        assert "No recovery actions needed" in msg
