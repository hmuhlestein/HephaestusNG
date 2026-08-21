"""Regression test: _cleanup_worktree's stale-working_directory clearing
must not touch a paused (resumable) workflow, mirroring the same fix in
worktree_manager.py's cleanup_all_stale_branches.

Found on adversarial review: pause_feature (autopilot_api.py) sets a
workflow to "paused" while deliberately keeping working_directory intact so
_resume_interrupted_workflows can restart the agent on its existing worktree
branch later. The original fix here only excluded "active" workflows from
having their working_directory nulled, leaving every paused workflow exposed
to the same "agent creation can't find the shared worktree, falls back to
an isolated per-agent one" breakage this fix exists to prevent.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.core.database import DatabaseManager, Workflow


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


class TestCleanupWorktreeDoesNotTouchPausedWorkflow:
    def test_paused_workflow_working_directory_survives(
        self, tmp_path, test_db, config, monkeypatch
    ):
        from src.autopilot.orchestrator.worktree_integration import _cleanup_worktree

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        project_path = tmp_path / "project"
        project_path.mkdir()

        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-paused",
                name="Phase 0",
                phases_folder_path="/tmp",
                status="paused",
                definition_id="feature_architect",
                working_directory=str(worktree),
            )
        )
        session.commit()
        session.close()

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)

        logger = MagicMock()
        with patch("src.core.worktree_manager.WorktreeManager") as MockWtMgr:
            mock_instance = MockWtMgr.return_value
            mock_instance.main_repo.git.worktree = MagicMock()
            _cleanup_worktree(worktree, "feature_architect/x", project_path, logger)

        session = test_db.get_session()
        wf = session.query(Workflow).filter_by(id="wf-paused").first()
        assert wf.working_directory == str(worktree), (
            "a paused (resumable) workflow's working_directory must not be "
            "cleared by cleanup meant only for genuinely stale entries"
        )
        session.close()

    def test_failed_workflow_working_directory_is_cleared(
        self, tmp_path, test_db, config, monkeypatch
    ):
        """Sanity check the guard isn't overbroad: a genuinely terminal
        (failed) workflow should still have its stale working_directory
        cleared, same as before this fix."""
        from src.autopilot.orchestrator.worktree_integration import _cleanup_worktree

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        project_path = tmp_path / "project"
        project_path.mkdir()

        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-failed",
                name="Phase 0",
                phases_folder_path="/tmp",
                status="failed",
                definition_id="feature_architect",
                working_directory=str(worktree),
            )
        )
        session.commit()
        session.close()

        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)

        logger = MagicMock()
        with patch("src.core.worktree_manager.WorktreeManager") as MockWtMgr:
            mock_instance = MockWtMgr.return_value
            mock_instance.main_repo.git.worktree = MagicMock()
            _cleanup_worktree(worktree, "feature_architect/x", project_path, logger)

        session = test_db.get_session()
        wf = session.query(Workflow).filter_by(id="wf-failed").first()
        assert wf.working_directory is None
        session.close()
