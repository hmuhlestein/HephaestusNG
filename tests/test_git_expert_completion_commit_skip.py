"""git_expert must not get a `git add -A` completion commit.

Every other phase gets one: commit_and_link_ticket captures work an agent
left uncommitted on disk (a real incident -- a feature was marked completed
with uncommitted changes still in its worktree).

git_expert is the exception, because its contract is the opposite: it has
already committed, pushed, and in review_mode opened a PR *before* it
reports done. A commit created afterwards lands AFTER that push, and
verify_git_expert_merged_and_pushed's next check -- "the feature branch has
commits not yet pushed" -- then rejects the completion over a commit the
tool itself just made.

That rejection precedes the same floor's PR/CI check, so get_pr_status is
never called: Feature.pr_url stays NULL, the task lands "failed" rather
than parking in_progress, and _resolve_pending_pr_status (which requires an
in_progress git_expert task AND a non-null pr_url, on an active workflow)
can never resolve the PR. A red CI check goes unnoticed indefinitely.

Observed live: the agent pushed and opened PR #1097, called done at
20:39:17, this hook swept three stray *.go.bak files into commit abbd12be,
and the floor rejected 12s later citing unpushed commits. IDB-2482.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.task_completion import git_link


def _task(phase_id="phase-1", workflow_id="wf-1"):
    task = MagicMock()
    task.id = "task-1234"
    task.phase_id = phase_id
    task.workflow_id = workflow_id
    task.ticket_id = None
    return task


def _session(phase_name):
    """A session whose Phase lookup returns phase_name and whose Workflow
    lookup returns a real directory."""
    phase = MagicMock()
    phase.name = phase_name
    workflow = MagicMock()
    workflow.working_directory = "/tmp"

    session = MagicMock()

    def _query(model):
        q = MagicMock()
        q.filter_by.return_value.first.return_value = (
            phase if getattr(model, "__name__", "") == "Phase" else workflow
        )
        return q

    session.query.side_effect = _query
    return session


@pytest.mark.asyncio
class TestCompletionCommitSkippedForGitExpert:
    async def test_git_expert_does_not_get_a_completion_commit(self):
        with patch.object(git_link, "_do_git_commit") as do_commit, \
             patch("src.core.app_context.get_app_state", return_value=MagicMock()):
            sha = await git_link.commit_and_link_ticket(
                _session("git_expert"), "agent-1", _task(), "hand-off complete"
            )

        do_commit.assert_not_called()
        assert sha is None

    async def test_every_other_phase_still_gets_one(self):
        """The safety net stays for the phases that need it -- this must be
        a git_expert carve-out, not a removal."""
        with patch.object(git_link, "_do_git_commit", return_value="abc1234") as do_commit, \
             patch("src.core.app_context.get_app_state", return_value=MagicMock()):
            sha = await git_link.commit_and_link_ticket(
                _session("development"), "agent-1", _task(), "implemented the thing"
            )

        do_commit.assert_called_once()
        assert sha == "abc1234"

    @pytest.mark.parametrize(
        "phase", ["development", "qa_validation", "doc_review", "security_review"]
    )
    async def test_the_carve_out_is_exactly_one_phase(self, phase):
        with patch.object(git_link, "_do_git_commit", return_value="sha") as do_commit, \
             patch("src.core.app_context.get_app_state", return_value=MagicMock()):
            await git_link.commit_and_link_ticket(
                _session(phase), "agent-1", _task(), "summary"
            )
        do_commit.assert_called_once()
