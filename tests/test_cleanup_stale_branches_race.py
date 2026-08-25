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

from src.core.database import Agent, AgentBranch, DatabaseManager, Workflow
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


class TestCleanupDoesNotRemoveActiveWorktree:
    def test_active_workflows_worktree_survives_cleanup(
        self, test_db, temp_repo, worktree_manager
    ):
        import src.core.simple_config

        base_path = src.core.simple_config.get_config().paths.worktree_base_path

        # An old, genuinely-stale worktree with no tracking Workflow row.
        stale_path = _add_worktree(temp_repo, base_path, "feature_architect/old-design")

        # A worktree that a currently-active workflow has just started using
        # -- the scenario that raced against Rerun.
        active_path = _add_worktree(temp_repo, base_path, "feature_architect/live-design")
        session = test_db.get_session()
        session.add(
            Workflow(
                id=f"wf-{uuid.uuid4().hex[:8]}",
                name="Phase 0",
                phases_folder_path="/tmp",
                status="active",
                definition_id="feature_architect",
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
        assert "feature_architect/live-design" in branch_names
        assert "feature_architect/old-design" not in branch_names

    def test_worktree_with_uncommitted_changes_survives_cleanup_even_if_workflow_is_failed(
        self, test_db, temp_repo, worktree_manager
    ):
        """Regression, observed live: a workflow can be wrongly marked
        "failed" by an unrelated self-heal (e.g. "abandoned: no activity"
        firing because the *backend itself* crashed and stopped recording
        activity, not because the agent actually stopped working) while an
        agent is still genuinely mid-task with real, uncommitted fixes
        sitting in its worktree. Trusting Workflow.status alone here let
        this exact sweep delete a security_review agent's uncommitted fixes
        (a written report, several source file changes) permanently --
        every phase already commits its own work as a matter of course, so
        a worktree that's actually done has nothing uncommitted left to
        lose; this one did, and must survive regardless of what the
        workflow's DB status says."""
        import src.core.simple_config

        base_path = src.core.simple_config.get_config().paths.worktree_base_path

        dirty_path = _add_worktree(temp_repo, base_path, "feature_architect/mid-task")
        (dirty_path / "uncommitted_fix.py").write_text("# real, unsaved work\n")

        session = test_db.get_session()
        session.add(
            Workflow(
                id=f"wf-{uuid.uuid4().hex[:8]}",
                name="Phase 0",
                phases_folder_path="/tmp",
                status="failed",
                definition_id="feature_architect",
                working_directory=str(dirty_path),
            )
        )
        session.commit()
        session.close()

        worktree_manager.cleanup_all_stale_branches()

        assert dirty_path.exists(), "a worktree with uncommitted changes must never be deleted"
        assert (dirty_path / "uncommitted_fix.py").exists(), "the uncommitted work itself must survive"

    def test_paused_workflows_worktree_survives_cleanup(
        self, test_db, temp_repo, worktree_manager
    ):
        """Found on adversarial review: pause_feature (autopilot_api.py) sets
        a workflow to 'paused' while deliberately keeping working_directory
        intact so _resume_interrupted_workflows can restart the agent on its
        'existing worktree branch' later. Only protecting 'active' workflows
        left every paused one exposed to the identical worktree-deletion /
        branch-merge-and-delete race this whole fix exists to close."""
        import src.core.simple_config

        base_path = src.core.simple_config.get_config().paths.worktree_base_path
        paused_path = _add_worktree(temp_repo, base_path, "feature_architect/paused-design")

        session = test_db.get_session()
        session.add(
            Workflow(
                id=f"wf-{uuid.uuid4().hex[:8]}",
                name="Phase 0",
                phases_folder_path="/tmp",
                status="paused",
                definition_id="feature_architect",
                working_directory=str(paused_path),
            )
        )
        session.add(
            AgentBranch(
                agent_id=f"agent-{uuid.uuid4().hex[:8]}",
                worktree_path=str(paused_path),
                branch_name="feature_architect/paused-design",
                parent_commit_sha=temp_repo.head.commit.hexsha,
                base_commit_sha=temp_repo.head.commit.hexsha,
                merge_status="active",
            )
        )
        session.commit()
        session.close()

        worktree_manager.cleanup_all_stale_branches()

        assert paused_path.exists(), "worktree claimed by a paused (resumable) workflow must survive cleanup"
        branch_names = Repo(temp_repo.working_dir).git.branch(
            "--format=%(refname:short)"
        ).split("\n")
        assert "feature_architect/paused-design" in branch_names

    def test_agent_branch_still_in_progress_is_not_merged_and_deleted(
        self, test_db, temp_repo, worktree_manager
    ):
        """AgentBranch.merge_status == 'active' only means 'not yet merged',
        the state for every currently-in-progress agent's branch -- not
        'the agent is done'. Without protection, any agent still genuinely
        working when this cleanup runs would have its branch merged into
        main and deleted mid-task."""
        import src.core.simple_config

        base_path = src.core.simple_config.get_config().paths.worktree_base_path
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


class TestCleanupDoesNotRemoveActiveAgentWorktree:
    """Workflow.working_directory only covers the shared-feature-worktree
    model -- the legacy isolated-per-agent worktree (AgentBranch,
    create_agent_worktree, used by validators/diagnostic agents) isn't
    tied to any Workflow row at all, so it had zero protection here. A
    worktree freshly created for a still-alive agent that hasn't written
    anything yet is genuinely clean (nothing dirty to trip
    _remove_worktree's require_clean guard), so without this it would be
    silently removed out from under an agent about to start working in
    it. Not a lost-work bug on its own (nothing was written yet), but the
    agent's own tmux session then points at a directory that no longer
    exists. This became materially more likely once cleanup_all_stale_
    branches started running on a periodic sweep instead of only rare
    manual/rerun triggers."""

    def test_still_alive_agents_fresh_worktree_survives_cleanup(
        self, test_db, temp_repo, worktree_manager
    ):
        import src.core.simple_config

        base_path = src.core.simple_config.get_config().paths.worktree_base_path

        # An old, genuinely-stale agent worktree -- its agent is terminated.
        stale_path = _add_worktree(temp_repo, base_path, "agent-dead-agent")
        # A brand-new, still-clean worktree for an agent that's still alive
        # (working) -- the race this guard exists to fix.
        fresh_path = _add_worktree(temp_repo, base_path, "agent-still-alive")

        session = test_db.get_session()
        session.add(
            Agent(
                id="dead-agent", system_prompt="p", status="terminated",
                cli_type="pi", tmux_session_name="tmux-dead",
            )
        )
        session.add(
            AgentBranch(
                agent_id="dead-agent", worktree_path=str(stale_path),
                branch_name="agent-dead-agent", parent_commit_sha="abc123",
                base_commit_sha="abc123", merge_status="active",
            )
        )
        session.add(
            Agent(
                id="still-alive", system_prompt="p", status="working",
                cli_type="pi", tmux_session_name="tmux-alive",
            )
        )
        session.add(
            AgentBranch(
                agent_id="still-alive", worktree_path=str(fresh_path),
                branch_name="agent-still-alive", parent_commit_sha="abc123",
                base_commit_sha="abc123", merge_status="active",
            )
        )
        session.commit()
        session.close()

        result = worktree_manager.cleanup_all_stale_branches()

        assert not stale_path.exists(), "a dead agent's worktree should still be cleaned up"
        assert fresh_path.exists(), "a still-alive agent's worktree must survive cleanup"
        assert result["worktrees_cleaned"] >= 1

        branch_names = Repo(temp_repo.working_dir).git.branch(
            "--format=%(refname:short)"
        ).split("\n")
        assert "agent-still-alive" in branch_names, (
            "an alive agent's branch must survive cleanup while its worktree "
            "is protected"
        )


class TestCleanupHandlesLegacyBranchPrefix:
    """config/workflows/autopilot-phase0/ was renamed to
    config/workflows/feature_architect/, so new Phase 0 branches are named
    "feature_architect/<design_id>" instead of "autopilot-phase0/<design_id>".
    cleanup_all_stale_branches' prefix filter (src/core/worktree_manager.py)
    keeps the old "autopilot-" prefix specifically so real branches created
    before the rename shipped on an already-deployed system still get swept
    up -- this test exercises that legacy path directly, since the rename's
    mechanical test-fixture updates left nothing else creating an
    "autopilot-*" branch anymore."""

    def test_legacy_autopilot_prefixed_branch_still_cleaned_up(
        self, test_db, temp_repo, worktree_manager
    ):
        import src.core.simple_config

        base_path = src.core.simple_config.get_config().paths.worktree_base_path
        legacy_path = _add_worktree(
            temp_repo, base_path, "autopilot-phase0/pre-rename-design"
        )

        worktree_manager.cleanup_all_stale_branches()

        assert not legacy_path.exists(), (
            "a genuinely stale worktree using the pre-rename branch prefix "
            "must still be cleaned up"
        )
        branch_names = Repo(temp_repo.working_dir).git.branch(
            "--format=%(refname:short)"
        ).split("\n")
        assert "autopilot-phase0/pre-rename-design" not in branch_names


class TestCleanupNeverTouchesMainRepo:
    """Critical regression test: git worktree list --porcelain reports the
    *resolved* path for every entry, including the main repo's own, while
    main_repo.working_dir is whatever unresolved form the Repo object was
    opened with. On any system where the repo path involves a symlink
    (guaranteed on macOS: /var -> /private/var), a raw string comparison
    between the two never matches -- so the "skip the main repo" guard
    silently never worked, and this function would try to `git worktree
    remove` the main project repository itself, falling back to a raw
    shutil.rmtree on failure. Confirmed live via direct reproduction.
    """

    def test_main_repo_directory_and_git_history_survive(
        self, test_db, temp_repo, worktree_manager
    ):
        import src.core.simple_config

        # Sanity-check the assumption this bug depended on: tempfile.mkdtemp()
        # paths on macOS resolve through /var -> /private/var, so raw and
        # resolved forms of the same path differ.
        main_repo_path = Path(temp_repo.working_dir)
        if str(main_repo_path) == str(main_repo_path.resolve()):
            pytest.skip("main repo path has no symlink component on this platform")

        base_path = src.core.simple_config.get_config().paths.worktree_base_path
        _add_worktree(temp_repo, base_path, "feature_architect/some-design")

        worktree_manager.cleanup_all_stale_branches()

        assert main_repo_path.exists(), "main repo directory must survive cleanup"
        assert (main_repo_path / ".git").exists(), "main repo's .git must survive cleanup"
        # A real commit from before cleanup must still be reachable.
        assert temp_repo.head.commit.message.strip() == "Initial commit"

    def test_remove_worktree_refuses_main_repo_path_directly(
        self, test_db, temp_repo, worktree_manager
    ):
        """Defense-in-depth: _remove_worktree itself must refuse to touch the
        main repo, independent of whether any caller's own path-matching
        logic correctly identified it as such. This is the sole choke point
        every removal path goes through -- a hard guard here means a future
        bug in a *caller's* comparison (like the one this test class's
        sibling test found) can't reach shutil.rmtree on the main repo again."""
        worktree_manager._remove_worktree(str(Path(temp_repo.working_dir).resolve()))

        assert Path(temp_repo.working_dir).exists()
        assert (Path(temp_repo.working_dir) / ".git").exists()
        assert temp_repo.head.commit.message.strip() == "Initial commit"


class TestMergeSharedBranch:
    """Characterization tests for merge_shared_branch — the single merge
    primitive for all worktree cleanup paths. These tests verify the
    current behavior BEFORE any strategy change, so the conflict-resolution
    decision is provably a decision, not an accident.
    """

    def test_merges_clean_branch(self, temp_repo, db_manager):
        """A branch with no conflicts merges successfully."""
        wt_mgr = WorktreeManager(db_manager=db_manager)
        wt_mgr.reload(Path(temp_repo.working_dir))

        # Create a branch with a clean commit.
        temp_repo.git.checkout("-b", "agent-clean")
        (Path(temp_repo.working_dir) / "clean.txt").write_text("clean")
        temp_repo.git.add("clean.txt")
        temp_repo.git.commit("-m", "Add clean.txt")
        temp_repo.git.checkout("main")

        result = wt_mgr.merge_shared_branch("agent-clean")

        assert result["action"] == "merged"
        assert result["branch"] == "agent-clean"
        assert (Path(temp_repo.working_dir) / "clean.txt").exists()

    def test_preserves_branch_on_conflict(self, temp_repo, db_manager):
        """A branch with conflicts is preserved, not force-deleted."""
        wt_mgr = WorktreeManager(db_manager=db_manager)
        wt_mgr.reload(Path(temp_repo.working_dir))

        # Create a shared file on main.
        shared = Path(temp_repo.working_dir) / "shared.txt"
        shared.write_text("original")
        temp_repo.git.add("shared.txt")
        temp_repo.git.commit("-m", "Add shared.txt")

        # Create branch from initial commit, modify shared file.
        temp_repo.git.checkout("-b", "agent-conflict")
        shared.write_text("branch version")
        temp_repo.git.add("shared.txt")
        temp_repo.git.commit("-m", "Modify shared.txt on branch")

        # Go back to main and make conflicting change.
        temp_repo.git.checkout("main")
        shared.write_text("main version")
        temp_repo.git.add("shared.txt")
        temp_repo.git.commit("-m", "Modify shared.txt on main")

        result = wt_mgr.merge_shared_branch("agent-conflict")

        assert result["action"] == "preserved"
        assert result["branch"] == "agent-conflict"
        # Branch must still exist (not force-deleted).
        branch_names = [b.name for b in temp_repo.branches]
        assert "agent-conflict" in branch_names

    def test_skips_nonexistent_branch(self, temp_repo, db_manager):
        """A nonexistent branch is skipped."""
        wt_mgr = WorktreeManager(db_manager=db_manager)
        wt_mgr.reload(Path(temp_repo.working_dir))

        result = wt_mgr.merge_shared_branch("nonexistent-branch")

        assert result["action"] == "skipped"
        assert result["branch"] == "nonexistent-branch"


class TestCleanupAllStaleBranchesSkipsRepeatedConflicts:
    """A conflicting branch is preserved (see
    TestMergeSharedBranch.test_preserves_branch_on_conflict), not deleted --
    but with no AgentBranch row (an orphaned agent, or a leftover feature
    branch), nothing stopped cleanup_all_stale_branches from re-merging,
    re-conflicting, and re-`git merge --abort`ing it on every single sweep
    call, forever. `merge --abort` performs its own internal reset-to-HEAD,
    which rewrites every tracked file's mtime -- observed live running
    every ~60s indefinitely against HephaestusNG's own primary checkout
    (monitor.py's periodic _cleanup_stale_worktrees sweep, for a
    self-hosted project with no separate isolated copy to target instead),
    forcing the live dev server's Vite process to full-page-reload
    repeatedly and occasionally catching a file mid-rewrite as a syntax
    error."""

    def _make_conflicting_branch(self, temp_repo, branch_name: str) -> None:
        shared = Path(temp_repo.working_dir) / "shared.txt"
        shared.write_text("original")
        temp_repo.git.add("shared.txt")
        temp_repo.git.commit("-m", "Add shared.txt")

        temp_repo.git.checkout("-b", branch_name)
        shared.write_text("branch version")
        temp_repo.git.add("shared.txt")
        temp_repo.git.commit("-m", "Modify shared.txt on branch")

        temp_repo.git.checkout("main")
        shared.write_text("main version")
        temp_repo.git.add("shared.txt")
        temp_repo.git.commit("-m", "Modify shared.txt on main")

    def test_conflicting_untracked_branch_is_not_reattempted_against_same_head(
        self, test_db, temp_repo, worktree_manager
    ):
        self._make_conflicting_branch(temp_repo, "agent-conflict")

        attempts = []
        original = worktree_manager.merge_shared_branch

        def spy(branch_name, **kwargs):
            attempts.append(branch_name)
            return original(branch_name, **kwargs)

        worktree_manager.merge_shared_branch = spy

        worktree_manager.cleanup_all_stale_branches()
        worktree_manager.cleanup_all_stale_branches()

        assert attempts.count("agent-conflict") == 1, (
            "a conflict against an unchanged HEAD must not be retried"
        )
        branch_names = [b.name for b in temp_repo.branches]
        assert "agent-conflict" in branch_names, "still preserved, not force-deleted"

    def test_conflicting_branch_is_retried_once_main_moves(
        self, test_db, temp_repo, worktree_manager
    ):
        """The skip is keyed to the HEAD it conflicted against, not a
        permanent block -- once main genuinely changes, the branch (which
        might now merge cleanly, or at least deserves a fresh look) is
        attempted again."""
        self._make_conflicting_branch(temp_repo, "agent-conflict")

        attempts = []
        original = worktree_manager.merge_shared_branch

        def spy(branch_name, **kwargs):
            attempts.append(branch_name)
            return original(branch_name, **kwargs)

        worktree_manager.merge_shared_branch = spy

        worktree_manager.cleanup_all_stale_branches()

        # main moves -- the first sweep's recorded HEAD is now stale.
        unrelated = Path(temp_repo.working_dir) / "unrelated.txt"
        unrelated.write_text("new commit")
        temp_repo.git.add("unrelated.txt")
        temp_repo.git.commit("-m", "Unrelated commit on main")

        worktree_manager.cleanup_all_stale_branches()

        assert attempts.count("agent-conflict") == 2
