"""Tests for _cleanup_worktree's tmux-transcript archiving step.

.hephaestus/ is git-excluded, so tmux pipe-pane transcripts written there
(src/agents/manager.py) never survive a normal git merge the way docs/*.md
report artifacts do -- they're deleted along with the worktree once the
feature pipeline finishes. _cleanup_worktree must copy them out to the
project root's .hephaestus/tmux/ (the same location _assess_run_health
reads from) before removing the worktree.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.core.constants import CONTEXT_DIR_NAME


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """_cleanup_worktree does `db = DbManager(str(cfg.paths.database_path))`
    before ever reaching the archiving step this file tests. get_config() is
    imported locally inside the function
    (`from src.core.simple_config import get_config`), so unlike
    worktree_integration's other tests (see
    test_heal_orphaned_agent_branches.py), patching the definition site here
    is sufficient -- the import re-resolves at call time.

    Without this, cfg.paths.database_path defaults to the real
    "hephaestus.db": every test in this file was constructing a
    DatabaseManager against the production database. Harmless only because
    WorktreeManager itself is mocked below and nothing reads from `db`
    afterward -- but a silent isolation bypass all the same, exactly the
    class tests/conftest.py's _forbid_production_database guard now catches.
    """
    import src.core.simple_config as sc

    cfg = sc.Config()
    cfg.paths.database_path = str(tmp_path / "isolated_test.db")
    monkeypatch.setattr(sc, "get_config", lambda: cfg)
    return cfg


class TestCleanupWorktreeTmuxArchive:
    def test_archives_tmux_logs_before_worktree_removal(self, tmp_path):
        from src.autopilot.orchestrator.worktree_integration import _cleanup_worktree

        worktree = tmp_path / "worktree"
        project_path = tmp_path / "project"
        (worktree / CONTEXT_DIR_NAME / "tmux").mkdir(parents=True)
        (worktree / CONTEXT_DIR_NAME / "tmux" / "development_abc123.log").write_text(
            "some transcript content"
        )
        project_path.mkdir()

        logger = MagicMock()
        with patch("src.core.worktree_manager.WorktreeManager") as MockWtMgr:
            mock_instance = MockWtMgr.return_value
            mock_instance.main_repo.git.worktree = MagicMock()
            _cleanup_worktree(worktree, "feature/x", project_path, logger)

        archived = (
            project_path / CONTEXT_DIR_NAME / "tmux" / "development_abc123.log"
        )
        assert archived.exists()
        assert archived.read_text() == "some transcript content"

    def test_merges_into_existing_archive_without_clobbering(self, tmp_path):
        """Regression: a second feature's cleanup must not wipe out a first
        feature's already-archived transcripts (dest dir already has files)."""
        from src.autopilot.orchestrator.worktree_integration import _cleanup_worktree

        worktree = tmp_path / "worktree"
        project_path = tmp_path / "project"
        (worktree / CONTEXT_DIR_NAME / "tmux").mkdir(parents=True)
        (worktree / CONTEXT_DIR_NAME / "tmux" / "qa_validation_new.log").write_text(
            "new feature's transcript"
        )
        existing_archive = project_path / CONTEXT_DIR_NAME / "tmux"
        existing_archive.mkdir(parents=True)
        (existing_archive / "development_old.log").write_text(
            "prior feature's transcript"
        )

        logger = MagicMock()
        with patch("src.core.worktree_manager.WorktreeManager") as MockWtMgr:
            mock_instance = MockWtMgr.return_value
            mock_instance.main_repo.git.worktree = MagicMock()
            _cleanup_worktree(worktree, "feature/y", project_path, logger)

        assert (existing_archive / "development_old.log").exists()
        assert (existing_archive / "qa_validation_new.log").exists()

    def test_no_tmux_dir_does_not_error(self, tmp_path):
        """No .hephaestus/tmux/ in the worktree (e.g. pipe-pane setup failed
        earlier) -> cleanup must not crash."""
        from src.autopilot.orchestrator.worktree_integration import _cleanup_worktree

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        project_path = tmp_path / "project"
        project_path.mkdir()

        logger = MagicMock()
        with patch("src.core.worktree_manager.WorktreeManager") as MockWtMgr:
            mock_instance = MockWtMgr.return_value
            mock_instance.main_repo.git.worktree = MagicMock()
            _cleanup_worktree(worktree, "feature/z", project_path, logger)

        assert not (project_path / CONTEXT_DIR_NAME / "tmux").exists()
