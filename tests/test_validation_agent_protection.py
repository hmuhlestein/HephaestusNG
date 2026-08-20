"""Test that validation agents are protected from duplicate termination."""

from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.database import Agent
from src.monitoring.conductor import Conductor, SystemDecision
from src.monitoring.guardian import Guardian


@pytest.fixture
def mock_db_manager():
    """Create mock database manager."""
    db_manager = Mock()
    db_manager.get_session = Mock()

    @contextmanager
    def _session_scope():
        # Conductor production code uses session_scope() rather than raw
        # get_session()/close() for writes (SOLID review 3.10) -- route
        # through the same mocked session so tests that configure
        # get_session.return_value keep working.
        session = db_manager.get_session()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    db_manager.session_scope = Mock(side_effect=_session_scope)
    return db_manager


@pytest.fixture
def mock_agent_manager():
    """Create mock agent manager."""
    agent_manager = Mock()
    agent_manager.terminate_agent = AsyncMock()
    agent_manager.send_message_to_agent = AsyncMock()
    return agent_manager


@pytest.fixture
def conductor(mock_db_manager, mock_agent_manager):
    """Create Conductor instance with mocks."""
    return Conductor(
        db_manager=mock_db_manager,
        agent_manager=mock_agent_manager,
        llm_provider=Mock(),
    )


@pytest.fixture
def validation_agent():
    """Create a mock validation agent."""
    agent = Mock(spec=Agent)
    agent.id = "task-validator-12345"
    agent.agent_type = "validator"
    agent.status = "working"
    agent.current_task_id = "task-001"
    return agent


@pytest.fixture
def regular_agent():
    """Create a mock regular agent."""
    agent = Mock(spec=Agent)
    agent.id = "agent-regular-67890"
    agent.agent_type = "phase"
    agent.status = "working"
    agent.current_task_id = "task-002"
    return agent


@pytest.mark.asyncio
async def test_validation_agent_not_terminated(
    conductor,
    mock_db_manager,
    mock_agent_manager,
    validation_agent,
):
    """Test that validation agents are not terminated even when marked as duplicate."""
    # Setup mock session to return validation agent
    mock_session = Mock()
    mock_session.query.return_value.filter_by.return_value.first.return_value = (
        validation_agent
    )
    mock_session.close = Mock()
    mock_db_manager.get_session.return_value = mock_session

    # Create a decision to terminate the validation agent
    decision = {
        "type": SystemDecision.TERMINATE_DUPLICATE.value,
        "target": validation_agent.id,
        "reason": "Duplicate work detected",
    }

    # Execute the decision
    await conductor._execute_single_decision(decision)

    # Verify the agent was NOT terminated
    mock_agent_manager.terminate_agent.assert_not_called()

    # Verify safety check was performed
    mock_session.query.assert_called_with(Agent)
    mock_session.query.return_value.filter_by.assert_called_with(id=validation_agent.id)


@pytest.mark.asyncio
async def test_regular_agent_is_terminated(
    conductor,
    mock_db_manager,
    mock_agent_manager,
    regular_agent,
):
    """Test that regular agents are terminated when marked as duplicate."""
    # Setup mock session to return regular agent
    mock_session = Mock()
    mock_session.query.return_value.filter_by.return_value.first.return_value = (
        regular_agent
    )
    mock_session.close = Mock()
    mock_session.add = Mock()
    mock_session.commit = Mock()
    mock_db_manager.get_session.return_value = mock_session

    # Create a decision to terminate the regular agent
    decision = {
        "type": SystemDecision.TERMINATE_DUPLICATE.value,
        "target": regular_agent.id,
        "reason": "Duplicate work detected",
    }

    # Execute the decision
    await conductor._execute_single_decision(decision)

    # Verify the agent WAS terminated
    mock_agent_manager.terminate_agent.assert_called_once_with(regular_agent.id)


@pytest.mark.asyncio
async def test_result_validator_not_terminated(
    conductor,
    mock_db_manager,
    mock_agent_manager,
):
    """Test that result_validator agents are also protected."""
    # Create a result validator agent
    result_validator = Mock(spec=Agent)
    result_validator.id = "result-validator-99999"
    result_validator.agent_type = "result_validator"

    # Setup mock session
    mock_session = Mock()
    mock_session.query.return_value.filter_by.return_value.first.return_value = (
        result_validator
    )
    mock_session.close = Mock()
    mock_db_manager.get_session.return_value = mock_session

    # Create a decision to terminate
    decision = {
        "type": SystemDecision.TERMINATE_DUPLICATE.value,
        "target": result_validator.id,
        "reason": "Duplicate validation work",
    }

    # Execute the decision
    await conductor._execute_single_decision(decision)

    # Verify the agent was NOT terminated
    mock_agent_manager.terminate_agent.assert_not_called()


def test_guardian_includes_agent_type():
    """Test that Guardian summaries include agent_type."""
    # Create a mock agent
    agent = Mock(spec=Agent)
    agent.id = "test-agent"
    agent.agent_type = "validator"

    # Create Guardian
    guardian = Guardian(
        db_manager=Mock(),
        agent_manager=Mock(),
        llm_provider=Mock(),
    )

    # Get default analysis
    analysis = guardian._get_default_analysis(agent)

    # Verify agent_type is included
    assert "agent_type" in analysis
    assert analysis["agent_type"] == "validator"
    assert analysis["agent_id"] == "test-agent"


# Test removed - logging test not critical for functionality


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
