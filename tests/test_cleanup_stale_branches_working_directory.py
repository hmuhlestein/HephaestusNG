"""Regression test: cleanup_all_stale_branches must clear Workflow.working_directory
for any terminal workflow whose directory no longer exists on disk -- both
ones it just removed itself, and ones already gone via some other path
(e.g. _cleanup_worktree in orchestrator.py, run at each feature pipeline's
own completion) that have since dropped out of `git worktree list` entirely.

Found live: a completed feature's Workflow row kept pointing at an
already-deleted worktree path forever, silently breaking any later lookup
of that workflow's docs (_resolve_feature_docs_base trusted the stale path
instead of falling back to project_path).
"""

import shutil
import tempfile
import uuid
from pathlib import Path

import pytest
from git import Repo

from src.core.database import DatabaseManager, Workflow
from src.core.worktree_manager import WorktreeManager


@pytest.fixture
def temp_repo():
    temp_dir = tempfile.mkdtemp()
    repo = Repo.init(temp_dir)
    test_file = Path(temp_dir) / "README.md"
    test_file.write_text("# Test Repository\n")
    repo.index.add([str(test_file)])
    repo.index.commit("Initial commit")
    # The scripts/agent-safe-bin/git wrapper on PATH during CLI-agent test
    # runs blocks `git merge` unless a .hephaestus/review_approved marker
    # is found walking up from cwd -- this repo's own merges are the thing
    # under test here, not a review-gated feature landing, so pre-approve.
    marker_dir = Path(temp_dir) / ".hephaestus"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "review_approved").write_text("test fixture pre-approval\n")
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
    config.paths.worktree_base_path = Path(tempfile.mkdtemp())
    config.git.main_repo_path = Path(temp_repo.working_dir)
    config.git.base_branch = temp_repo.active_branch.name
    config.worktree_branch_prefix = "test-agent-"
    config.conflict_resolution_strategy = "newest_file_wins"
    config.prefer_child_on_tie = True
    config.auto_merge_enabled = True
    config.git.branch_retention_hours = {
        "merged": 1,
        "failed": 24,
        "abandoned": 6,
        "active": -1,
    }

    monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
    monkeypatch.setattr("src.core.worktree_manager.get_config", lambda: config)

    manager = WorktreeManager(test_db)
    yield manager
    shutil.rmtree(str(config.paths.worktree_base_path), ignore_errors=True)


def _add_worktree(temp_repo, base_path: Path, branch: str) -> Path:
    wt_path = base_path / f"wt_{branch.replace('/', '-')}"
    try:
        temp_repo.git.branch(branch)
    except Exception:
        pass
    temp_repo.git.worktree("add", str(wt_path), branch)
    return wt_path


class TestCleanupClearsWorkingDirectoryOfRemovedWorktree:
    def test_completed_workflow_working_directory_is_cleared(self, test_db, temp_repo, worktree_manager):
        import src.core.simple_config

        base_path = src.core.simple_config.get_config().paths.worktree_base_path
        wt_path = _add_worktree(temp_repo, base_path, "feature_architect/done-design")

        session = test_db.get_session()
        wf_id = f"wf-{uuid.uuid4().hex[:8]}"
        session.add(
            Workflow(
                id=wf_id,
                name="Phase 0",
                phases_folder_path="/tmp",
                status="completed",
                definition_id="feature_architect",
                working_directory=str(wt_path),
            )
        )
        session.commit()
        session.close()

        worktree_manager.cleanup_all_stale_branches()

        assert not wt_path.exists(), "worktree with no active/paused claimant should be removed"

        session = test_db.get_session()
        wf = session.query(Workflow).filter_by(id=wf_id).first()
        assert wf.working_directory is None, (
            "a completed workflow's working_directory must be cleared once "
            "its worktree is actually removed, so later lookups fall back to "
            "the project's real (merged) docs location instead of trusting "
            "a stale, now-nonexistent path"
        )
        session.close()

    def test_already_orphaned_workflow_working_directory_is_cleared(self, test_db, temp_repo, worktree_manager):
        """The worktree is already gone from disk and untracked by git (no
        `git worktree add` was ever run for it in this test) -- simulating a
        directory removed by some path other than this function's own Step 1
        loop, which only iterates entries `git worktree list` still reports."""
        import src.core.simple_config

        base_path = src.core.simple_config.get_config().paths.worktree_base_path
        already_gone_path = base_path / "wt_orphaned-design"
        assert not already_gone_path.exists()

        session = test_db.get_session()
        wf_id = f"wf-{uuid.uuid4().hex[:8]}"
        session.add(
            Workflow(
                id=wf_id,
                name="Phase 0",
                phases_folder_path="/tmp",
                status="completed",
                definition_id="feature_architect",
                working_directory=str(already_gone_path),
            )
        )
        session.commit()
        session.close()

        worktree_manager.cleanup_all_stale_branches()

        session = test_db.get_session()
        wf = session.query(Workflow).filter_by(id=wf_id).first()
        assert wf.working_directory is None, "a terminal workflow's working_directory must be cleared even when the directory was removed outside this function's own git-worktree-list-driven loop"
        session.close()

    def test_resurrected_stub_directory_working_directory_is_cleared(self, test_db, temp_repo, worktree_manager):
        """Observed live: something keeps writing to a removed worktree's
        .hephaestus/tmux/ path after `git worktree remove` deletes
        everything, resurrecting an empty parent directory with no `.git` --
        bare existence alone would wrongly treat this as still a real
        worktree."""
        import src.core.simple_config

        base_path = src.core.simple_config.get_config().paths.worktree_base_path
        stub_path = base_path / "wt_resurrected-stub"
        (stub_path / ".hephaestus" / "tmux").mkdir(parents=True)
        assert stub_path.exists()
        assert not (stub_path / ".git").exists()

        session = test_db.get_session()
        wf_id = f"wf-{uuid.uuid4().hex[:8]}"
        session.add(
            Workflow(
                id=wf_id,
                name="Phase 0",
                phases_folder_path="/tmp",
                status="completed",
                definition_id="feature_architect",
                working_directory=str(stub_path),
            )
        )
        session.commit()
        session.close()

        worktree_manager.cleanup_all_stale_branches()

        session = test_db.get_session()
        wf = session.query(Workflow).filter_by(id=wf_id).first()
        assert wf.working_directory is None, "an existing-but-not-a-real-worktree stub directory must not be trusted just because Path.exists() is True"
        session.close()

    def test_active_workflow_working_directory_survives(self, test_db, temp_repo, worktree_manager):
        """Sanity check the guard isn't overbroad: an active workflow's
        worktree survives cleanup entirely (covered elsewhere), so its
        working_directory must also survive untouched."""
        import src.core.simple_config

        base_path = src.core.simple_config.get_config().paths.worktree_base_path
        active_path = _add_worktree(temp_repo, base_path, "feature_architect/live-design")

        session = test_db.get_session()
        wf_id = f"wf-{uuid.uuid4().hex[:8]}"
        session.add(
            Workflow(
                id=wf_id,
                name="Phase 0",
                phases_folder_path="/tmp",
                status="active",
                definition_id="feature_architect",
                working_directory=str(active_path),
            )
        )
        session.commit()
        session.close()

        worktree_manager.cleanup_all_stale_branches()

        session = test_db.get_session()
        wf = session.query(Workflow).filter_by(id=wf_id).first()
        assert wf.working_directory == str(active_path)
        session.close()
