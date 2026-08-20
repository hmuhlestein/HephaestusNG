"""Test the Agent Trajectory Monitoring System."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.database import Agent, Task
from src.monitoring.conductor import Conductor
from src.monitoring.guardian import Guardian


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
    mock.send_message_to_agent = AsyncMock()
    # steer_agent() only interrupts (send_recovery_keystrokes) for
    # stuck/idle steering types — must be an AsyncMock, it's awaited.
    mock.send_recovery_keystrokes = AsyncMock(return_value=True)
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
            "progress_estimate": 60,
            "needs_steering": False,
            "steering_type": None,
            "steering_recommendation": None,
            "trajectory_summary": "Agent implementing task successfully",
        }
    )
    return mock


class TestGuardian:
    """Test Guardian monitoring with trajectory thinking."""

    @pytest.mark.asyncio
    async def test_guardian_trajectory_analysis(
        self,
        mock_db_manager,
        mock_agent_manager,
        mock_llm_provider,
    ):
        """Test Guardian analyzes agent with trajectory thinking."""
        # Setup Guardian
        guardian = Guardian(
            db_manager=mock_db_manager,
            agent_manager=mock_agent_manager,
            llm_provider=mock_llm_provider,
        )

        # Create test agent
        agent = Agent(
            id="test-agent-1",
            current_task_id="task-1",
            tmux_session_name="agent-test-1",
        )

        # Mock task retrieval
        mock_task = Task(
            id="task-1",
            raw_description="Implement authentication",
            enriched_description="Implement JWT authentication system",
            done_definition="Authentication working with tests",
        )

        with patch.object(guardian, "_get_agent_task", return_value=_task_dict(mock_task)):
            with patch.object(
                guardian,
                "_build_accumulated_context",
                return_value={
                    "overall_goal": "Implement JWT authentication",
                    "constraints": ["no external libraries"],
                    "session_start": datetime.utcnow() - timedelta(minutes=5),
                    "conversation_length": 3,
                    "session_duration": "0:05:00",
                },
            ):
                # Perform analysis
                result = await guardian.analyze_agent_with_trajectory(
                    agent=agent,
                    tmux_output="Creating auth module...",
                    past_summaries=[],
                )

        # Verify results
        assert result["agent_id"] == "test-agent-1"
        assert result["trajectory_aligned"] is True
        assert result["alignment_score"] == 0.8
        assert result["current_phase"] == "implementation"

    @pytest.mark.asyncio
    async def test_guardian_detects_constraint_violation(
        self,
        mock_db_manager,
        mock_agent_manager,
        mock_llm_provider,
    ):
        """Test Guardian detects constraint violations."""
        guardian = Guardian(
            db_manager=mock_db_manager,
            agent_manager=mock_agent_manager,
            llm_provider=mock_llm_provider,
        )

        agent = Agent(
            id="test-agent-2",
            current_task_id="task-2",
        )

        mock_task = Task(
            id="task-2",
            enriched_description="Build simple API",
            done_definition="API endpoints working",
        )

        # Mock LLM to return misaligned analysis
        mock_llm_provider.analyze_agent_trajectory.return_value = {
            "current_phase": "implementation",
            "trajectory_aligned": False,
            "alignment_score": 0.3,
            "alignment_issues": [
                "Installing packages violates: no external libraries"
            ],
            "needs_steering": True,
            "steering_type": "constraint_violation",
            "steering_recommendation": "Stop installing packages",
            "trajectory_summary": "Agent violating constraints",
        }

        # Setup to detect violation
        with patch.object(guardian, "_get_agent_task", return_value=_task_dict(mock_task)):
            with patch.object(
                guardian,
                "_build_accumulated_context",
                return_value={
                    "overall_goal": "Build simple API",
                    "constraints": ["no external libraries", "keep it simple"],
                    "session_start": datetime.utcnow(),
                    "conversation_length": 4,
                    "session_duration": "0:01:00",
                },
            ):
                result = await guardian.analyze_agent_with_trajectory(
                    agent=agent,
                    tmux_output="pip install requests flask sqlalchemy",
                    past_summaries=[],
                )

        # Should detect misalignment
        assert result["trajectory_aligned"] is False
        assert result["alignment_score"] < 0.5

    @pytest.mark.asyncio
    async def test_guardian_steering_decision(
        self,
        mock_db_manager,
        mock_agent_manager,
        mock_llm_provider,
    ):
        """Test Guardian makes appropriate steering decisions."""
        guardian = Guardian(
            db_manager=mock_db_manager,
            agent_manager=mock_agent_manager,
            llm_provider=mock_llm_provider,
        )

        # Test steering for stuck agent
        agent = Agent(id="test-agent-3", current_task_id="task-3")

        # Mock being stuck
        mock_llm_provider.analyze_agent_trajectory.return_value = {
            "current_phase": "implementation",
            "trajectory_aligned": False,
            "alignment_score": 0.4,
            "alignment_issues": ["Stuck on same error for 5 minutes"],
            "progress_estimate": 30,
            "needs_steering": True,
            "steering_type": "stuck",
            "steering_recommendation": "Check your imports",
            "trajectory_summary": "Agent stuck on error",
        }

        await guardian.steer_agent(
            agent=agent,
            steering_type="stuck",
            message="The error suggests missing import. Check the top of the file.",
        )

        # Verify steering message sent
        mock_agent_manager.send_message_to_agent.assert_called_once()
        call_args = mock_agent_manager.send_message_to_agent.call_args
        assert "GUARDIAN" in call_args[0][1]


class TestConductor:
    """Test Conductor system orchestration."""

    def _make_mock_llm(self):
        """Create a properly configured mock LLM provider."""
        from unittest.mock import AsyncMock, MagicMock
        mock_llm = MagicMock()
        mock_llm.analyze_system_coherence = AsyncMock()
        mock_llm.get_model_for_component = MagicMock(return_value="test-model")
        return mock_llm

    @pytest.mark.asyncio
    async def test_conductor_detects_duplicates(
        self,
        mock_db_manager,
        mock_agent_manager,
    ):
        """Test Conductor detects duplicate work."""
        # Mock LLM provider
        mock_llm = self._make_mock_llm()
        mock_llm.analyze_system_coherence.return_value = {
            "duplicates": [
                {
                    "agent1": "agent-1",
                    "agent2": "agent-2",
                    "similarity": 0.9,
                    "description": "Both working on authentication",
                }
            ],
            "coherence_score": 0.6,
            "termination_recommendations": [],
            "coordination_needs": [],
        }

        conductor = Conductor(
            db_manager=mock_db_manager,
            agent_manager=mock_agent_manager,
            llm_provider=mock_llm,
        )

        # Create Guardian summaries showing duplicate work
        summaries = [
            {
                "agent_id": "agent-1",
                "summary": "Implementing authentication module",
                "accumulated_goal": "Build JWT authentication system",
                "current_phase": "implementation",
                "trajectory_aligned": True,
            },
            {
                "agent_id": "agent-2",
                "summary": "Creating auth system with JWT",
                "accumulated_goal": "Implement JWT auth module",
                "current_phase": "implementation",
                "trajectory_aligned": True,
            },
            {
                "agent_id": "agent-3",
                "summary": "Building user profile API",
                "accumulated_goal": "Create user profile endpoints",
                "current_phase": "planning",
                "trajectory_aligned": True,
            },
        ]

        result = await conductor.analyze_system_state(summaries)

        # Should detect agents 1 and 2 doing similar work
        assert len(result["duplicates"]) > 0
        duplicate = result["duplicates"][0]
        assert "agent-1" in [duplicate["agent1"], duplicate["agent2"]]
        assert "agent-2" in [duplicate["agent1"], duplicate["agent2"]]

    @pytest.mark.asyncio
    async def test_conductor_system_coherence(
        self,
        mock_db_manager,
        mock_agent_manager,
    ):
        """Test Conductor evaluates system coherence."""
        # Mock LLM provider
        mock_llm = self._make_mock_llm()
        mock_llm.analyze_system_coherence.return_value = {
            "duplicates": [],
            "coherence_score": 0.5,
            "termination_recommendations": [],
            "coordination_needs": [],
        }

        conductor = Conductor(
            db_manager=mock_db_manager,
            agent_manager=mock_agent_manager,
            llm_provider=mock_llm,
        )

        # Mix of aligned and misaligned agents
        summaries = [
            {
                "agent_id": "agent-1",
                "summary": "On track with task",
                "trajectory_aligned": True,
                "needs_steering": False,
            },
            {
                "agent_id": "agent-2",
                "summary": "Drifting from goal",
                "trajectory_aligned": False,
                "needs_steering": True,
            },
            {
                "agent_id": "agent-3",
                "summary": "Stuck on error",
                "trajectory_aligned": False,
                "needs_steering": True,
            },
        ]

        result = await conductor.analyze_system_state(summaries)

        # System coherence should be degraded
        coherence = result["coherence"]
        assert coherence["score"] < 0.7  # Low due to misaligned agents

    @pytest.mark.asyncio
    async def test_conductor_makes_decisions(
        self,
        mock_db_manager,
        mock_agent_manager,
    ):
        """Test Conductor makes appropriate system decisions."""
        # Mock LLM provider
        mock_llm = self._make_mock_llm()
        mock_llm.analyze_system_coherence.return_value = {
            "duplicates": [
                {
                    "agent1": "agent-dup-1",
                    "agent2": "agent-dup-2",
                    "similarity": 0.9,
                    "description": "Both building REST API",
                }
            ],
            "coherence_score": 0.5,
            "termination_recommendations": [
                {
                    "agent_id": "agent-dup-2",
                    "reason": "Duplicate of agent-dup-1",
                    "type": "terminate_duplicate",
                }
            ],
            "coordination_needs": [],
        }

        conductor = Conductor(
            db_manager=mock_db_manager,
            agent_manager=mock_agent_manager,
            llm_provider=mock_llm,
        )

        # Setup scenario requiring decisions
        summaries = [
            {
                "agent_id": "agent-dup-1",
                "accumulated_goal": "Build API",
                "summary": "Creating REST API",
                "current_phase": "implementation",
            },
            {
                "agent_id": "agent-dup-2",
                "accumulated_goal": "Build API",
                "summary": "Implementing REST endpoints",
                "current_phase": "implementation",
            },
        ]

        result = await conductor.analyze_system_state(summaries)

        # Should have termination recommendations
        decisions = result["decisions"]
        assert len(decisions) > 0


@pytest.mark.asyncio
async def test_full_monitoring_cycle():
    """Test complete monitoring cycle with Guardian and Conductor."""
    # This would be an integration test with all components
    # For brevity, showing the structure:

    # 1. Setup monitoring loop with trajectory components
    # 2. Add test agents with various states
    # 3. Run one monitoring cycle
    # 4. Verify:
    #    - Guardian analyses performed for each agent
    #    - Steering messages sent where needed
    #    - Conductor analyzed system state
    #    - Duplicates detected and handled
    #    - System coherence evaluated

    pass  # Full implementation would require more setup


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v"])
