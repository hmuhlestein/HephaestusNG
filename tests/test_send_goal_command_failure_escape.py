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
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.database import Phase, Task, Workflow


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


class TestSendGoalCommandNamesInputFiles:
    """The goal condition sent to the CLI's own self-checked-completion
    hook must name this phase's actual resolved input file(s), not just
    the (separately-delivered, best-effort) "INPUTS AVAILABLE" manifest --
    the hook re-evaluates the goal on every attempted stop, so a named
    file the agent never engaged with keeps failing the check instead of
    silently going unread. Same class of enforcement as the server-side
    verify_requirements_cover_scope_cli_flags/
    verify_development_produced_a_commit hard floors, at the CLI's own
    self-check layer instead of only at task-completion time."""

    @pytest.fixture
    def _task_with_phase(self, db_manager, tmp_path):
        workflow_id = str(uuid.uuid4())
        phase_id = str(uuid.uuid4())
        task_id = str(uuid.uuid4())
        with db_manager.session_scope() as session:
            session.add(Workflow(
                id=workflow_id, name="w", phases_folder_path="/tmp",
                status="active", working_directory=str(tmp_path),
            ))
            session.add(Phase(
                id=phase_id, workflow_id=workflow_id, order=5, name="development",
                description="d", done_definitions=["x"],
            ))
            session.add(Task(
                id=task_id, workflow_id=workflow_id, phase_id=phase_id,
                raw_description="x", done_definition="Task marked as done",
                status="in_progress",
            ))
        with db_manager.session_scope() as session:
            return session.query(Task).filter_by(id=task_id).first()

    @pytest.mark.asyncio
    async def test_appends_resolved_input_filenames_to_the_goal(
        self, db_manager, tmp_path, _task_with_phase
    ):
        pipeline = _launch_pipeline(db_manager)
        pane = Mock()
        cli_agent = Mock(needs_chunked_delivery=False)
        cli_agent.format_goal_command = Mock(side_effect=lambda c: f"/goal {c}")

        (tmp_path / ".hephaestus" / "architecture_design").mkdir(parents=True)
        (tmp_path / ".hephaestus" / "architecture_design" / "architecture-abc123.md").write_text("x")
        (tmp_path / ".hephaestus").joinpath("requirements-def456.md").write_text("x")

        with patch(
            "src.autopilot.spec.load_phase_inputs",
            return_value={"development": {"required": ["architecture.md", "requirements.md"]}},
        ), patch(
            "src.autopilot.spec.input_producer_phases",
            side_effect=lambda wf_id, name: ["architecture_design"] if name == "architecture.md" else [],
        ):
            await pipeline._send_goal_command(pane, cli_agent, _task_with_phase, "phase")

        sent_condition = cli_agent.format_goal_command.call_args[0][0]
        assert "architecture-abc123.md" in sent_condition
        assert "requirements-def456.md" in sent_condition
        assert "actually read and resolved" in sent_condition

    @pytest.mark.asyncio
    async def test_no_declared_inputs_leaves_goal_unchanged(
        self, db_manager, tmp_path, _task_with_phase
    ):
        pipeline = _launch_pipeline(db_manager)
        pane = Mock()
        cli_agent = Mock(needs_chunked_delivery=False)
        cli_agent.format_goal_command = Mock(side_effect=lambda c: f"/goal {c}")

        with patch("src.autopilot.spec.load_phase_inputs", return_value={}):
            await pipeline._send_goal_command(pane, cli_agent, _task_with_phase, "phase")

        sent_condition = cli_agent.format_goal_command.call_args[0][0]
        assert "actually read and resolved" not in sent_condition
