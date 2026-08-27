"""Regression: WorktreeRemover.remove logged every REFUSAL to remove a
worktree (main-repo guard, dirty-worktree guard, unreadable-worktree
guard) but nothing at all on a successful removal -- the actually
destructive outcome. A caller elsewhere later hitting "worktree is
missing or not a valid git worktree" had no way to answer its own logged
advice ("find out what deleted it"), because nothing had ever recorded
that a removal happened, by what code path, or when.

Observed live: a feature's shared worktree vanished with zero trace in
either backend.log or monitor.log across the removal's actual timeframe --
every plausible caller (cleanup_all_stale_branches, discard_agent, orphan
reclaim) goes through this same choke point, and none of them left a
mark.
"""

from unittest.mock import patch

import pytest
from git import Repo

from src.core.worktree_removal import WorktreeRemover


@pytest.fixture
def main_repo(tmp_path):
    repo_dir = tmp_path / "main_repo"
    repo_dir.mkdir()
    repo = Repo.init(repo_dir)
    (repo_dir / "README.md").write_text("# Test\n")
    repo.index.add(["README.md"])
    repo.index.commit("Initial commit")
    return repo


def _make_worktree(main_repo, tmp_path, branch, dirty=False):
    worktree_path = tmp_path / "worktree"
    main_repo.git.branch(branch)
    main_repo.git.worktree("add", str(worktree_path), branch)
    if dirty:
        (worktree_path / "scratch.txt").write_text("uncommitted work\n")
    return worktree_path


class TestWorktreeRemovalLogging:
    def test_successful_removal_is_logged(self, tmp_path, main_repo):
        worktree = _make_worktree(main_repo, tmp_path, "feature/clean")

        with patch("src.core.worktree_removal.logger") as mock_logger:
            WorktreeRemover().remove(main_repo, str(worktree), require_clean=True)

        assert not worktree.exists()
        mock_logger.info.assert_called_once()
        message = mock_logger.info.call_args[0][0]
        assert str(worktree) in message
        assert mock_logger.info.call_args.kwargs.get("stack_info") is True

    def test_refused_removal_still_logs_only_the_refusal(self, tmp_path, main_repo):
        """Guard against regressing the existing refusal-logging behavior
        while adding the new success log -- a refused (dirty) removal
        must NOT also claim success."""
        worktree = _make_worktree(main_repo, tmp_path, "feature/dirty", dirty=True)

        with patch("src.core.worktree_removal.logger") as mock_logger:
            WorktreeRemover().remove(main_repo, str(worktree), require_clean=True)

        assert worktree.exists(), "dirty worktree must survive"
        mock_logger.info.assert_not_called()
        mock_logger.error.assert_called_once()

    def test_fallback_removal_path_is_also_logged(self, tmp_path, main_repo):
        """The shutil.rmtree+prune fallback (taken when `git worktree
        remove` itself raises) must log success too, not just the
        primary path."""
        worktree = _make_worktree(main_repo, tmp_path, "feature/fallback")

        from git import GitCommandError
        from git.cmd import Git

        # GitPython's Git.worktree is dynamic (__getattr__-dispatched on
        # the instance), which confuses unittest.mock.patch.object's
        # instance-level restore-on-exit logic -- patch the class's
        # __getattr__ instead so only the "worktree" command name is
        # intercepted, everything else (branch, rev_list, etc., used by
        # setup/teardown) still resolves normally.
        real_getattr = Git.__getattr__
        calls = []

        def fake_getattr(self, name):
            if name != "worktree":
                return real_getattr(self, name)

            def fake_worktree(*args, **kwargs):
                calls.append(args)
                if len(calls) == 1:
                    raise GitCommandError("worktree remove", 1)
                return real_getattr(self, name)(*args, **kwargs)

            return fake_worktree

        with patch.object(Git, "__getattr__", fake_getattr), patch(
            "src.core.worktree_removal.logger"
        ) as mock_logger:
            WorktreeRemover().remove(main_repo, str(worktree), require_clean=True)

        mock_logger.info.assert_called_once()
        message = mock_logger.info.call_args[0][0]
        assert "fallback" in message.lower()
