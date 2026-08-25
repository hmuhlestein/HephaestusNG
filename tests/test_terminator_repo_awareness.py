"""REQ-16: Terminator._commit_wip_in_shared_worktree must not blindly
commit into the workflow's working_directory when the task it's
committing on behalf of is scoped to a different repo (Task.repo_id) --
on a multi-repo project that directory could be the wrong git tree
entirely.
"""

from unittest.mock import MagicMock

import git as git_module
import pytest

from src.core.database import AutopilotProject, ProjectRepo, Task, Workflow


@pytest.fixture
def terminator(db_manager):
    from src.agents.terminator import Terminator

    agent_manager = MagicMock()
    agent_manager.db_manager = db_manager
    return Terminator(agent_manager)


def _seed_project_and_workflow(db_manager, wf_working_dir, repo_path):
    session = db_manager.get_session()
    try:
        session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(repo_path)))
        session.flush()
        session.add(ProjectRepo(id="repo-primary", project_id="proj-1", label="main", path=str(repo_path), is_primary=True))
        session.add(ProjectRepo(id="repo-sibling", project_id="proj-1", label="backend", path=str(repo_path.parent / "sibling-repo"), is_primary=False))
        session.add(
            Workflow(
                id="wf-1",
                name="wf",
                status="active",
                project_id="proj-1",
                phases_folder_path=str(wf_working_dir),
                working_directory=str(wf_working_dir),
            )
        )
        session.commit()
    finally:
        session.close()


def test_skips_commit_when_task_repo_id_points_elsewhere(terminator, db_manager, tmp_path):
    wf_working_dir = tmp_path / "workflow-worktree"
    wf_working_dir.mkdir()
    repo = git_module.Repo.init(wf_working_dir)
    repo.index.commit("initial", author=git_module.Actor("t", "t@t.com"), committer=git_module.Actor("t", "t@t.com"))
    (wf_working_dir / "dirty.txt").write_text("uncommitted change")

    _seed_project_and_workflow(db_manager, wf_working_dir, wf_working_dir)

    session = db_manager.get_session()
    try:
        session.add(Task(id="task-1", raw_description="x", done_definition="d", workflow_id="wf-1", repo_id="repo-sibling", status="in_progress"))
        session.commit()
    finally:
        session.close()

    terminator._commit_wip_in_shared_worktree("agent-1", "task-1")

    # The mismatch must have short-circuited before any commit was made --
    # the dirty file is still uncommitted, not swept into a WIP commit in
    # what is (per repo_id) the wrong repo.
    assert git_module.Repo(wf_working_dir).is_dirty(untracked_files=True)


def test_commits_when_task_repo_id_matches_working_directory(terminator, db_manager, tmp_path):
    wf_working_dir = tmp_path / "workflow-worktree"
    wf_working_dir.mkdir()
    repo = git_module.Repo.init(wf_working_dir)
    repo.index.commit("initial", author=git_module.Actor("t", "t@t.com"), committer=git_module.Actor("t", "t@t.com"))
    (wf_working_dir / "dirty.txt").write_text("uncommitted change")

    _seed_project_and_workflow(db_manager, wf_working_dir, wf_working_dir)

    session = db_manager.get_session()
    try:
        session.add(Task(id="task-2", raw_description="x", done_definition="d", workflow_id="wf-1", repo_id="repo-primary", status="in_progress"))
        session.commit()
    finally:
        session.close()

    terminator._commit_wip_in_shared_worktree("agent-1", "task-2")

    assert not git_module.Repo(wf_working_dir).is_dirty(untracked_files=True)


def test_commits_when_task_has_no_repo_id(terminator, db_manager, tmp_path):
    """Unchanged behavior: no repo_id means no mismatch to check."""
    wf_working_dir = tmp_path / "workflow-worktree"
    wf_working_dir.mkdir()
    repo = git_module.Repo.init(wf_working_dir)
    repo.index.commit("initial", author=git_module.Actor("t", "t@t.com"), committer=git_module.Actor("t", "t@t.com"))
    (wf_working_dir / "dirty.txt").write_text("uncommitted change")

    _seed_project_and_workflow(db_manager, wf_working_dir, wf_working_dir)

    session = db_manager.get_session()
    try:
        session.add(Task(id="task-3", raw_description="x", done_definition="d", workflow_id="wf-1", repo_id=None, status="in_progress"))
        session.commit()
    finally:
        session.close()

    terminator._commit_wip_in_shared_worktree("agent-1", "task-3")

    assert not git_module.Repo(wf_working_dir).is_dirty(untracked_files=True)
