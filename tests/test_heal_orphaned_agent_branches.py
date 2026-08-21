"""Regression coverage for heal_orphaned_agent_branches.

Root incident: workflow 7938b3b5 reached "completed" while a goto-triggered
straggler security_review task was still being worked by a live agent in
its own per-agent worktree. sweep_completed_workflow_worktrees force-removed
that worktree (fixed separately, see test_sweep_completed_workflow_worktrees.py),
stranding the agent with a deleted cwd. Its real, already-committed fixes
sat on an orphaned branch (agent-<uuid>) that nothing ever merged, since the
task that owned it was never going to report "done" again. This is the
automated healer for exactly that leftover state: any branch matching the
configured agent branch_prefix, with no live `git worktree` checkout, gets
fast-forwarded into the base branch if it's a clean fast-forward -- anything
that isn't is left alone and just logged for manual review.
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from git import Repo

from src.core.database import AutopilotProject, DatabaseManager


@pytest.fixture
def temp_repo():
    temp_dir = tempfile.mkdtemp()
    repo = Repo.init(temp_dir)
    readme = Path(temp_dir) / "README.md"
    readme.write_text("# Test Repository\n")
    repo.index.add([str(readme)])
    repo.index.commit("Initial commit")
    # heal_orphaned_agent_branches only considers a configured base branch
    # by name -- make sure it's "main" regardless of the host's git default.
    if repo.active_branch.name != "main":
        repo.git.branch("-m", repo.active_branch.name, "main")
    yield repo
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def test_db(tmp_path):
    db_path = tmp_path / "test.db"
    manager = DatabaseManager(str(db_path))
    manager.create_tables()
    return manager


@pytest.fixture
def config(test_db, temp_repo, monkeypatch):
    """Point every get_config() lookup this module reaches at a test config.

    Patching only the definition site (src.core.simple_config.get_config) is
    not enough, and the difference is invisible when this file runs alone.
    worktree_integration.py binds the name at import time
    (`from src.core.simple_config import get_config`, line 23), so whether
    that binding is the real function or this lambda depends purely on
    whether the module was already imported when the patch was applied.

    Run this file alone and it is not: the import happens inside the test
    body, after the patch, so the module picks up the lambda and everything
    works. Run anything that imports src.mcp.server first (it pulls in
    worktree_integration at collection) and the binding is the real
    get_config -- which returns the memoized production Config, whose
    database_path is the real "hephaestus.db". heal_orphaned_agent_branches
    then enumerates the developer's REAL projects and attempts git
    fast-forwards in them, while this test sees healed == 0.

    Patch where the name is looked up, not only where it is defined -- the
    same rule AUTOPILOT_REFACTOR_PLAN.md 3.1 records for the
    test_phase0_idempotency.py bug.
    """
    import src.core.simple_config

    cfg = src.core.simple_config.Config()
    cfg.paths.database_path = test_db.engine.url.database
    cfg.git.base_branch = "main"
    cfg.git.branch_prefix = "agent-"
    monkeypatch.setattr("src.core.simple_config.get_config", lambda: cfg)

    import src.autopilot.orchestrator.worktree_integration as _wi

    monkeypatch.setattr(_wi, "get_config", lambda: cfg)
    return cfg


def _register_project(test_db, temp_repo):
    session = test_db.get_session()
    session.add(
        AutopilotProject(
            id="proj-test",
            name="Test Project",
            base_dir=temp_repo.working_dir,
        )
    )
    session.commit()
    session.close()


def _branch_names(temp_repo):
    return temp_repo.git.branch("--format=%(refname:short)").split("\n")


class TestHealOrphanedAgentBranches:
    def test_fast_forwards_orphaned_branch_with_no_live_worktree(
        self, test_db, temp_repo, config
    ):
        from src.autopilot.orchestrator.worktree_integration import heal_orphaned_agent_branches

        _register_project(test_db, temp_repo)

        wt_path = Path(tempfile.mkdtemp())
        temp_repo.git.branch("agent-abc123")
        temp_repo.git.worktree("add", str(wt_path), "agent-abc123")
        (wt_path / "fix.py").write_text("# real fix\n")
        wt_repo = Repo(wt_path)
        wt_repo.git.add("-A")
        wt_repo.git.commit("-m", "phase(security_review): fixed a real bug")
        branch_tip = wt_repo.head.commit.hexsha

        # Simulate the stranding: the worktree gets force-removed while the
        # branch (with the agent's real commit) survives.
        temp_repo.git.worktree("remove", str(wt_path), "--force")

        healed = heal_orphaned_agent_branches(MagicMock())

        assert healed == 1
        assert temp_repo.heads["main"].commit.hexsha == branch_tip
        # temp_repo's own working directory is checked out on "main" for
        # the whole test (the common case for a project's primary
        # checkout) -- a bare `update-ref` would move HEAD's target commit
        # forward without touching the working tree/index, leaving `git
        # status` showing the merged file as locally modified. Confirm the
        # working tree actually reflects the merge, not just the ref.
        assert (Path(temp_repo.working_dir) / "fix.py").read_text() == "# real fix\n"
        assert not temp_repo.is_dirty(untracked_files=True)

    def test_skips_healing_when_primary_checkout_of_base_branch_is_dirty(
        self, test_db, temp_repo, config
    ):
        """base_branch is checked out (as usual) in the project's primary
        directory, which has uncommitted changes sitting in it. Healing
        must not merge on top of that unattended -- skip and leave both
        the dirty change and the orphaned branch alone for manual review."""
        from src.autopilot.orchestrator.worktree_integration import heal_orphaned_agent_branches

        _register_project(test_db, temp_repo)

        wt_path = Path(tempfile.mkdtemp())
        temp_repo.git.branch("agent-def456")
        temp_repo.git.worktree("add", str(wt_path), "agent-def456")
        (wt_path / "fix.py").write_text("# real fix\n")
        wt_repo = Repo(wt_path)
        wt_repo.git.add("-A")
        wt_repo.git.commit("-m", "phase(security_review): fixed a real bug")
        temp_repo.git.worktree("remove", str(wt_path), "--force")

        # Uncommitted, unrelated change sitting in the primary checkout.
        (Path(temp_repo.working_dir) / "README.md").write_text("# dirty\n")
        main_tip_before = temp_repo.heads["main"].commit.hexsha

        healed = heal_orphaned_agent_branches(MagicMock())

        assert healed == 0
        assert temp_repo.heads["main"].commit.hexsha == main_tip_before
        assert (Path(temp_repo.working_dir) / "README.md").read_text() == "# dirty\n"
        assert "agent-def456" in _branch_names(temp_repo)

    def test_does_not_touch_branch_still_checked_out_in_a_live_worktree(
        self, test_db, temp_repo, config
    ):
        from src.autopilot.orchestrator.worktree_integration import heal_orphaned_agent_branches

        _register_project(test_db, temp_repo)

        wt_path = Path(tempfile.mkdtemp())
        try:
            temp_repo.git.branch("agent-still-working")
            temp_repo.git.worktree("add", str(wt_path), "agent-still-working")
            (wt_path / "fix.py").write_text("# in-progress fix\n")
            wt_repo = Repo(wt_path)
            wt_repo.git.add("-A")
            wt_repo.git.commit("-m", "wip")
            main_tip_before = temp_repo.heads["main"].commit.hexsha

            healed = heal_orphaned_agent_branches(MagicMock())

            assert healed == 0
            assert temp_repo.heads["main"].commit.hexsha == main_tip_before
            assert "agent-still-working" in _branch_names(temp_repo)
        finally:
            shutil.rmtree(wt_path, ignore_errors=True)

    def test_does_not_merge_a_diverged_branch(self, test_db, temp_repo, config):
        """base_branch moved on independently after the orphaned branch
        forked from it -- no longer a clean fast-forward. Must not attempt
        any merge unattended; just leave it for manual review."""
        from src.autopilot.orchestrator.worktree_integration import heal_orphaned_agent_branches

        _register_project(test_db, temp_repo)

        wt_path = Path(tempfile.mkdtemp())
        temp_repo.git.branch("agent-diverged")
        temp_repo.git.worktree("add", str(wt_path), "agent-diverged")
        (wt_path / "fix.py").write_text("# orphaned fix\n")
        wt_repo = Repo(wt_path)
        wt_repo.git.add("-A")
        wt_repo.git.commit("-m", "orphaned work")
        temp_repo.git.worktree("remove", str(wt_path), "--force")

        # main advances independently of the orphaned branch.
        other = Path(temp_repo.working_dir) / "unrelated.py"
        other.write_text("# unrelated change on main\n")
        temp_repo.index.add([str(other)])
        temp_repo.index.commit("unrelated work on main")
        main_tip_before = temp_repo.heads["main"].commit.hexsha

        healed = heal_orphaned_agent_branches(MagicMock())

        assert healed == 0
        assert temp_repo.heads["main"].commit.hexsha == main_tip_before
        assert "agent-diverged" in _branch_names(temp_repo)

    def test_ignores_branches_not_matching_the_agent_prefix(
        self, test_db, temp_repo, config
    ):
        from src.autopilot.orchestrator.worktree_integration import heal_orphaned_agent_branches

        _register_project(test_db, temp_repo)

        wt_path = Path(tempfile.mkdtemp())
        temp_repo.git.branch("feature/unrelated")
        temp_repo.git.worktree("add", str(wt_path), "feature/unrelated")
        (wt_path / "fix.py").write_text("# not an agent branch\n")
        wt_repo = Repo(wt_path)
        wt_repo.git.add("-A")
        wt_repo.git.commit("-m", "some feature work")
        temp_repo.git.worktree("remove", str(wt_path), "--force")
        main_tip_before = temp_repo.heads["main"].commit.hexsha

        healed = heal_orphaned_agent_branches(MagicMock())

        assert healed == 0
        assert temp_repo.heads["main"].commit.hexsha == main_tip_before

    def test_ignores_branch_fully_caught_up_with_base(self, test_db, temp_repo, config):
        from src.autopilot.orchestrator.worktree_integration import heal_orphaned_agent_branches

        _register_project(test_db, temp_repo)

        # A branch created but never advanced past base -- nothing to heal.
        temp_repo.git.branch("agent-empty")

        healed = heal_orphaned_agent_branches(MagicMock())

        assert healed == 0
        assert "agent-empty" in _branch_names(temp_repo)
