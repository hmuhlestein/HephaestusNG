"""Unit tests for the Guardian trajectory monitoring system."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.database import Agent, AgentLog, Task
from src.monitoring.guardian import Guardian


@pytest.fixture
def mock_db_manager():
    """Create mock database manager."""
    mock = Mock()
    mock.get_session = Mock()

    @contextmanager
    def _session_scope():
        # Guardian production code uses session_scope() rather than raw
        # get_session()/close() — route through the same mocked session so
        # tests that configure get_session.return_value keep working, and
        # mirror DatabaseManager.session_scope()'s real commit/close semantics.
        session = mock.get_session()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    mock.session_scope = Mock(side_effect=_session_scope)
    return mock


@pytest.fixture
def mock_agent_manager():
    """Create mock agent manager."""
    mock = Mock()
    mock.get_agent_output = Mock(return_value="Agent working on task...")
    mock.send_recovery_keystrokes = AsyncMock(return_value=True)
    mock.send_message_to_agent = AsyncMock()
    mock.tmux_server = Mock()
    return mock


@pytest.fixture
def mock_llm_provider():
    """Create mock LLM provider."""
    mock = AsyncMock()
    mock.analyze_agent_trajectory = AsyncMock(
        return_value={
            "current_phase": "implementation",
            "trajectory_aligned": True,
            "alignment_score": 0.8,
            "alignment_issues": [],
            "needs_steering": False,
            "steering_type": None,
            "steering_recommendation": None,
            "trajectory_summary": "Agent implementing task successfully",
        }
    )
    return mock


def _task_dict(task: Task) -> dict:
    """Mirror Guardian._get_agent_task's dict shape (H-0d: returns
    primitives, not a detached ORM object) for tests that build a Task."""
    return {
        "id": task.id,
        "phase_id": task.phase_id,
        "workflow_id": task.workflow_id,
        "enriched_description": task.enriched_description,
        "raw_description": task.raw_description,
        "done_definition": task.done_definition,
    }


@pytest.fixture
def guardian(mock_db_manager, mock_agent_manager, mock_llm_provider):
    """Create Guardian instance with mocked dependencies."""
    return Guardian(
        db_manager=mock_db_manager,
        agent_manager=mock_agent_manager,
        llm_provider=mock_llm_provider,
    )


class TestGuardian:
    """Test the Guardian monitoring system."""

    @pytest.mark.asyncio
    async def test_analyze_agent_with_trajectory_success(
        self, guardian, mock_llm_provider
    ):
        """Test successful trajectory analysis of an agent."""
        # Setup
        agent = Agent(
            id="test-agent-1",
            current_task_id="task-1",
            tmux_session_name="agent-test-1",
        )

        mock_task = Task(
            id="task-1",
            raw_description="Implement authentication",
            enriched_description="Implement JWT authentication system",
            done_definition="Authentication working with tests",
        )

        # Mock accumulated context
        with patch.object(
            guardian,
            "_build_accumulated_context",
            return_value={
                "overall_goal": "Implement JWT authentication",
                "constraints": ["no external libraries"],
                "lifted_constraints": [],
                "standing_instructions": ["keep it simple"],
                "session_start": datetime.utcnow() - timedelta(minutes=5),
                "conversation_length": 10,
                "current_focus": "implementation",
            },
        ):
            with patch.object(guardian, "_get_agent_task", return_value=_task_dict(mock_task)):
                # Execute
                result = await guardian.analyze_agent_with_trajectory(
                    agent=agent,
                    tmux_output="Creating auth module...",
                    past_summaries=[],
                )

        # Assert
        assert result["agent_id"] == "test-agent-1"
        assert result["trajectory_aligned"] is True
        assert result["alignment_score"] == 0.8
        assert result["current_phase"] == "implementation"
        assert "JWT authentication" in result["accumulated_goal"]
        assert "no external libraries" in result["active_constraints"]

        # Verify LLM was called
        mock_llm_provider.analyze_agent_trajectory.assert_called_once()

    @pytest.mark.asyncio
    async def test_benign_session_id_error_stripped_before_llm_sees_it(
        self, guardian, mock_llm_provider
    ):
        """Regression: session-reuse launches (see cli_interface.py's
        ClaudeCodeAgent.get_launch_command) print "Error: Session ID X is
        already in use" as an expected, self-resolving artifact of trying
        --session-id before falling back to --resume. Fed raw to the
        Guardian LLM, this was misread as a live problem and produced a
        fabricated "fix your session conflict" steering message. It must be
        stripped from what the LLM sees, without altering the agent's real
        output around it."""
        agent = Agent(
            id="test-agent-1",
            current_task_id="task-1",
            tmux_session_name="agent-test-1",
        )
        mock_task = Task(
            id="task-1",
            raw_description="Validate the feature",
            enriched_description="Validate the feature against design",
            done_definition="Validation report written",
        )
        raw_output = (
            "some earlier output\n"
            "Error: Session ID fb7e5fdc-9568-52ef-b37b-e81a8fef240b is already in use.\n"
            "resumed conversation continues here"
        )

        with patch.object(
            guardian,
            "_build_accumulated_context",
            return_value={
                "overall_goal": "Validate the feature",
                "constraints": [],
                "lifted_constraints": [],
                "standing_instructions": [],
                "session_start": datetime.utcnow() - timedelta(minutes=5),
                "conversation_length": 1,
                "current_focus": "validation",
            },
        ):
            with patch.object(
                guardian, "_get_agent_task", return_value=_task_dict(mock_task)
            ):
                await guardian.analyze_agent_with_trajectory(
                    agent=agent,
                    tmux_output=raw_output,
                    past_summaries=[],
                )

        call_kwargs = mock_llm_provider.analyze_agent_trajectory.call_args.kwargs
        sent_output = call_kwargs["agent_output"]
        assert "already in use" not in sent_output
        assert "some earlier output" in sent_output
        assert "resumed conversation continues here" in sent_output

    @pytest.mark.asyncio
    async def test_analyze_agent_with_steering_needed(
        self, guardian, mock_llm_provider
    ):
        """Test when agent needs steering intervention."""
        # Setup - agent needs steering
        mock_llm_provider.analyze_agent_trajectory.return_value = {
            "current_phase": "implementation",
            "trajectory_aligned": False,
            "alignment_score": 0.3,
            "alignment_issues": ["Installing external packages"],
            "needs_steering": True,
            "steering_type": "violating_constraints",
            "steering_recommendation": "Remember: no external libraries allowed",
            "trajectory_summary": "Agent violating constraints",
        }

        agent = Agent(id="test-agent-2", current_task_id="task-2")
        mock_task = Task(
            id="task-2", enriched_description="Build API", done_definition="API working"
        )

        with patch.object(
            guardian,
            "_build_accumulated_context",
            return_value={
                "overall_goal": "Build API",
                "constraints": ["no external libraries"],
                "session_start": datetime.utcnow(),
            },
        ):
            with patch.object(guardian, "_get_agent_task", return_value=_task_dict(mock_task)):
                # Execute
                result = await guardian.analyze_agent_with_trajectory(
                    agent=agent, tmux_output="pip install requests", past_summaries=[]
                )

        # Assert steering needed
        assert result["trajectory_aligned"] is False
        assert result["needs_steering"] is True
        assert result["steering_type"] == "violating_constraints"
        assert result["steering_message"] == "Remember: no external libraries allowed"

    @pytest.mark.asyncio
    async def test_repeated_timeout_defaults_to_benign_until_threshold(
        self, guardian, mock_llm_provider
    ):
        """A single (or a couple) slow/over-streaming LLM call must still
        fall back to the benign default -- the timeout exists precisely so
        an occasional slow call doesn't wrongly flag a healthy agent."""
        import asyncio

        async def _hang(*args, **kwargs):
            raise asyncio.TimeoutError()

        agent = Agent(id="timeout-agent", current_task_id="task-x")
        mock_task = Task(id="task-x", enriched_description="Do work")

        with patch.object(
            guardian,
            "_build_accumulated_context",
            return_value={
                "overall_goal": "Do work",
                "constraints": [],
                "session_start": datetime.utcnow(),
            },
        ):
            with patch.object(
                guardian, "_get_agent_task", return_value=_task_dict(mock_task)
            ):
                with patch("asyncio.wait_for", side_effect=_hang):
                    result = await guardian.analyze_agent_with_trajectory(
                        agent=agent, tmux_output="...", past_summaries=[]
                    )

        assert result["trajectory_aligned"] is True
        assert result["needs_steering"] is False

    @pytest.mark.asyncio
    async def test_consecutive_timeouts_escalate_to_stuck_steering(
        self, guardian, mock_llm_provider
    ):
        """After GUARDIAN_TIMEOUT_ESCALATION_THRESHOLD consecutive timeouts,
        the timeout pattern itself must be treated as a stuck signal --
        needs_steering=True/steering_type='stuck' feeds the same nudge +
        auto-restart path a real stuck-trajectory detection would trigger.
        Regression test: previously this returned the benign "aligned,
        no steering needed" default forever, no matter how many times in a
        row the analysis itself failed to complete (observed live: an agent
        hard-stopped on a model error timed out 4+ times over 12 minutes
        with zero intervention)."""
        import asyncio

        from src.monitoring.guardian import GUARDIAN_TIMEOUT_ESCALATION_THRESHOLD

        async def _hang(*args, **kwargs):
            raise asyncio.TimeoutError()

        agent = Agent(id="timeout-agent-2", current_task_id="task-y")
        mock_task = Task(id="task-y", enriched_description="Do work")

        with patch.object(
            guardian,
            "_build_accumulated_context",
            return_value={
                "overall_goal": "Do work",
                "constraints": [],
                "session_start": datetime.utcnow(),
            },
        ):
            with patch.object(
                guardian, "_get_agent_task", return_value=_task_dict(mock_task)
            ):
                with patch("asyncio.wait_for", side_effect=_hang):
                    result = None
                    for _ in range(GUARDIAN_TIMEOUT_ESCALATION_THRESHOLD):
                        result = await guardian.analyze_agent_with_trajectory(
                            agent=agent, tmux_output="...", past_summaries=[]
                        )

        assert result["needs_steering"] is True
        assert result["steering_type"] == "stuck"
        assert result["trajectory_aligned"] is False

    @pytest.mark.asyncio
    async def test_successful_analysis_resets_timeout_counter(
        self, guardian, mock_llm_provider
    ):
        """A successful analysis between timeouts must reset the consecutive
        count -- an occasionally-slow model shouldn't accumulate toward
        escalation across unrelated cycles."""
        import asyncio

        from src.monitoring.guardian import GUARDIAN_TIMEOUT_ESCALATION_THRESHOLD

        async def _hang(*args, **kwargs):
            raise asyncio.TimeoutError()

        agent = Agent(id="timeout-agent-3", current_task_id="task-z")
        mock_task = Task(id="task-z", enriched_description="Do work")

        with patch.object(
            guardian,
            "_build_accumulated_context",
            return_value={
                "overall_goal": "Do work",
                "constraints": [],
                "session_start": datetime.utcnow(),
            },
        ):
            with patch.object(
                guardian, "_get_agent_task", return_value=_task_dict(mock_task)
            ):
                # One fewer than the threshold, then a real success.
                with patch("asyncio.wait_for", side_effect=_hang):
                    for _ in range(GUARDIAN_TIMEOUT_ESCALATION_THRESHOLD - 1):
                        await guardian.analyze_agent_with_trajectory(
                            agent=agent, tmux_output="...", past_summaries=[]
                        )

                result = await guardian.analyze_agent_with_trajectory(
                    agent=agent, tmux_output="...", past_summaries=[]
                )
                assert result["needs_steering"] is False  # real success, not escalation

                # Timing out again now should NOT immediately escalate --
                # the counter was reset by the success above.
                with patch("asyncio.wait_for", side_effect=_hang):
                    result = await guardian.analyze_agent_with_trajectory(
                        agent=agent, tmux_output="...", past_summaries=[]
                    )
                assert result["needs_steering"] is False

    @pytest.mark.asyncio
    async def test_guardian_caching(self, guardian):
        """Test that Guardian caches trajectory analysis."""
        agent = Agent(id="test-agent-3", current_task_id="task-3")
        mock_task = Task(id="task-3", enriched_description="Test task")

        # Provide complete accumulated context
        complete_context = {
            "overall_goal": "Test",
            "constraints": [],
            "lifted_constraints": [],
            "standing_instructions": [],
            "references": {},
            "conversation_length": 0,
            "session_start": datetime.utcnow(),
            "discovered_blockers": [],
        }

        with patch.object(
            guardian, "_build_accumulated_context", return_value=complete_context
        ):
            with patch.object(guardian, "_get_agent_task", return_value=_task_dict(mock_task)):
                await guardian.analyze_agent_with_trajectory(
                    agent=agent, tmux_output="test", past_summaries=[]
                )

        # Check cache
        assert "test-agent-3" in guardian.trajectory_cache
        cached = guardian.trajectory_cache["test-agent-3"]
        assert "analysis" in cached
        assert "timestamp" in cached

    @pytest.mark.asyncio
    async def test_steer_agent(self, guardian, mock_agent_manager, mock_db_manager):
        """Test steering message sent to agent."""
        agent = Agent(id="test-agent-4", current_task_id="task-4")

        # Mock database session
        mock_session = Mock()
        mock_db_manager.get_session.return_value = mock_session

        # Execute steering
        await guardian.steer_agent(
            agent=agent, steering_type="stuck", message="Try checking your imports"
        )

        # Verify message sent
        mock_agent_manager.send_message_to_agent.assert_called_once()
        call_args = mock_agent_manager.send_message_to_agent.call_args[0]
        assert call_args[0] == "test-agent-4"
        assert "GUARDIAN" in call_args[1]
        assert "Try checking your imports" in call_args[1]

        # Verify logged
        mock_session.add.assert_called()
        mock_session.commit.assert_called()

    @pytest.mark.asyncio
    async def test_build_accumulated_context(self, guardian, mock_db_manager):
        """Test building accumulated context from agent logs."""
        agent = Agent(id="test-agent-5", current_task_id="task-5")

        # Mock logs
        mock_logs = [
            AgentLog(
                agent_id="test-agent-5",
                log_type="input",
                message="Build auth without external libraries",
                created_at=datetime.utcnow() - timedelta(minutes=10),
                details={},
            ),
            AgentLog(
                agent_id="test-agent-5",
                log_type="output",
                message="I'll implement JWT from scratch",
                created_at=datetime.utcnow() - timedelta(minutes=9),
                details={},
            ),
        ]

        mock_task = Task(
            id="task-5",
            enriched_description="Build authentication",
            done_definition="Auth working",
        )

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = mock_logs
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            mock_task
        )
        mock_db_manager.get_session.return_value = mock_session

        # Execute
        context = await guardian._build_accumulated_context(agent, [])

        # Assert
        assert context["overall_goal"] == "Build authentication"
        assert context["done_definition"] == "Auth working"
        assert context["conversation_length"] == 2
        assert isinstance(context["constraints"], list)
        assert isinstance(context["session_start"], datetime)

    def test_extract_last_error(self, guardian):
        """Test error extraction from output."""
        output = """
        Working on task...
        Error: Module not found
        at line 42
        continuing...
        """

        error = guardian._extract_last_error(output)
        assert "Error: Module not found" in error
        assert "at line 42" in error

    @pytest.mark.asyncio
    async def test_handle_missing_task(self, guardian):
        """Test handling when task not found."""
        agent = Agent(id="test-agent-7", current_task_id="missing-task")

        with patch.object(guardian, "_get_agent_task", return_value=None):
            with patch.object(
                guardian,
                "_build_accumulated_context",
                return_value={"overall_goal": "Unknown"},
            ):
                result = await guardian.analyze_agent_with_trajectory(
                    agent=agent, tmux_output="test", past_summaries=[]
                )

        # Should return default analysis
        assert result["agent_id"] == "test-agent-7"
        assert (
            result["trajectory_summary"] == "LLM analysis unavailable - using default"
        )

    @pytest.mark.asyncio
    async def test_llm_failure_handling(self, guardian, mock_llm_provider):
        """Test handling when LLM analysis fails."""
        # Make LLM throw exception
        mock_llm_provider.analyze_agent_trajectory.side_effect = Exception("LLM Error")

        agent = Agent(id="test-agent-8", current_task_id="task-8")
        mock_task = Task(id="task-8", enriched_description="Test")

        with patch.object(
            guardian,
            "_build_accumulated_context",
            return_value={"overall_goal": "Test"},
        ):
            with patch.object(guardian, "_get_agent_task", return_value=_task_dict(mock_task)):
                result = await guardian.analyze_agent_with_trajectory(
                    agent=agent, tmux_output="test", past_summaries=[]
                )

        # Should return default analysis
        assert (
            result["trajectory_summary"] == "LLM analysis unavailable - using default"
        )
        assert result["trajectory_aligned"] is True  # Safe default

    def test_clear_agent_cache(self, guardian):
        """Test clearing agent cache."""
        agent_id = "test-agent-9"

        # Add to cache
        guardian.trajectory_cache[agent_id] = {"test": "data"}
        guardian.steering_history[agent_id] = [{"test": "history"}]

        # Clear cache
        guardian.clear_agent_cache(agent_id)

        # Verify cleared
        assert agent_id not in guardian.trajectory_cache
        assert agent_id not in guardian.steering_history


