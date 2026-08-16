"""Regression test: _cleanup_worktree must route worktree removal through
WorktreeManager._remove_worktree's require_clean guard instead of a raw
`git worktree remove --force`.

Before this fix, _cleanup_worktree called `wt_mgr.main_repo.git.worktree(
"remove", ..., "--force")` directly, bypassing the same require_clean check
that protects every other removal path in worktree_manager.py. A feature
pipeline that "completed" while real, uncommitted work was still sitting in
its worktree (e.g. a crash-induced false "abandoned" marking, or a phase's
own commit step silently failing) had that work permanently destroyed --
the exact failure mode _remove_worktree's require_clean guard exists to
prevent, already observed live once via cleanup_all_stale_branches's
identical bypass (see worktree_manager.py's _remove_worktree docstring).
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from git import Repo

from src.core.database import DatabaseManager


@pytest.fixture
def main_repo(tmp_path):
    repo_dir = tmp_path / "main_repo"
    repo_dir.mkdir()
    repo = Repo.init(repo_dir)
    (repo_dir / "README.md").write_text("# Test\n")
    repo.index.add(["README.md"])
    repo.index.commit("Initial commit")
    return repo


@pytest.fixture
def test_db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "test.db"))
    manager.create_tables()
    return manager


@pytest.fixture
def config(main_repo, test_db):
    import src.core.simple_config

    cfg = src.core.simple_config.Config()
    cfg.database_path = test_db.engine.url.database
    cfg.main_repo_path = Path(main_repo.working_dir)
    return cfg


def _make_worktree(main_repo, tmp_path, branch, dirty):
    worktree_path = tmp_path / "worktree"
    main_repo.git.branch(branch)
    main_repo.git.worktree("add", str(worktree_path), branch)
    if dirty:
        (worktree_path / "scratch.txt").write_text("uncommitted work\n")
    return worktree_path


class TestCleanupWorktreeSafetyGuard:
    def test_dirty_worktree_is_not_destroyed(
        self, tmp_path, main_repo, config, monkeypatch
    ):
        from src.autopilot.orchestrator.worktree_integration import _cleanup_worktree

        worktree = _make_worktree(main_repo, tmp_path, "feature/dirty", dirty=True)
        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
        monkeypatch.setattr(
            "src.core.worktree_manager.get_config", lambda: config
        )

        logger = MagicMock()
        _cleanup_worktree(worktree, "feature/dirty", Path(main_repo.working_dir), logger)

        assert worktree.exists(), (
            "a worktree with uncommitted changes must survive cleanup -- "
            "require_clean must have refused the removal"
        )
        assert (worktree / "scratch.txt").exists()

    def test_clean_worktree_is_removed(
        self, tmp_path, main_repo, config, monkeypatch
    ):
        from src.autopilot.orchestrator.worktree_integration import _cleanup_worktree

        worktree = _make_worktree(main_repo, tmp_path, "feature/clean", dirty=False)
        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
        monkeypatch.setattr(
            "src.core.worktree_manager.get_config", lambda: config
        )

        logger = MagicMock()
        _cleanup_worktree(worktree, "feature/clean", Path(main_repo.working_dir), logger)

        assert not worktree.exists(), (
            "a worktree with no uncommitted changes must be removed by cleanup, "
            "same as before this fix"
        )
