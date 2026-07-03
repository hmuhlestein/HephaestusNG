"""Tests for AgentDispatchService — the agent dispatch context
builder extracted from server.py (SOLID review finding 1.2/1.3).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestGetPhaseCliConfig:
    """Tests for AgentDispatchService.get_phase_cli_config."""

    def test_returns_defaults_for_none_phase_id(self):
        from src.services.agent_dispatch_service import AgentDispatchService

        session = MagicMock()
        result = AgentDispatchService.get_phase_cli_config(session, None)

        assert result["cli_tool"] is None
        assert result["cli_model"] is None
        assert result["glm_token_env"] is None
        assert result["thinking_level"] is None
        assert result["working_directory"] is None

    def test_returns_phase_config_when_phase_exists(self):
        from src.services.agent_dispatch_service import AgentDispatchService

        session = MagicMock()
        mock_phase = MagicMock()
        mock_phase.cli_tool = "claude"
        mock_phase.cli_model = "sonnet"
        mock_phase.glm_api_token_env = "GLM_API_TOKEN"
        mock_phase.thinking_level = "medium"
        mock_phase.working_directory = "/tmp/test-project"

        session.query.return_value.filter_by.return_value.first.return_value = mock_phase

        result = AgentDispatchService.get_phase_cli_config(session, "phase-123")

        assert result["cli_tool"] == "claude"
        assert result["cli_model"] == "sonnet"
        assert result["glm_token_env"] == "GLM_API_TOKEN"
        assert result["thinking_level"] == "medium"
        assert result["working_directory"] == "/tmp/test-project"

    def test_returns_defaults_when_phase_not_found(self):
        from src.services.agent_dispatch_service import AgentDispatchService

        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = None

        result = AgentDispatchService.get_phase_cli_config(session, "nonexistent-phase")

        assert result["cli_tool"] is None
        assert result["cli_model"] is None


class TestMarkAssigned:
    """Tests for AgentDispatchService.mark_assigned."""

    @patch("src.core.app_context.get_app_state")
    def test_updates_task_status(self, mock_get_state):
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_get_state.return_value = mock_state

        mock_task = MagicMock()
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = mock_task
        mock_state.db_manager.get_session.return_value = mock_session

        AgentDispatchService.mark_assigned("task-123", "agent-456")

        assert mock_task.assigned_agent_id == "agent-456"
        assert mock_task.status == "assigned"
        mock_session.commit.assert_called_once()
        mock_session.close.assert_called_once()

    @patch("src.core.app_context.get_app_state")
    def test_rolls_back_on_commit_failure(self, mock_get_state):
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_get_state.return_value = mock_state

        mock_task = MagicMock()
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = mock_task
        mock_session.commit.side_effect = Exception("DB locked")
        mock_state.db_manager.get_session.return_value = mock_session

        with pytest.raises(Exception, match="DB locked"):
            AgentDispatchService.mark_assigned("task-123", "agent-456")

        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()

    @patch("src.core.app_context.get_app_state")
    def test_accepts_external_session(self, mock_get_state):
        """FIX #16: When session is provided, should not open a new one."""
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_get_state.return_value = mock_state

        mock_task = MagicMock()
        external_session = MagicMock()
        external_session.query.return_value.filter_by.return_value.first.return_value = mock_task

        AgentDispatchService.mark_assigned("task-123", "agent-456", session=external_session)

        assert mock_task.assigned_agent_id == "agent-456"
        external_session.commit.assert_called_once()
        # Should NOT have opened a new session
        mock_state.db_manager.get_session.assert_not_called()
        # Should NOT have closed the external session
        external_session.close.assert_not_called()
