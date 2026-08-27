"""Regression tests for HEPHAESTUS_INSTALL_DIR / _resolve_install_dir.

HephaestusNG is self-hosting: its own autopilot checks out full git
worktrees of itself under .worktrees/ to work on features in isolation.
Every prior call site of "where is Hephaestus installed" computed it as a
Path(__file__).parent-chain, which resolves to whichever COPY of the
source tree is currently executing -- the worktree's own copy, when
invoked from inside one. Confirmed live: a backend process started with
cwd inside a linked worktree opened/created an empty hephaestus.db there
instead of the real one, and every /autopilot request it served showed
zero projects.

_resolve_install_dir fixes this via `git rev-parse --git-common-dir`,
which (unlike --show-toplevel) always resolves to the ONE shared repo
root regardless of which worktree it's run from.
"""

import subprocess

import pytest

from src.core.constants import _resolve_install_dir
from src.core.simple_config import HEPHAESTUS_INSTALL_DIR, PathsConfig


def _run_git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo_and_worktree(tmp_path):
    """A real throwaway git repo with one linked worktree, mirroring this
    project's own .worktrees/ layout."""
    repo = tmp_path / "main_repo"
    repo.mkdir()
    _run_git("init", cwd=repo)
    _run_git("config", "user.email", "test@test.com", cwd=repo)
    _run_git("config", "user.name", "test", cwd=repo)
    (repo / "README.md").write_text("# test\n")
    _run_git("add", "README.md", cwd=repo)
    _run_git("commit", "-m", "initial", cwd=repo)

    worktree = tmp_path / "worktrees" / "wt_feature"
    worktree.parent.mkdir(parents=True)
    _run_git("worktree", "add", "-b", "feature/x", str(worktree), cwd=repo)

    return repo, worktree


class TestResolveInstallDir:
    def test_resolves_to_main_repo_from_the_main_repo_itself(self, repo_and_worktree):
        repo, _worktree = repo_and_worktree
        assert _resolve_install_dir(anchor=repo) == repo

    def test_resolves_to_main_repo_from_inside_a_linked_worktree(self, repo_and_worktree):
        """The core regression: run the exact same resolution from inside
        the WORKTREE and it must still land on the main repo, not the
        worktree's own path."""
        repo, worktree = repo_and_worktree
        assert _resolve_install_dir(anchor=worktree) == repo
        assert _resolve_install_dir(anchor=worktree) != worktree

    def test_falls_back_to_parent_parent_outside_any_git_repo(self, tmp_path):
        """A packaged/pip-installed deployment with no .git present at
        all -- must not raise, must fall back to the old __file__-relative
        guess (anchor.parent.parent, matching every prior call site's
        pre-existing non-git behavior)."""
        stray_dir = tmp_path / "not_a_repo" / "src" / "core"
        stray_dir.mkdir(parents=True)
        assert _resolve_install_dir(anchor=stray_dir) == stray_dir.parent.parent


class TestPathsConfigDatabasePathAnchoring:
    def test_relative_database_path_anchors_to_install_dir_not_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)  # simulate a wrong-directory launch
        cfg = PathsConfig({"paths": {"database": "./hephaestus.db"}})
        assert cfg.database_path == HEPHAESTUS_INSTALL_DIR / "hephaestus.db"
        assert cfg.database_path != tmp_path / "hephaestus.db"

    def test_absolute_database_path_is_left_untouched(self, tmp_path):
        explicit = tmp_path / "custom" / "my.db"
        cfg = PathsConfig({"paths": {"database": str(explicit)}})
        assert cfg.database_path == explicit
