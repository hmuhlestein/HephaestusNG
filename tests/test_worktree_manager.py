#!/usr/bin/env python3
"""Tests for the git worktree isolation system."""

import shutil
import sys
import tempfile
import uuid
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

import pytest
from git import Repo

from src.core.database import Agent, DatabaseManager
from src.core.worktree_manager import WorktreeManager


@pytest.fixture
def temp_repo():
    """Create a temporary git repository for testing."""
    temp_dir = tempfile.mkdtemp()
    repo = Repo.init(temp_dir)

    # Create initial commit
    test_file = Path(temp_dir) / "README.md"
    test_file.write_text("# Test Repository\n")
    repo.index.add([str(test_file)])
    repo.index.commit("Initial commit")

    # Create review_approved marker so agent-safe-bin/git wrapper
    # allows merges in this test repo (the wrapper blocks merges to
    # protected branches unless this marker exists).
    marker_dir = Path(temp_dir) / ".hephaestus"
    marker_dir.mkdir(exist_ok=True)
    (marker_dir / "review_approved").write_text("test")

    yield repo

    # Cleanup
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def test_db():
    """Create a test database."""
    db_manager = DatabaseManager(":memory:")
    db_manager.create_tables()
    return db_manager


@pytest.fixture
def worktree_manager(test_db, temp_repo, monkeypatch):
    """Create a WorktreeManager with test configuration."""
    # Mock the config
    import src.core.simple_config

    config = src.core.simple_config.Config()
    config.paths.worktree_base_path = Path(tempfile.mkdtemp())
    config.git.main_repo_path = Path(temp_repo.working_dir)
    config.worktree_branch_prefix = "test-agent-"
    config.conflict_resolution_strategy = "newest_file_wins"
    config.prefer_child_on_tie = True

    monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
    monkeypatch.setattr("src.core.worktree_manager.get_config", lambda: config)

    manager = WorktreeManager(test_db)

    yield manager

    # Cleanup worktrees
    shutil.rmtree(config.paths.worktree_base_path, ignore_errors=True)


def test_create_agent_worktree(worktree_manager, test_db):
    """Test creating an isolated worktree for an agent."""
    agent_id = str(uuid.uuid4())

    # Create a test agent
    session = test_db.get_session()
    agent = Agent(
        id=agent_id, system_prompt="Test agent", status="working", cli_type="test"
    )
    session.add(agent)
    session.commit()
    session.close()

    # Create worktree
    result = worktree_manager.create_agent_worktree(agent_id)

    # Verify result
    assert "working_directory" in result
    assert "branch_name" in result
    assert "parent_commit" in result

    # Verify worktree exists
    worktree_path = Path(result["working_directory"])
    assert worktree_path.exists()
    assert worktree_path.is_dir()

    # Verify git worktree
    worktree_repo = Repo(worktree_path)
    assert worktree_repo.active_branch.name == result["branch_name"]

    # Cleanup
    worktree_manager.cleanup_worktree(agent_id)


def test_get_agent_branch_path_returns_none_when_no_record(worktree_manager, test_db):
    """Phase 3 Tier 1 item 7 (docs/AUTOPILOT_REFACTOR_PLAN.md): when no
    AgentBranch record exists for an agent, get_agent_branch_path must
    return None, not the main repo path. Pre-fix, it silently returned
    str(self._project_root) -- a caller like restart_agent that only checks
    truthiness (`if candidate: ...`) would accept the main repo as a valid
    worktree and could relaunch an agent directly into it instead of
    failing loudly."""
    agent_id = str(uuid.uuid4())
    # Deliberately no AgentBranch record created for this agent_id.
    assert worktree_manager.get_agent_branch_path(agent_id) is None


def test_get_agent_branch_path_returns_real_path_when_record_exists(
    worktree_manager, test_db
):
    """Companion to the None-fallback test above -- confirms the fix didn't
    also break the normal, working case."""
    agent_id = str(uuid.uuid4())
    session = test_db.get_session()
    agent = Agent(
        id=agent_id, system_prompt="Test agent", status="working", cli_type="test"
    )
    session.add(agent)
    session.commit()
    session.close()

    result = worktree_manager.create_agent_worktree(agent_id)

    path = worktree_manager.get_agent_branch_path(agent_id)
    assert path == result["working_directory"]

    worktree_manager.cleanup_worktree(agent_id)


