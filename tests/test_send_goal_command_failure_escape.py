"""Regression: task.done_definition is written as pure success criteria
(an AND-chain of what must be true for "done") with no clause for a
legitimate give-up. _send_goal_command used to hand that text straight to
the CLI's self-checked-completion hook (Claude Code's `/goal`) verbatim --
when a task genuinely can't succeed for a reason outside the agent's
control (e.g. git_expert blocked by an unrelated open bug ticket the merge
gate correctly refuses to ignore), the goal as written can never be
satisfied, even by a legitimate update_task_status(status='failed') call.
The CLI's own stop-hook then refuses to end the turn -- it just cycles
"goal not met -- continuing" until its own retry cap gives up, leaving the
task stuck in_progress server-side with an idle agent nothing will nudge
again. Observed live: task 7ef17b96 (git_expert) deadlocked exactly this
way over ticket-6de20f94.

_send_goal_command now appends an explicit "OR legitimately marked failed"
escape to the condition before it's sent, so a real failed-with-reason
call satisfies the goal too.
"""

import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.database import Task, Workflow


@pytest.fixture
def _task(db_manager):
    workflow_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    with db_manager.session_scope() as session:
        session.add(
            Workflow(id=workflow_id, name="w", phases_folder_path="/tmp", status="active")
        )
        session.add(
            Task(
                id=task_id,
                workflow_id=workflow_id,
                raw_description="x",
                done_definition="Current branch confirmed AND Task marked as done",
                status="in_progress",
            )
        )
    with db_manager.session_scope() as session:
        return session.query(Task).filter_by(id=task_id).first()


def _launch_pipeline(db_manager):
    from src.agents.launch_pipeline import LaunchPipeline

    fake_agent_manager = type("FakeAgentManager", (), {"db_manager": db_manager})()
    return LaunchPipeline(fake_agent_manager)


class TestSendGoalCommandFailureEscape:
    @pytest.mark.asyncio
    async def test_goal_condition_includes_a_legitimate_failure_escape(
        self, db_manager, _task
    ):
        pipeline = _launch_pipeline(db_manager)
        pane = Mock()
        cli_agent = Mock(needs_chunked_delivery=False)
        cli_agent.format_goal_command = Mock(side_effect=lambda c: f"/goal {c}")

        await pipeline._send_goal_command(pane, cli_agent, _task, "phase")

        sent_condition = cli_agent.format_goal_command.call_args[0][0]
        assert _task.done_definition in sent_condition
        assert "failed" in sent_condition.lower()
        assert "update_task_status" in sent_condition

    @pytest.mark.asyncio
    async def test_validator_agent_types_get_no_goal_at_all(self, db_manager, _task):
        """Only phase agents work from task.done_definition -- a goal built
        from it for a validator/arbitration agent would describe someone
        else's task, so these must be skipped entirely (unaffected by the
        new escape clause)."""
        pipeline = _launch_pipeline(db_manager)
        pane = Mock()
        cli_agent = Mock(needs_chunked_delivery=False)
        cli_agent.format_goal_command = Mock(side_effect=lambda c: f"/goal {c}")

        await pipeline._send_goal_command(pane, cli_agent, _task, "arbitration")

        cli_agent.format_goal_command.assert_not_called()
        pane.send_keys.assert_not_called()
