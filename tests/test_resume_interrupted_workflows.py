"""Regression: _resume_interrupted_workflows' on-demand retry path
(reactivate=True, the design-level Play/Resume button's /api/autopilot/recover
call) only reset tasks in "failed" status, not tasks individually paused
("blocked", via /api/tasks/{id}/pause). A workflow whose only non-terminal
task was "blocked" flipped back to "active" on every Resume click but never
re-dispatched that task -- invisible to both the failed-task reset and the
orphaned-agent scan below it -- so the workflow looked like it "immediately
paused again" no matter how many times Play was pressed.
"""

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.database import Agent, DatabaseManager, Phase, Task, Workflow


@pytest.fixture
def test_db(tmp_path):
    db_path = tmp_path / "test.db"
    manager = DatabaseManager(str(db_path))
    manager.create_tables()
    return manager


async def _run_resume(test_db, workflow_id=None, project_id=None):
    import src.mcp.server as server_module

    with patch.object(server_module, "server_state") as mock_state:
        mock_state.db_manager = test_db
        mock_state.agent_manager = MagicMock()
        mock_state.agent_manager.restart_agent = AsyncMock()
        mock_state.queue_service.should_queue_task.return_value = True
        return await server_module._resume_interrupted_workflows(
            workflow_id=workflow_id, project_id=project_id, reactivate=True
        )


class TestResumeInterruptedWorkflowsUnblocksTasks:
    @pytest.mark.asyncio
    async def test_blocked_task_is_reset_and_requeued(self, test_db):
        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-blocked", name="t", phases_folder_path="/tmp",
                status="paused", definition_id="feature_architect",
            )
        )
        session.add(
            Task(
                id="task-blocked", workflow_id="wf-blocked", phase_id="phase-1",
                raw_description="r", done_definition="d",
                status="blocked", assigned_agent_id="old-agent",
            )
        )
        session.commit()
        session.close()

        result = await _run_resume(test_db, "wf-blocked")

        session = test_db.get_session()
        task = session.query(Task).filter_by(id="task-blocked").first()
        wf = session.query(Workflow).filter_by(id="wf-blocked").first()
        assert task.status == "pending"
        assert task.assigned_agent_id is None
        assert wf.status == "active"
        session.close()
        assert result["resumed"] == 1

    @pytest.mark.asyncio
    async def test_failed_task_still_reset_and_requeued(self, test_db):
        """Sanity check the fix isn't overbroad: pre-existing "failed" task
        handling must still work exactly as before."""
        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-failed", name="t", phases_folder_path="/tmp",
                status="paused", definition_id="feature_architect",
            )
        )
        session.add(
            Task(
                id="task-failed", workflow_id="wf-failed", phase_id="phase-1",
                raw_description="r", done_definition="d",
                status="failed", failure_reason="boom",
            )
        )
        session.commit()
        session.close()

        result = await _run_resume(test_db, "wf-failed")

        session = test_db.get_session()
        task = session.query(Task).filter_by(id="task-failed").first()
        session.close()
        assert task.status == "pending"
        assert task.failure_reason is None
        assert result["resumed"] == 1


class TestResumeInterruptedWorkflowsResetsGotoBudget:
    """Regression: total_gotos is a persisted counter that never decreases
    on its own. A workflow that failed by exhausting max_total_gotos (or
    the arbitration cap that follows it) re-exceeded the SAME exhausted
    limit on its very next evaluation after a Retry, instantly re-failing
    with zero real attempt in between -- Retry looked like it did nothing.
    """

    @pytest.mark.asyncio
    async def test_reactivate_resets_total_gotos_and_stamps_gotos_reset_at(self, test_db):
        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-exhausted", name="t", phases_folder_path="/tmp",
                status="failed", status_reason="scope_review: arbitrated 3 times without converging",
                definition_id="autopilot", total_gotos=676,
            )
        )
        session.commit()
        session.close()

        result = await _run_resume(test_db, "wf-exhausted")

        session = test_db.get_session()
        wf = session.query(Workflow).filter_by(id="wf-exhausted").first()
        assert wf.status == "active"
        assert wf.total_gotos == 0
        assert wf.gotos_reset_at is not None
        session.close()
        assert result["resumed"] == 0  # no tasks to unblock, but the workflow itself was reactivated