def test_parent_child_inheritance(worktree_manager, test_db):
    """Test that child agents inherit parent's state."""
    parent_id = str(uuid.uuid4())
    child_id = str(uuid.uuid4())

    # Create parent agent
    session = test_db.get_session()
    parent_agent = Agent(
        id=parent_id, system_prompt="Parent", status="working", cli_type="test"
    )
    session.add(parent_agent)
    session.commit()
    session.close()

    # Create parent worktree
    parent_result = worktree_manager.create_agent_worktree(parent_id)
    parent_path = Path(parent_result["working_directory"])

    # Parent creates a file
    parent_file = parent_path / "parent_work.txt"
    parent_file.write_text("Parent's work content")

    # Commit parent's work
    parent_repo = Repo(parent_path)
    parent_repo.index.add([str(parent_file)])
    parent_repo.index.commit("Parent work")

    # Create child agent
    session = test_db.get_session()
    child_agent = Agent(
        id=child_id, system_prompt="Child", status="working", cli_type="test"
    )
    session.add(child_agent)
    session.commit()
    session.close()

    # Create child worktree with parent
    child_result = worktree_manager.create_agent_worktree(
        child_id, parent_agent_id=parent_id
    )
    child_path = Path(child_result["working_directory"])

    # Verify child has parent's file
    child_parent_file = child_path / "parent_work.txt"
    assert child_parent_file.exists()
    assert child_parent_file.read_text() == "Parent's work content"

    # Cleanup
    worktree_manager.cleanup_worktree(parent_id)
    worktree_manager.cleanup_worktree(child_id)


def test_parallel_isolation(worktree_manager, test_db):
    """Test that parallel agents work in isolation."""
    agent1_id = str(uuid.uuid4())
    agent2_id = str(uuid.uuid4())

    # Create two agents
    session = test_db.get_session()
    agent1 = Agent(
        id=agent1_id, system_prompt="Agent 1", status="working", cli_type="test"
    )
    agent2 = Agent(
        id=agent2_id, system_prompt="Agent 2", status="working", cli_type="test"
    )
    session.add(agent1)
    session.add(agent2)
    session.commit()
    session.close()

    # Create worktrees for both
    result1 = worktree_manager.create_agent_worktree(agent1_id)
    result2 = worktree_manager.create_agent_worktree(agent2_id)

    path1 = Path(result1["working_directory"])
    path2 = Path(result2["working_directory"])

    # Each agent creates a different file
    file1 = path1 / "agent1_file.txt"
    file1.write_text("Agent 1 content")

    file2 = path2 / "agent2_file.txt"
    file2.write_text("Agent 2 content")

    # Verify isolation - agent1's file not in agent2's worktree
    assert not (path2 / "agent1_file.txt").exists()
    assert not (path1 / "agent2_file.txt").exists()

    # Cleanup
    worktree_manager.cleanup_worktree(agent1_id)
    worktree_manager.cleanup_worktree(agent2_id)


def test_commit_for_validation(worktree_manager, test_db):
    """Test creating validation commits."""
    agent_id = str(uuid.uuid4())

    # Create agent
    session = test_db.get_session()
    agent = Agent(id=agent_id, system_prompt="Test", status="working", cli_type="test")
    session.add(agent)
    session.commit()
    session.close()

    # Create worktree
    result = worktree_manager.create_agent_worktree(agent_id)
    worktree_path = Path(result["working_directory"])

    # Create some work
    work_file = worktree_path / "work.py"
    work_file.write_text("def hello():\n    return 'world'")

    # Create validation commit
    commit_result = worktree_manager.commit_for_validation(agent_id, iteration=1)

    assert "commit_sha" in commit_result
    assert commit_result["files_changed"] == 1
    assert "Ready for validation" in commit_result["message"]

    # Verify commit exists (git message carries an [Agent <id>] prefix for traceability)
    worktree_repo = Repo(worktree_path)
    commit = worktree_repo.commit(commit_result["commit_sha"])
    assert commit_result["message"] in commit.message

    # Cleanup
    worktree_manager.cleanup_worktree(agent_id)


def test_cleanup_worktree(worktree_manager, test_db):
    """Test worktree cleanup."""
    agent_id = str(uuid.uuid4())

    # Create agent
    session = test_db.get_session()
    agent = Agent(id=agent_id, system_prompt="Test", status="working", cli_type="test")
    session.add(agent)
    session.commit()
    session.close()

    # Create worktree
    result = worktree_manager.create_agent_worktree(agent_id)
    worktree_path = Path(result["working_directory"])

    # Verify it exists
    assert worktree_path.exists()

    # Cleanup
    cleanup_result = worktree_manager.cleanup_worktree(agent_id)

    assert cleanup_result["status"] == "cleaned"
    assert cleanup_result["branch_preserved"]

    # Verify worktree is gone
    assert not worktree_path.exists()


