"""Regression test: cleanup_all_stale_branches must not remove a worktree
that's still claimed by an active workflow.

Found live: /autopilot/queue/rerun fires this cleanup from a background
thread at the same moment a brand-new orchestrator process starts and
creates a fresh Phase 0 worktree at a deterministic, design-derived path
(same path reused across every retry of the same design). Step 1 used to
remove every linked worktree unconditionally ("stale" was never actually
verified), so it could -- and did -- delete the brand-new worktree the new
run had just created, moments after Rerun was clicked. The Feature Architect
agent completed successfully, but its worktree vanished ~16s later and the
whole design got marked "failed" even though nothing had actually gone
wrong.
"""

import shutil
import tempfile
import uuid
from pathlib import Path

import pytest
from git import Repo

from src.core.database import AgentBranch, DatabaseManager, Workflow
from src.core.worktree_manager import WorktreeManager


@pytest.fixture
def temp_repo():
    temp_dir = tempfile.mkdtemp()
    repo = Repo.init(temp_dir)
    test_file = Path(temp_dir) / "README.md"
    test_file.write_text("# Test Repository\n")
    repo.index.add([str(test_file)])
    repo.index.commit("Initial commit")
    yield repo
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def test_db():
    db_manager = DatabaseManager(":memory:")
    db_manager.create_tables()
    return db_manager


@pytest.fixture
def worktree_manager(test_db, temp_repo, monkeypatch):
    import src.core.simple_config

    config = src.core.simple_config.Config()
    config.worktree_base_path = Path(tempfile.mkdtemp())
    config.main_repo_path = Path(temp_repo.working_dir)
    config.base_branch = temp_repo.active_branch.name
    config.worktree_branch_prefix = "test-agent-"
    config.conflict_resolution_strategy = "newest_file_wins"
    config.prefer_child_on_tie = True
    config.auto_merge_enabled = True
    config.branch_retention_hours = {
        "merged": 1,
        "failed": 24,
        "abandoned": 6,
        "active": -1,
    }

    monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
    monkeypatch.setattr("src.core.worktree_manager.get_config", lambda: config)

    manager = WorktreeManager(test_db)
    yield manager
    shutil.rmtree(str(config.worktree_base_path), ignore_errors=True)


def _add_worktree(temp_repo, base_path: Path, branch: str) -> Path:
    wt_path = base_path / f"wt_{branch.replace('/', '-')}"
    try:
        temp_repo.git.branch(branch)
    except Exception:
        pass
    temp_repo.git.worktree("add", str(wt_path), branch)
    return wt_path


class TestCleanupDoesNotRemoveActiveWorktree:
    def test_active_workflows_worktree_survives_cleanup(
        self, test_db, temp_repo, worktree_manager
    ):
        import src.core.simple_config

        base_path = src.core.simple_config.get_config().worktree_base_path

        # An old, genuinely-stale worktree with no tracking Workflow row.
        stale_path = _add_worktree(temp_repo, base_path, "autopilot-phase0/old-design")

        # A worktree that a currently-active workflow has just started using
        # -- the scenario that raced against Rerun.
        active_path = _add_worktree(temp_repo, base_path, "autopilot-phase0/live-design")
        session = test_db.get_session()
        session.add(
            Workflow(
                id=f"wf-{uuid.uuid4().hex[:8]}",
                name="Phase 0",
                phases_folder_path="/tmp",
                status="active",
                definition_id="autopilot-phase0",
                working_directory=str(active_path),
            )
        )
        session.commit()
        session.close()

        result = worktree_manager.cleanup_all_stale_branches()

        assert not stale_path.exists(), "genuinely stale worktree should still be cleaned up"
        assert active_path.exists(), "worktree claimed by an active workflow must survive cleanup"
        assert result["worktrees_cleaned"] >= 1

        # Protecting the worktree directory isn't enough on its own: git merge
        # doesn't require a branch to be un-checked-out anywhere, so Step 2
        # could still merge the live branch into main and force-delete it out
        # from under the active worktree even after Step 1 stopped removing
        # the directory. Confirm the branch itself also survived untouched.
        branch_names = Repo(temp_repo.working_dir).git.branch(
            "--format=%(refname:short)"
        ).split("\n")
        assert "autopilot-phase0/live-design" in branch_names
        assert "autopilot-phase0/old-design" not in branch_names

    def test_agent_branch_still_in_progress_is_not_merged_and_deleted(
        self, test_db, temp_repo, worktree_manager
    ):
        """AgentBranch.merge_status == 'active' only means 'not yet merged',
        the state for every currently-in-progress agent's branch -- not
        'the agent is done'. Without protection, any agent still genuinely
        working when this cleanup runs would have its branch merged into
        main and deleted mid-task."""
        import src.core.simple_config

        base_path = src.core.simple_config.get_config().worktree_base_path
        working_path = _add_worktree(temp_repo, base_path, "agent-still-working")

        session = test_db.get_session()
        session.add(
            Workflow(
                id=f"wf-{uuid.uuid4().hex[:8]}",
                name="autopilot",
                phases_folder_path="/tmp",
                status="active",
                definition_id="autopilot",
                working_directory=str(working_path),
            )
        )
        session.add(
            AgentBranch(
                agent_id=f"agent-{uuid.uuid4().hex[:8]}",
                worktree_path=str(working_path),
                branch_name="agent-still-working",
                parent_commit_sha=temp_repo.head.commit.hexsha,
                base_commit_sha=temp_repo.head.commit.hexsha,
                merge_status="active",
            )
        )
        session.commit()
        session.close()

        worktree_manager.cleanup_all_stale_branches()

        branch_names = Repo(temp_repo.working_dir).git.branch(
            "--format=%(refname:short)"
        ).split("\n")
        assert "agent-still-working" in branch_names, (
            "an agent's branch still marked 'active' must survive cleanup "
            "while its worktree belongs to an active workflow"
        )
        assert working_path.exists()