class TestResumeInterruptedWorkflowsProjectScoping:
    """Regression: the project-level Play button, on hitting the "already
    running" self-conflict 409, used to just show a no-op toast -- the
    service loop being up doesn't by itself re-drive a workflow stuck on an
    individually-blocked task, so a project could sit paused forever no
    matter how many times Play was pressed. Play now cascades into
    recovering every one of the project's own workflows, scoped by
    project_id instead of a single workflow_id."""

    @pytest.mark.asyncio
    async def test_recovers_every_workflow_in_the_project(self, test_db):
        session = test_db.get_session()
        session.add_all(
            [
                Workflow(
                    id="wf-proj-a-1", name="t", phases_folder_path="/tmp",
                    status="paused", definition_id="feature_architect",
                    project_id="proj-a",
                ),
                Workflow(
                    id="wf-proj-a-2", name="t", phases_folder_path="/tmp",
                    status="failed", definition_id="feature_architect",
                    project_id="proj-a",
                ),
            ]
        )
        session.add_all(
            [
                Task(
                    id="task-a1", workflow_id="wf-proj-a-1", phase_id="phase-1",
                    raw_description="r", done_definition="d", status="blocked",
                ),
                Task(
                    id="task-a2", workflow_id="wf-proj-a-2", phase_id="phase-1",
                    raw_description="r", done_definition="d", status="failed",
                ),
            ]
        )
        session.commit()
        session.close()

        result = await _run_resume(test_db, project_id="proj-a")

        session = test_db.get_session()
        task_a1 = session.query(Task).filter_by(id="task-a1").first()
        task_a2 = session.query(Task).filter_by(id="task-a2").first()
        wf_a1 = session.query(Workflow).filter_by(id="wf-proj-a-1").first()
        wf_a2 = session.query(Workflow).filter_by(id="wf-proj-a-2").first()
        session.close()
        assert task_a1.status == "pending"
        assert task_a2.status == "pending"
        assert wf_a1.status == "active"
        assert wf_a2.status == "active"
        assert result["resumed"] == 2

    @pytest.mark.asyncio
    async def test_does_not_touch_a_different_projects_workflows(self, test_db):
        session = test_db.get_session()
        session.add_all(
            [
                Workflow(
                    id="wf-proj-a", name="t", phases_folder_path="/tmp",
                    status="paused", definition_id="feature_architect",
                    project_id="proj-a",
                ),
                Workflow(
                    id="wf-proj-b", name="t", phases_folder_path="/tmp",
                    status="paused", definition_id="feature_architect",
                    project_id="proj-b",
                ),
            ]
        )
        session.add_all(
            [
                Task(
                    id="task-a", workflow_id="wf-proj-a", phase_id="phase-1",
                    raw_description="r", done_definition="d", status="blocked",
                ),
                Task(
                    id="task-b", workflow_id="wf-proj-b", phase_id="phase-1",
                    raw_description="r", done_definition="d", status="blocked",
                ),
            ]
        )
        session.commit()
        session.close()

        result = await _run_resume(test_db, project_id="proj-a")

        session = test_db.get_session()
        task_b = session.query(Task).filter_by(id="task-b").first()
        wf_b = session.query(Workflow).filter_by(id="wf-proj-b").first()
        session.close()
        assert task_b.status == "blocked"
        assert wf_b.status == "paused"
        assert result["resumed"] == 1


