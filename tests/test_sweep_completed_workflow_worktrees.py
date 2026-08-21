"""Regression test: sweep_completed_workflow_worktrees must clean up
worktrees for workflows that reached "completed" status but never got
their normal post-completion _cleanup_worktree() call to run (e.g. the
backend restarted in the gap between run_single_workflow returning
"completed" and _run_one_feature's own cleanup call a few lines later).

Before this fix, nothing ever revisited a "completed" workflow again, so
an orphaned worktree from that race sat on disk forever unless someone
manually reran that exact design or hit /cleanup-branches by hand.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.core.database import Agent, DatabaseManager, Task, Workflow


@pytest.fixture
def test_db(tmp_path):
    db_path = tmp_path / "test.db"
    manager = DatabaseManager(str(db_path))
    manager.create_tables()
    return manager


@pytest.fixture
def config(tmp_path, test_db):
    import src.core.simple_config

    cfg = src.core.simple_config.Config()
    cfg.paths.database_path = test_db.engine.url.database
    return cfg


def _make_worktree(tmp_path, name, real=True):
    worktree = tmp_path / name
    worktree.mkdir(parents=True)
    if real:
        (worktree / ".git").mkdir()
    return worktree


class TestSweepCompletedWorkflowWorktrees:
    def test_cleans_up_orphaned_worktree_for_completed_workflow(
        self, tmp_path, test_db, config, monkeypatch
    ):
        from src.autopilot.orchestrator.worktree_integration import sweep_completed_workflow_worktrees

        worktree = _make_worktree(tmp_path, ".worktrees/wt_feature-x")
        project_path = tmp_path / "project"
        project_path.mkdir()

        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-done",
                name="Feature X",
                phases_folder_path="/tmp",
                status="completed",
                definition_id="autopilot",
                working_directory=str(worktree),
                launch_params={"project_path": str(project_path)},
            )
        )
        session.commit()
        session.close()

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)

        with patch("src.autopilot.orchestrator.worktree_integration._cleanup_worktree") as mock_cleanup:
            removed = sweep_completed_workflow_worktrees(MagicMock())

        assert removed == 1
        mock_cleanup.assert_called_once()
        call_args = mock_cleanup.call_args[0]
        assert call_args[0] == worktree
        assert str(call_args[2]) == str(project_path)

    def test_skips_workflow_whose_worktree_is_already_gone(
        self, tmp_path, test_db, config, monkeypatch
    ):
        from src.autopilot.orchestrator.worktree_integration import sweep_completed_workflow_worktrees

        worktree = _make_worktree(tmp_path, ".worktrees/wt_feature-y", real=False)
        project_path = tmp_path / "project"
        project_path.mkdir()

        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-done-2",
                name="Feature Y",
                phases_folder_path="/tmp",
                status="completed",
                definition_id="autopilot",
                working_directory=str(worktree),
                launch_params={"project_path": str(project_path)},
            )
        )
        session.commit()
        session.close()

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)

        with patch("src.autopilot.orchestrator.worktree_integration._cleanup_worktree") as mock_cleanup:
            removed = sweep_completed_workflow_worktrees(MagicMock())

        assert removed == 0
        mock_cleanup.assert_not_called()

    def test_skips_workflow_missing_project_path_instead_of_guessing(
        self, tmp_path, test_db, config, monkeypatch
    ):
        from src.autopilot.orchestrator.worktree_integration import sweep_completed_workflow_worktrees

        worktree = _make_worktree(tmp_path, ".worktrees/wt_feature-z")

        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-done-3",
                name="Feature Z",
                phases_folder_path="/tmp",
                status="completed",
                definition_id="autopilot",
                working_directory=str(worktree),
                launch_params={},
            )
        )
        session.commit()
        session.close()

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)

        with patch("src.autopilot.orchestrator.worktree_integration._cleanup_worktree") as mock_cleanup:
            removed = sweep_completed_workflow_worktrees(MagicMock())

        assert removed == 0
        mock_cleanup.assert_not_called()

    def test_does_not_touch_active_or_paused_workflow_worktrees(
        self, tmp_path, test_db, config, monkeypatch
    ):
        from src.autopilot.orchestrator.worktree_integration import sweep_completed_workflow_worktrees

        worktree = _make_worktree(tmp_path, ".worktrees/wt_feature-active")
        project_path = tmp_path / "project"
        project_path.mkdir()

        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-active",
                name="Feature Active",
                phases_folder_path="/tmp",
                status="active",
                definition_id="autopilot",
                working_directory=str(worktree),
                launch_params={"project_path": str(project_path)},
            )
        )
        session.commit()
        session.close()

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)

        with patch("src.autopilot.orchestrator.worktree_integration._cleanup_worktree") as mock_cleanup:
            removed = sweep_completed_workflow_worktrees(MagicMock())

        assert removed == 0
        mock_cleanup.assert_not_called()

    def test_skips_completed_workflow_with_a_live_agent_still_working(
        self, tmp_path, test_db, config, monkeypatch
    ):
        """A goto-triggered re-run of an earlier phase can still be
        in-flight, with its agent still 'working', even after the
        workflow's overall status has already flipped to 'completed'
        through a different path. Force-removing the worktree in that
        case destroys the live agent's in-progress work and leaves it
        stuck forever with a deleted cwd."""
        from src.autopilot.orchestrator.worktree_integration import sweep_completed_workflow_worktrees

        worktree = _make_worktree(tmp_path, ".worktrees/wt_feature-straggler")
        project_path = tmp_path / "project"
        project_path.mkdir()

        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-done-straggler",
                name="Feature Straggler",
                phases_folder_path="/tmp",
                status="completed",
                definition_id="autopilot",
                working_directory=str(worktree),
                launch_params={"project_path": str(project_path)},
            )
        )
        session.add(
            Task(
                id="task-straggler",
                raw_description="security_review re-run",
                done_definition="n/a",
                status="in_progress",
                workflow_id="wf-done-straggler",
                assigned_agent_id="agent-straggler",
            )
        )
        session.add(
            Agent(
                id="agent-straggler",
                system_prompt="n/a",
                status="working",
                cli_type="pi",
                current_task_id="task-straggler",
            )
        )
        session.commit()
        session.close()

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)

        with patch("src.autopilot.orchestrator.worktree_integration._cleanup_worktree") as mock_cleanup:
            removed = sweep_completed_workflow_worktrees(MagicMock())

        assert removed == 0
        mock_cleanup.assert_not_called()
