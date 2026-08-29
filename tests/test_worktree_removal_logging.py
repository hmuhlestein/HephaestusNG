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

from pathlib import Path
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
    # Matches every real Hephaestus-managed repo: .hephaestus/ is git-
    # excluded. Without this, a worktree with content only under
    # .hephaestus/tmux/ would wrongly register as "dirty" to this test
    # fixture's is_dirty(untracked_files=True) check -- unlike a real
    # repo, where git status never sees those files at all.
    (repo_dir / ".gitignore").write_text(".hephaestus/\n")
    repo.index.add(["README.md", ".gitignore"])
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


class TestWorktreeRemovalArchivesTmuxTranscripts:
    """Regression: WorktreeRemover.remove is the single choke point every
    worktree removal goes through (cleanup_all_stale_branches's periodic
    sweep, discard_agent/cleanup_worktree, AND the orchestrator's own
    end-of-pipeline _cleanup_worktree) -- but only the orchestrator path
    ever archived .hephaestus/tmux/ (agent .clean.log/.transcript.log)
    before deleting the worktree. Every other path destroyed it with no
    trace. .hephaestus/ is git-excluded, so it doesn't survive the merge
    like docs/*.md reports do -- once gone here, it's gone forever.
    Observed live: task 6debd5fa's agent lost its .clean.log/
    .transcript.log this way when its workflow failed and the periodic
    stale-worktree sweep reaped it via this exact method with no
    archival step at all."""

    def test_archives_tmux_dir_before_removing(self, tmp_path, main_repo):
        worktree = _make_worktree(main_repo, tmp_path, "feature/with-tmux")
        tmux_dir = worktree / ".hephaestus" / "tmux"
        tmux_dir.mkdir(parents=True)
        (tmux_dir / "agent_abc12345.clean.log").write_text("real transcript content\n")

        WorktreeRemover().remove(main_repo, str(worktree), require_clean=True)

        assert not worktree.exists()
        archived = Path(main_repo.working_dir) / ".hephaestus" / "tmux" / "agent_abc12345.clean.log"
        assert archived.exists()
        assert archived.read_text() == "real transcript content\n"

    def test_no_tmux_dir_is_a_silent_noop(self, tmp_path, main_repo):
        """A worktree with nothing to archive must not error or create an
        empty .hephaestus/tmux/ in the main repo."""
        worktree = _make_worktree(main_repo, tmp_path, "feature/no-tmux")

        WorktreeRemover().remove(main_repo, str(worktree), require_clean=True)

        assert not worktree.exists()
        assert not (Path(main_repo.working_dir) / ".hephaestus").exists()

    def test_archival_failure_does_not_block_removal(self, tmp_path, main_repo):
        """A broken/unreadable tmux dir must not prevent the worktree
        itself from still being removed -- losing the transcript is
        already the failure mode being guarded against; refusing to
        clean up the worktree too would make it worse, not better."""
        worktree = _make_worktree(main_repo, tmp_path, "feature/bad-tmux")
        tmux_dir = worktree / ".hephaestus" / "tmux"
        tmux_dir.mkdir(parents=True)
        (tmux_dir / "agent_abc12345.clean.log").write_text("content\n")

        with patch("src.core.worktree_removal.shutil.copy2", side_effect=OSError("disk full")):
            WorktreeRemover().remove(main_repo, str(worktree), require_clean=True)

        assert not worktree.exists()