def _make_git_repo_with_worktree(tmp_path, branch_merged: bool):
    """A base repo (default branch "main") plus a worktree checked out on
    its own branch with one commit -- mirrors this codebase's shared
    per-workflow worktree layout (<project>/.worktrees/wt_X on branch
    agent-X). branch_merged merges that branch into main first, matching a
    git_commit_push task whose work already landed."""
    repo = tmp_path / "project"
    repo.mkdir()
    run = lambda *args, cwd=repo: subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    )
    run("git", "init", "-b", "main")
    run("git", "config", "user.email", "t@t.com")
    run("git", "config", "user.name", "t")
    (repo / "f.txt").write_text("base")
    run("git", "add", ".")
    run("git", "commit", "-m", "base")

    wt_path = repo / ".worktrees" / "wt_abc123"
    wt_path.parent.mkdir()
    run("git", "worktree", "add", "-b", "agent-abc123", str(wt_path))
    (wt_path / "g.txt").write_text("feature work")
    run("git", "add", ".", cwd=wt_path)
    run("git", "commit", "-m", "feature work", cwd=wt_path)

    if branch_merged:
        run("git", "merge", "--no-ff", "agent-abc123")

    return wt_path


class TestResumeInterruptedWorkflowsGitCommitPushRecovery:
    """Regression, observed live: an orphaned git_commit_push agent whose
    completion call was lost to a connection drop (a backend restart
    landed exactly on top of the agent's final complete_my_task call) got
    blindly redispatched to redo the whole git sequence from scratch, even
    though the merge+push had already succeeded (verified after the fact
    via `git log` -- the branch was already an ancestor of main).
    _git_commit_push_already_landed checks git state directly so the
    orphan-resume path can mark the task done instead of wasting a full
    re-run on an already-completed merge."""

    def _seed(self, test_db, working_directory, phase_name="git_commit_push"):
        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-git", name="t", phases_folder_path="/tmp", status="active",
                definition_id="autopilot", working_directory=str(working_directory),
            )
        )
        session.add(
            Phase(
                id="phase-gcp", workflow_id="wf-git", order=12, name=phase_name,
                description="d", done_definitions=["x"],
            )
        )
        session.add(
            Agent(
                id="agent-old", system_prompt="p", status="working", cli_type="claude",
                agent_type="phase", tmux_session_name="agent_old", current_task_id="task-git",
            )
        )
        session.add(
            Task(
                id="task-git", workflow_id="wf-git", phase_id="phase-gcp",
                raw_description="r", done_definition="d", status="in_progress",
                assigned_agent_id="agent-old",
            )
        )
        session.commit()
        session.close()

    @pytest.mark.asyncio
    async def test_marks_done_instead_of_restarting_when_branch_already_merged(self, test_db, tmp_path):
        wt_path = _make_git_repo_with_worktree(tmp_path, branch_merged=True)
        self._seed(test_db, wt_path)

        with patch("src.mcp.server._tmux_session_alive", return_value=False):
            result = await _run_resume(test_db, "wf-git")

        assert result["resumed"] == 1
        session = test_db.get_session()
        task = session.query(Task).filter_by(id="task-git").first()
        agent = session.query(Agent).filter_by(id="agent-old").first()
        assert task.status == "done"
        assert agent.status == "terminated"
        assert agent.current_task_id is None

    @pytest.mark.asyncio
    async def test_still_restarts_when_branch_not_yet_merged(self, test_db, tmp_path):
        wt_path = _make_git_repo_with_worktree(tmp_path, branch_merged=False)
        self._seed(test_db, wt_path)

        with patch("src.mcp.server._tmux_session_alive", return_value=False):
            result = await _run_resume(test_db, "wf-git")

        assert result["resumed"] == 1
        session = test_db.get_session()
        task = session.query(Task).filter_by(id="task-git").first()
        assert task.status == "in_progress"  # untouched here -- restart_agent handles it

    @pytest.mark.asyncio
    async def test_does_not_apply_to_other_phases(self, test_db, tmp_path):
        """Only git_commit_push has this external-state check -- any other
        phase falls straight through to the normal redispatch path even if
        its branch happens to already be merged."""
        wt_path = _make_git_repo_with_worktree(tmp_path, branch_merged=True)
        self._seed(test_db, wt_path, phase_name="development")

        with patch("src.mcp.server._tmux_session_alive", return_value=False):
            result = await _run_resume(test_db, "wf-git")

        assert result["resumed"] == 1
        session = test_db.get_session()
        task = session.query(Task).filter_by(id="task-git").first()
        assert task.status == "in_progress"
