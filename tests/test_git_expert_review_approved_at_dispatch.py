"""Regression: _create_integration_worktree's review_approved pre-write
(see test_integration_worktree_review_mode_marker.py) only fires at
worktree CREATION time -- a git_expert task retried later (the common
case, since verify_git_expert_merged_and_pushed's own hard floor is what
forces the retry) reuses the existing worktree via Workflow.working_directory
without ever going back through that function. Observed live: tasks
03e8b25a and 5d2d8828 both had to have the marker written by hand after
their worktrees already existed, because nothing re-checked it on retry.

_ensure_git_expert_review_approved runs on every git_expert dispatch
instead (see _prepare_tmux_and_prompt), resolving the actual repo checkout
via `git rev-parse --git-common-dir` (mirroring git_expert.yaml's own `cd
"$MAIN_REPO"` resolution) rather than AutopilotProject.base_dir, which is
wrong for a multi-repo project (base_dir is the parent folder, not the
specific repo git_expert's prompt operates in).
"""

import uuid
from unittest.mock import MagicMock

import pytest
from git import Repo

from src.agents._create_agent_for_task_steps import _ensure_git_expert_review_approved
from src.core.database import AutopilotProject, Task, Workflow


@pytest.fixture
def repo_with_worktree(tmp_path):
    main_repo_path = tmp_path / "main_repo"
    main_repo_path.mkdir()
    repo = Repo.init(main_repo_path)
    (main_repo_path / "README.md").write_text("# x\n")
    repo.index.add(["README.md"])
    repo.index.commit("initial")

    branch = "feature/x"
    repo.git.branch(branch)
    wt_path = tmp_path / "worktrees" / "wt_x"
    wt_path.parent.mkdir(parents=True)
    repo.git.worktree("add", str(wt_path), branch)

    return main_repo_path, wt_path


def _pipeline(db_manager):
    return type("FakePipeline", (), {"db_manager": db_manager})()


def _task(db_manager, review_mode: bool):
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    workflow_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    with db_manager.session_scope() as session:
        session.add(
            AutopilotProject(
                id=project_id,
                name="Test Project",
                base_dir=f"/tmp/{project_id}",
                review_mode=review_mode,
            )
        )
        session.add(
            Workflow(
                id=workflow_id,
                name="w",
                phases_folder_path="/tmp",
                status="active",
                project_id=project_id,
            )
        )
        session.add(
            Task(
                id=task_id,
                workflow_id=workflow_id,
                raw_description="x",
                done_definition="x",
                status="pending",
            )
        )
    with db_manager.session_scope() as session:
        return session.query(Task).filter_by(id=task_id).first()


class TestEnsureGitExpertReviewApproved:
    def test_full_autopilot_writes_marker_to_main_repo_and_worktree(self, db_manager, repo_with_worktree):
        main_repo_path, wt_path = repo_with_worktree
        task = _task(db_manager, review_mode=False)

        _ensure_git_expert_review_approved(_pipeline(db_manager), task, str(wt_path))

        assert (main_repo_path / ".hephaestus" / "review_approved").exists()
        assert (wt_path / ".hephaestus" / "review_approved").exists()

    def test_review_mode_project_gets_no_marker(self, db_manager, repo_with_worktree):
        main_repo_path, wt_path = repo_with_worktree
        task = _task(db_manager, review_mode=True)

        _ensure_git_expert_review_approved(_pipeline(db_manager), task, str(wt_path))

        assert not (main_repo_path / ".hephaestus" / "review_approved").exists()
        assert not (wt_path / ".hephaestus" / "review_approved").exists()

    def test_no_project_found_does_not_raise(self, db_manager, repo_with_worktree):
        """task.workflow_id resolves to nothing (e.g. a standalone task) --
        must no-op quietly, not crash agent dispatch."""
        _main_repo_path, wt_path = repo_with_worktree
        task = MagicMock(workflow_id=None)

        _ensure_git_expert_review_approved(_pipeline(db_manager), task, str(wt_path))
