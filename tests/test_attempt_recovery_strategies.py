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
                return_value="/tmp/does-not-matter",
            ),
        ):
            attempt_recovery("wf-1", logger)

        terminate.assert_called_once_with("agent-dead")


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
