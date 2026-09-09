"""Regression: _commit_wip_in_shared_worktree's blind `git add -A &&
git commit --no-verify` used to commit whatever was on disk unconditionally
when a phase agent (on a shared feature worktree) got force-terminated --
including, in a real incident, the literal unresolved markers left by a
`git stash pop` that was still conflicted at the moment of termination. A
later phase's own commit had to manually strip them back out. Must refuse
to commit here instead, leaving the conflicted state for a human/ticketed
look -- same guard, same reasoning, as worktree_manager.py's
_commit_in_worktree (see tests/test_worktree_manager.py's
TestWorktreeUnresolvedConflictReason for the shared detection logic's own
tests)."""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from git import Repo

from src.agents.terminator import Terminator
from src.core.database import DatabaseManager, Workflow


@pytest.fixture
def shared_worktree_repo():
    temp_dir = tempfile.mkdtemp()
    repo = Repo.init(temp_dir, initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@t.com")
        cw.set_value("user", "name", "t")
    (Path(temp_dir) / "file.py").write_text("line1\n")
    repo.index.add(["file.py"])
    repo.index.commit("init")
    yield temp_dir, repo
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def terminator_with_task(shared_worktree_repo):
    temp_dir, repo = shared_worktree_repo
    db_manager = DatabaseManager(":memory:")
    db_manager.create_tables()

    from src.core.database import Task

    session = db_manager.get_session()
    session.add(Workflow(
        id="wf-1", name="t", phases_folder_path="/tmp",
        status="active", working_directory=temp_dir,
    ))
    session.add(Task(
        id="task-1", raw_description="d", done_definition="d",
        workflow_id="wf-1",
    ))
    session.commit()
    session.close()

    agent_manager = MagicMock(db_manager=db_manager)
    return Terminator(agent_manager), repo


def test_refuses_to_commit_over_conflict_markers(terminator_with_task):
    terminator, repo = terminator_with_task
    head_before = repo.head.commit.hexsha

    conflicted_file = Path(repo.working_dir) / "conflicted.py"
    conflicted_file.write_text(
        "line one\n<<<<<<< Updated upstream\nold\n=======\nnew\n>>>>>>> Stashed changes\n"
    )

    terminator._commit_wip_in_shared_worktree("agent-1", "task-1")

    assert repo.head.commit.hexsha == head_before, "must not create a commit over unresolved conflict markers"
    assert conflicted_file.exists(), "the conflicted file itself must be left untouched for manual cleanup"


def test_still_commits_normal_wip(terminator_with_task):
    """Sanity check the guard isn't overbroad -- ordinary uncommitted work
    still gets auto-saved as before."""
    terminator, repo = terminator_with_task
    head_before = repo.head.commit.hexsha

    (Path(repo.working_dir) / "work.py").write_text("def hello(): pass\n")

    terminator._commit_wip_in_shared_worktree("agent-1", "task-1")

    assert repo.head.commit.hexsha != head_before
    assert "Auto-saved on terminate" in repo.head.commit.message