class TestSteerAgentGating:
    """Regression coverage for the incident where a single 'off_track'
    trajectory judgment interrupted (via a forced Esc keystroke) an agent's
    legitimate, in-progress file write. Two behaviors were added:
    1. Soft concerns (drifting/off_track/over_engineering/confused/
       violating_constraints) require the SAME type flagged on 2 consecutive
       calls before Guardian acts at all.
    2. Even once acted on, the interrupt keystroke only fires for stuck/idle
       — soft concerns get the message without interrupting anything.
    """

    @pytest.mark.asyncio
    async def test_stuck_acts_on_first_flag_with_interrupt(
        self, guardian, mock_agent_manager, mock_db_manager
    ):
        agent = Agent(id="agent-stuck", current_task_id="task-1")
        mock_db_manager.get_session.return_value = Mock()

        await guardian.steer_agent(agent=agent, steering_type="stuck", message="m")

        mock_agent_manager.send_recovery_keystrokes.assert_awaited_once_with(
            "agent-stuck"
        )
        mock_agent_manager.send_message_to_agent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idle_acts_on_first_flag_with_interrupt(
        self, guardian, mock_agent_manager, mock_db_manager
    ):
        agent = Agent(id="agent-idle", current_task_id="task-1")
        mock_db_manager.get_session.return_value = Mock()

        await guardian.steer_agent(agent=agent, steering_type="idle", message="m")

        mock_agent_manager.send_recovery_keystrokes.assert_awaited_once_with(
            "agent-idle"
        )
        mock_agent_manager.send_message_to_agent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_off_track_does_not_act_on_first_flag(
        self, guardian, mock_agent_manager, mock_db_manager
    ):
        agent = Agent(id="agent-drift", current_task_id="task-1")
        mock_db_manager.get_session.return_value = Mock()

        await guardian.steer_agent(agent=agent, steering_type="off_track", message="m")

        mock_agent_manager.send_recovery_keystrokes.assert_not_awaited()
        mock_agent_manager.send_message_to_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_off_track_acts_on_second_consecutive_flag_without_interrupt(
        self, guardian, mock_agent_manager, mock_db_manager
    ):
        agent = Agent(id="agent-drift2", current_task_id="task-1")
        mock_db_manager.get_session.return_value = Mock()

        await guardian.steer_agent(agent=agent, steering_type="off_track", message="m")
        await guardian.steer_agent(agent=agent, steering_type="off_track", message="m")

        # Confirmed on the 2nd consecutive flag — message sent, but no
        # interrupt keystroke, since off_track work may be legitimate and
        # finite (e.g. an in-progress file write).
        mock_agent_manager.send_recovery_keystrokes.assert_not_awaited()
        mock_agent_manager.send_message_to_agent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_different_type_resets_confirmation_count(
        self, guardian, mock_agent_manager, mock_db_manager
    ):
        agent = Agent(id="agent-drift3", current_task_id="task-1")
        mock_db_manager.get_session.return_value = Mock()

        await guardian.steer_agent(agent=agent, steering_type="off_track", message="m")
        # A different soft type shouldn't count toward off_track's confirmation
        await guardian.steer_agent(agent=agent, steering_type="confused", message="m")

        mock_agent_manager.send_message_to_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_confirmed_flag_state_is_cleared_after_acting(
        self, guardian, mock_agent_manager, mock_db_manager
    ):
        agent = Agent(id="agent-drift4", current_task_id="task-1")
        mock_db_manager.get_session.return_value = Mock()

        await guardian.steer_agent(agent=agent, steering_type="off_track", message="m")
        await guardian.steer_agent(agent=agent, steering_type="off_track", message="m")
        assert "agent-drift4" not in guardian._consecutive_flags


