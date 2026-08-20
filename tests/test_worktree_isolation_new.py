#!/usr/bin/env python3
"""Tests for the worktree isolation design decisions (§9.2):

- in-repo `.worktrees/` default base
- `.git/info/exclude` management (untracked .gitignore stays pristine)
- per-worktree git-excluded `.hephaestus/` inbound-context dir
- merge-on-success brings committed work into main
- discard-on-failure removes the worktree+branch, leaving main clean
"""

import shutil
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

import pytest
from git import Repo

from src.core.database import Agent, DatabaseManager
from src.core.worktree_manager import WorktreeManager


@pytest.fixture
def temp_repo():
    temp_dir = tempfile.mkdtemp()
    repo = Repo.init(temp_dir)
    readme = Path(temp_dir) / "README.md"
    readme.write_text("# Test\n")
    repo.index.add([str(readme)])
    repo.index.commit("Initial commit")
    # Ensure a stable base branch name
    try:
        repo.git.branch("-M", "main")
    except Exception:
        pass
    yield repo
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def test_db():
    db = DatabaseManager(":memory:")
    db.create_tables()
    return db


@pytest.fixture
def manager(test_db, temp_repo, monkeypatch):
    import src.core.simple_config

    config = src.core.simple_config.Config()
    config.git.main_repo_path = Path(temp_repo.working_dir)
    config.paths.project_root = Path(temp_repo.working_dir)
    config.git.base_branch = "main"
    config.git.branch_prefix = "agent-"
    config.conflict_resolution_strategy = "newest_file_wins"
    config.paths.worktree_base_path = None  # exercise the <repo>/.worktrees default
    # Patch where the name is actually looked up (the manager's own namespace),
    # not just the source module — otherwise the real singleton config is used.
    monkeypatch.setattr("src.core.simple_config.get_config", lambda: config)
    monkeypatch.setattr("src.core.worktree_manager.get_config", lambda: config)
    return WorktreeManager(test_db)


def _make_agent(test_db, agent_id):
    session = test_db.get_session()
    session.add(
        Agent(id=agent_id, system_prompt="t", status="working", cli_type="test")
    )
    session.commit()
    session.close()


def test_worktree_base_is_in_repo(manager, temp_repo):
    assert manager.worktree_base == Path(temp_repo.working_dir) / ".worktrees"


def test_info_exclude_managed(manager, temp_repo):
    exclude = Path(temp_repo.working_dir) / ".git" / "info" / "exclude"
    content = exclude.read_text()
    assert ".worktrees/" in content
    assert ".hephaestus/" in content
    # The tracked .gitignore must not be touched
    assert not (Path(temp_repo.working_dir) / ".gitignore").exists()


def test_context_dir_populated(manager, test_db):
    agent_id = str(uuid.uuid4())
    _make_agent(test_db, agent_id)
    result = manager.create_agent_worktree(
        agent_id, context_files={"design.md": "# Design", "qa_spec.json": "{}"}
    )
    ctx = Path(result["context_dir"])
    assert ctx == Path(result["working_directory"]) / ".hephaestus"
    assert (ctx / "design.md").read_text() == "# Design"
    assert (ctx / "qa_spec.json").read_text() == "{}"


def test_context_dir_is_git_excluded(manager, test_db):
    """The .hephaestus/ context must never be staged/committed/merged."""
    agent_id = str(uuid.uuid4())
    _make_agent(test_db, agent_id)
    result = manager.create_agent_worktree(agent_id, context_files={"secret.md": "x"})
    wt = Repo(result["working_directory"])
    wt.git.add("-A")
    # Nothing under .hephaestus should be staged
    staged = wt.git.diff("--cached", "--name-only")
    assert ".hephaestus" not in staged


def test_merge_on_success_brings_work_to_main(manager, test_db, temp_repo):
    agent_id = str(uuid.uuid4())
    _make_agent(test_db, agent_id)
    result = manager.create_agent_worktree(agent_id)
    wt_path = Path(result["working_directory"])

    (wt_path / "feature.py").write_text("print('hi')\n")
    manager.commit_for_validation(agent_id, iteration=1)
    merge = manager.merge_to_main(agent_id)

    assert merge["status"] in ("success", "conflict_resolved")
    # File is now on main, and the context dir never leaked in
    main_files = temp_repo.git.ls_files().splitlines()
    assert "feature.py" in main_files
    assert not any(".hephaestus" in f for f in main_files)


def test_discard_on_failure_leaves_main_clean(manager, test_db, temp_repo):
    agent_id = str(uuid.uuid4())
    _make_agent(test_db, agent_id)
    result = manager.create_agent_worktree(agent_id)
    wt_path = Path(result["working_directory"])
    branch = result["branch_name"]

    # Agent produces half-baked work, then fails (never merged)
    (wt_path / "broken.py").write_text("syntax error(((\n")
    disc = manager.discard_agent(agent_id)

    assert disc["status"] == "cleaned"
    assert disc["branch_preserved"] is False
    assert not wt_path.exists()  # worktree gone
    assert branch not in [b.name for b in temp_repo.branches]  # branch gone
    assert "broken.py" not in temp_repo.git.ls_files().splitlines()  # main clean


def test_parallel_agents_do_not_collide(manager, test_db):
    a1, a2 = str(uuid.uuid4()), str(uuid.uuid4())
    _make_agent(test_db, a1)
    _make_agent(test_db, a2)
    r1 = manager.create_agent_worktree(a1)
    r2 = manager.create_agent_worktree(a2)
    p1, p2 = Path(r1["working_directory"]), Path(r2["working_directory"])
    (p1 / "a1.txt").write_text("1")
    (p2 / "a2.txt").write_text("2")
    assert p1 != p2
    assert not (p2 / "a1.txt").exists()
    assert not (p1 / "a2.txt").exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-p", "no:libtmux"])
