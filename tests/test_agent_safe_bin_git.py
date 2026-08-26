"""Tests for scripts/agent-safe-bin/git — the protected `git` wrapper CLI
agent sessions get on PATH (see AGENT_SAFE_BIN_DIR in
src/interfaces/cli_interface.py). Blocks landing work on a protected
branch (main/master) via `git merge` or `git push` until
.hephaestus/review_approved exists.

Known separate gap, not covered/fixed here: the push handler's "no
explicit refspec" fallback re-checks the CURRENT branch even when an
explicit, already-cleared refspec was given (its `if [[ -z
"$blocked_reason" ]]` doesn't actually verify no refspec was present) --
`git push origin feature` while sitting on `main` locally gets
incorrectly blocked, even though the pushed branch itself is safe.
"""

import subprocess
from pathlib import Path

import pytest


GIT_SCRIPT = str(
    Path(__file__).parent.parent / "scripts" / "agent-safe-bin" / "git"
)


def _run_git(args, cwd):
    """Run the wrapper with the real `git` resolved normally via PATH, but
    the wrapper script itself invoked directly (not via PATH lookup) so
    the test doesn't depend on install location."""
    return subprocess.run(
        [GIT_SCRIPT] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A real git repo with two branches (main, feature) each holding a
    distinct commit, so `git merge feature` on either branch has real
    work to merge -- not just a no-op fast-forward of nothing. Includes a
    .hephaestus/ dir so the wrapper recognizes it as a Hephaestus-managed
    repo (see hephaestus_root in the script) -- without one, the wrapper
    is a no-op for every subcommand, by design (see
    TestAgentSafeBinGitUnmanagedRepo)."""
    (tmp_path / ".hephaestus").mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "main.txt").write_text("main\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "main commit"], cwd=tmp_path, check=True)

    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=tmp_path, check=True)
    (tmp_path / "feature.txt").write_text("feature\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature commit"], cwd=tmp_path, check=True)

    subprocess.run(["git", "checkout", "-q", "main"], cwd=tmp_path, check=True)
    return tmp_path


class TestAgentSafeBinGitMerge:
    def test_blocks_merge_into_main(self, repo):
        """The original, correct protection: merging INTO main itself
        must still be blocked."""
        result = _run_git(["merge", "feature"], cwd=repo)
        assert result.returncode != 0
        assert "BLOCKED" in result.stderr
        assert not (repo / "feature.txt").exists()

    def test_blocks_merge_into_master(self, repo):
        subprocess.run(["git", "branch", "-m", "main", "master"], cwd=repo, check=True)
        result = _run_git(["merge", "feature"], cwd=repo)
        assert result.returncode != 0
        assert "BLOCKED" in result.stderr

    def test_allows_merge_into_non_protected_branch(self, repo):
        """Regression for ticket-d6148c43: `git merge` used to be blocked
        unconditionally regardless of the CURRENT branch, breaking any
        test (or real workflow) that merges branches unrelated to this
        repo's own main/master -- e.g. a disposable fixture repo's own
        feature branches, or a legitimate merge on a working branch that
        was never headed for main directly."""
        subprocess.run(["git", "checkout", "-q", "-b", "wip", "main"], cwd=repo, check=True)
        result = _run_git(["merge", "feature"], cwd=repo)
        assert result.returncode == 0, result.stderr
        assert (repo / "feature.txt").exists()

    def test_allows_merge_into_main_when_approved(self, repo):
        (repo / ".hephaestus" / "review_approved").touch()
        result = _run_git(["merge", "feature"], cwd=repo)
        assert result.returncode == 0, result.stderr
        assert (repo / "feature.txt").exists()


class TestAgentSafeBinGitUnmanagedRepo:
    """Regression for ticket-d6148c43: a git repo with no .hephaestus/
    ancestor at all -- e.g. a disposable fixture repo a test spins up
    under a pytest tmp dir to exercise real git-merge behavior -- was
    never "this feature's repo" and must never be gated, regardless of
    branch name or subcommand."""

    @pytest.fixture
    def unmanaged_repo(self, tmp_path):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        (tmp_path / "main.txt").write_text("main\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "main commit"], cwd=tmp_path, check=True)

        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=tmp_path, check=True)
        (tmp_path / "feature.txt").write_text("feature\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "feature commit"], cwd=tmp_path, check=True)

        subprocess.run(["git", "checkout", "-q", "main"], cwd=tmp_path, check=True)
        return tmp_path

    def test_allows_merge_into_main_with_no_hephaestus_ancestor(self, unmanaged_repo):
        result = _run_git(["merge", "feature"], cwd=unmanaged_repo)
        assert result.returncode == 0, result.stderr
        assert (unmanaged_repo / "feature.txt").exists()

    def test_stray_hephaestus_dir_several_levels_up_does_not_leak_in(self, tmp_path):
        """A genuine incident, not hypothetical: a stray .hephaestus/ dir
        sitting several levels above a disposable fixture repo (e.g. a
        shared pytest-of-<user> tmp root some OTHER, unrelated fixture
        left a .hephaestus/ scratch dir in) must not make the wrapper
        treat that fixture repo as Hephaestus-managed. The check is
        bounded to the git repo's own toplevel (`git rev-parse
        --show-toplevel`), not an unbounded walk up $PWD's ancestors."""
        (tmp_path / ".hephaestus").mkdir()  # stray, several levels up
        repo_dir = tmp_path / "nested" / "deeper" / "fixture_repo"
        repo_dir.mkdir(parents=True)

        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
        (repo_dir / "main.txt").write_text("main\n")
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "main commit"], cwd=repo_dir, check=True)
        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo_dir, check=True)
        (repo_dir / "feature.txt").write_text("feature\n")
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "feature commit"], cwd=repo_dir, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo_dir, check=True)

        result = _run_git(["merge", "feature"], cwd=repo_dir)

        assert result.returncode == 0, result.stderr
        assert (repo_dir / "feature.txt").exists()

    def test_allows_push_targeting_main_with_no_hephaestus_ancestor(
        self, unmanaged_repo, tmp_path_factory
    ):
        remote = tmp_path_factory.mktemp("remote")
        subprocess.run(["git", "init", "-q", "--bare"], cwd=remote, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)], cwd=unmanaged_repo, check=True
        )

        result = _run_git(["push", "origin", "main"], cwd=unmanaged_repo)

        assert result.returncode == 0, result.stderr


class TestAgentSafeBinGitPush:
    def test_blocks_push_targeting_main(self, repo, tmp_path_factory):
        remote = tmp_path_factory.mktemp("remote")
        subprocess.run(["git", "init", "-q", "--bare"], cwd=remote, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)

        result = _run_git(["push", "origin", "main"], cwd=repo)

        assert result.returncode != 0
        assert "BLOCKED" in result.stderr

    def test_allows_push_of_non_protected_branch(self, repo, tmp_path_factory):
        remote = tmp_path_factory.mktemp("remote")
        subprocess.run(["git", "init", "-q", "--bare"], cwd=remote, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
        # Current branch matters here too, separately from the pushed
        # branch -- see the module docstring's note on the no-refspec
        # fallback re-checking current_branch even when an explicit,
        # already-safe target was given. Check out the non-protected
        # branch to isolate this test to what it's actually regression-
        # testing (pushing a safe branch), not that separate gap.
        subprocess.run(["git", "checkout", "-q", "feature"], cwd=repo, check=True)

        result = _run_git(["push", "origin", "feature"], cwd=repo)

        assert result.returncode == 0, result.stderr