class TestGuardianPhaseContextUsesLiveRequiredOutput:
    """Regression: _get_phase_context fed the LLM (and, through its nudge
    message, the agent) the raw Phase.outputs column -- a per-workflow-
    instance snapshot taken at workflow-creation time from whatever
    workflow.yaml said then, and never refreshed afterward. A workflow
    created before an output-format change (e.g. the OKF single-file
    refactor collapsing a phase's json+md pair into one .md) kept telling
    Guardian to have the agent produce the OLD file(s) for its entire
    remaining run, not just its next retry. Must use
    load_phase_output_artifacts's required_output override, which IS read
    fresh from disk on every call, while still falling back to phase.outputs
    (preserving non-file descriptive text) for phases with no override."""

    @pytest.fixture
    def real_db_with_override(self, tmp_path, monkeypatch):
        from src.core.database import (
            DatabaseManager,
            Phase,
            Workflow,
            WorkflowDefinition,
        )

        db_path = tmp_path / "test.db"
        real_db = DatabaseManager(str(db_path))
        real_db.create_tables()
        monkeypatch.setattr(
            "src.core.database.DatabaseManager", lambda *a, **kw: real_db
        )

        workflows_dir = tmp_path / "workflows"
        (workflows_dir / "guardian_test_def").mkdir(parents=True)
        (workflows_dir / "guardian_test_def" / "workflow.yaml").write_text(
            "required_output:\n"
            "  architectural_review: review.md\n"
        )
        monkeypatch.setattr("src.workflow_registry._WORKFLOWS_DIR", workflows_dir)

        session = real_db.get_session()
        session.add(WorkflowDefinition(id="guardian_test_def", name="t"))
        session.add(
            Workflow(
                id="wf-1", name="t", phases_folder_path="/tmp",
                definition_id="guardian_test_def",
            )
        )
        import json as _json

        session.add(
            Phase(
                id="phase-1", workflow_id="wf-1", order=5,
                name="architectural_review", description="d",
                done_definitions=["x"],
                # The stale snapshot: this workflow was created back when
                # the phase still wrote a json+md pair.
                outputs=_json.dumps(
                    [
                        "review.md",
                        "architectural_review_result.json",
                    ]
                ),
            )
        )
        session.add(
            Phase(
                id="phase-2", workflow_id="wf-1", order=4,
                name="development", description="d",
                done_definitions=["x"],
                # No required_output override for this phase -- non-file
                # descriptive text like this must survive unchanged.
                outputs=_json.dumps(["source code in project path"]),
            )
        )
        session.commit()
        session.close()
        return real_db

    @pytest.mark.asyncio
    async def test_outputs_reflects_the_current_required_output_override(
        self, real_db_with_override, mock_agent_manager, mock_llm_provider
    ):
        guardian = Guardian(
            db_manager=real_db_with_override,
            agent_manager=mock_agent_manager,
            llm_provider=mock_llm_provider,
        )
        context = await guardian._get_phase_context("phase-1", "wf-1")
        assert context["outputs"] == ["review.md"]

    @pytest.mark.asyncio
    async def test_non_file_outputs_survive_for_a_phase_with_no_override(
        self, real_db_with_override, mock_agent_manager, mock_llm_provider
    ):
        """Sanity check the fix isn't overbroad: a phase with no
        required_output override (e.g. development) must keep its
        non-file descriptive text, not have it dropped by a strict
        required-files check."""
        guardian = Guardian(
            db_manager=real_db_with_override,
            agent_manager=mock_agent_manager,
            llm_provider=mock_llm_provider,
        )
        context = await guardian._get_phase_context("phase-2", "wf-1")
        assert context["outputs"] == ["source code in project path"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