class TestReloadInstanceIsolation:
    """reload() must be instance-local, not a side-channel write to the
    shared config singleton -- otherwise two WorktreeManager instances each
    scoped to a different project (the multi-project dispatch fix) would
    silently interfere with each other, whichever reload()ed last winning
    for every reader of get_config()."""

    def _make_repo(self, path):
        path.mkdir(parents=True, exist_ok=True)
        repo = Repo.init(path)
        f = path / "f.txt"
        f.write_text("x")
        repo.index.add([str(f)])
        repo.index.commit("init")
        return repo

    def test_reload_does_not_mutate_shared_config(self, worktree_manager, tmp_path):
        import src.core.simple_config as simple_config

        config = simple_config.get_config()
        original_main_repo_path = config.git.main_repo_path
        original_worktree_base_path = config.paths.worktree_base_path
        config.paths.worktree_base_path = None  # exercise the _project_root fallback
        try:
            other_repo_dir = tmp_path / "other-repo"
            self._make_repo(other_repo_dir)

            worktree_manager.reload(other_repo_dir)

            assert worktree_manager.worktree_base == other_repo_dir / ".worktrees"
            assert config.git.main_repo_path == original_main_repo_path
        finally:
            config.paths.worktree_base_path = original_worktree_base_path

    def test_two_instances_scoped_to_different_projects_dont_interfere(
        self, test_db, tmp_path, monkeypatch
    ):
        import src.core.simple_config as simple_config

        config = simple_config.Config()
        config.paths.worktree_base_path = None
        config.git.main_repo_path = tmp_path / "default"
        self._make_repo(config.git.main_repo_path)
        monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
        monkeypatch.setattr("src.core.worktree_manager.get_config", lambda: config)

        proj_a = tmp_path / "proj-a"
        self._make_repo(proj_a)
        proj_b = tmp_path / "proj-b"
        self._make_repo(proj_b)

        wt_a = WorktreeManager(test_db)
        wt_a.reload(proj_a)
        wt_b = WorktreeManager(test_db)
        wt_b.reload(proj_b)

        # Constructing/reloading b after a must not move a's state.
        assert wt_a.worktree_base == proj_a / ".worktrees"
        assert wt_b.worktree_base == proj_b / ".worktrees"
        assert wt_a.main_repo.working_dir == str(proj_a)
        assert wt_b.main_repo.working_dir == str(proj_b)


class TestCleanupAllStaleBranchesPrefixFilter:
    """Phase 3 Tier 2 item 9 (docs/AUTOPILOT_REFACTOR_PLAN.md):
    cleanup_all_stale_branches's untracked-branch sweep only matched
    "agent-"/"autopilot-"/"feature_architect/" -- missing "feature/",
    which every feature-pipeline branch (f"feature/{design}" and
    f"feature/{design}/{feature}") actually uses, so those branches were
    permanently exempt from cleanup."""

    def _commit_on_new_branch(self, repo, branch_name, filename):
        repo.git.checkout("-b", branch_name)
        f = Path(repo.working_dir) / filename
        f.write_text("content")
        repo.index.add([filename])
        repo.index.commit(f"add {filename}")
        repo.git.checkout(repo.heads[0].name if repo.heads[0].name != branch_name else "main")

    def test_feature_prefixed_branch_is_swept(self, worktree_manager, temp_repo):
        base = temp_repo.active_branch.name
        self._commit_on_new_branch(temp_repo, "feature/des12345/my-feature", "feat.txt")
        temp_repo.heads[base].checkout()

        result = worktree_manager.cleanup_all_stale_branches()

        remaining = [b.name for b in temp_repo.branches]
        assert "feature/des12345/my-feature" not in remaining
        assert result["merged"] + result["cleaned"] >= 1

    def test_unrelated_branch_is_left_alone(self, worktree_manager, temp_repo):
        base = temp_repo.active_branch.name
        self._commit_on_new_branch(temp_repo, "totally-unrelated-branch", "other.txt")
        temp_repo.heads[base].checkout()

        worktree_manager.cleanup_all_stale_branches()

        remaining = [b.name for b in temp_repo.branches]
        assert "totally-unrelated-branch" in remaining


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v"])
