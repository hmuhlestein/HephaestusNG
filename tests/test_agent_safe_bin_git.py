"""Tests for scripts/agent-safe-bin/git — the protected `git` wrapper CLI
agent sessions get on PATH. Blocks `git merge`/`git push` from landing a
feature's work on a protected branch (main, master) before human review
approval (signaled by a .hephaestus/review_approved marker file).
"""

import subprocess
import tempfile
from pathlib import Path

GIT_SCRIPT = str(
    Path(__file__).parent.parent / "scripts" / "agent-safe-bin" / "git"
)


def _git(args, cwd):
    """Run the wrapper script directly (not via PATH lookup), so the test
    doesn't depend on install location or risk the wrapper resolving
    itself as "real_git" if another copy were also on PATH."""
    return subprocess.run(
        [GIT_SCRIPT] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _init_repo_with_two_branches(root, initial_branch="main", feature_branch="feature"):
    """<root> gets an initial commit on <initial_branch>, then a second
    commit on <feature_branch> branched off it. Leaves HEAD checked out
    on <initial_branch>, positioned for a `git merge <feature_branch>`."""
    subprocess.run(["git", "init", "-q", "-b", initial_branch, str(root)], check=True)
    (root / "f.txt").write_text("one\n")
    subprocess.run(["git", "add", "f.txt"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "init"],
        cwd=root, check=True,
    )
    subprocess.run(["git", "checkout", "-q", "-b", feature_branch], cwd=root, check=True)
    (root / "f.txt").write_text("one\ntwo\n")
    subprocess.run(["git", "add", "f.txt"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "feat"],
        cwd=root, check=True,
    )
    subprocess.run(["git", "checkout", "-q", initial_branch], cwd=root, check=True)


class TestMergeIntoProtectedBranch:
    def test_blocks_merge_into_main_when_hephaestus_project(self, tmp_path):
        """Adversarial review / QA gate: this is the guardrail's actual
        job -- landing a feature's work on main before approval."""
        (tmp_path / ".hephaestus").mkdir()
        _init_repo_with_two_branches(tmp_path, initial_branch="main")

        result = _git(["merge", "--no-ff", "-m", "merge", "feature"], cwd=tmp_path)

        assert result.returncode != 0
        assert "BLOCKED" in result.stderr

    def test_allows_merge_into_main_after_approval(self, tmp_path):
        heph = tmp_path / ".hephaestus"
        heph.mkdir()
        (heph / "review_approved").write_text("approved")
        _init_repo_with_two_branches(tmp_path, initial_branch="main")

        result = _git(["merge", "--no-ff", "-m", "merge", "feature"], cwd=tmp_path)

        assert result.returncode == 0

    def test_allows_merge_into_non_protected_branch(self, tmp_path):
        """Regression: the wrapper used to block ANY `git merge`
        unconditionally -- merging one throwaway branch into another
        (e.g. an agent's child branch into its parent's) has nothing to
        do with landing on main and must pass through."""
        (tmp_path / ".hephaestus").mkdir()
        _init_repo_with_two_branches(
            tmp_path, initial_branch="agent-parent", feature_branch="agent-child"
        )

        result = _git(["merge", "--no-ff", "-m", "merge", "agent-child"], cwd=tmp_path)

        assert result.returncode == 0

    def test_allows_merge_into_main_outside_hephaestus_project(self):
        """Regression: a repo with no .hephaestus/ anywhere above it isn't
        a Hephaestus-managed project (e.g. a scratch git repo a test
        creates under /tmp) -- git's own default branch is commonly
        named "main" too, so branch-name matching alone produced false
        positives on every such repo. No .hephaestus/ here at all.

        Uses tempfile.mkdtemp() directly rather than the tmp_path fixture:
        pytest's shared temp base (tmp_path_factory.getbasetemp()'s
        parent) can itself accumulate an unrelated .hephaestus/ dir from
        another test's fixture bug, which would falsely satisfy the
        "found .hephaestus above cwd" check this test exists to rule out.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _init_repo_with_two_branches(root, initial_branch="main")

            result = _git(["merge", "--no-ff", "-m", "merge", "feature"], cwd=root)

            assert result.returncode == 0


class TestPushToProtectedBranch:
    def test_blocks_push_of_current_main_branch(self, tmp_path):
        (tmp_path / ".hephaestus").mkdir()
        remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        (repo / "f.txt").write_text("one\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "init"],
            cwd=repo, check=True,
        )
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)

        result = _git(["push", "origin", "main"], cwd=repo)

        assert result.returncode != 0
        assert "BLOCKED" in result.stderr


class TestScriptExecutable:
    def test_script_is_executable(self):
        assert Path(GIT_SCRIPT).stat().st_mode & 0o111
