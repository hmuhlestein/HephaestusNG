"""Tests for AgentDispatchService — the agent dispatch context
builder extracted from server.py (SOLID review finding 1.2/1.3).
"""

from unittest.mock import MagicMock, patch

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

    @patch("src.core.app_context.get_app_state")
    def test_does_not_overwrite_a_task_that_already_finished_during_dispatch(
        self, mock_get_state
    ):
        """Regression, observed live (task b938bee7-b327-4e69-9ba6-5ace277c1314):
        every caller dispatches an agent (an awaited call than can run for
        over a minute -- worktree setup, tmux launch, ready-wait, prompt
        delivery) and only calls mark_assigned AFTER that returns. The
        agent itself is live the whole time that call is in flight and can
        legitimately finish and call update_task_status first -- an
        arbitration task recognizing a duplicate dispatch and reporting
        "done" within seconds is exactly this shape. Overwriting status
        back to "assigned" here would clobber that real "done" outcome
        with a fresh started_at and an assigned_agent_id pointing at an
        agent already terminated by the time this runs -- indistinguishable
        from a real in-flight task to every self-heal path, so the
        pipeline never advances past it."""
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_get_state.return_value = mock_state

        mock_task = MagicMock()
        mock_task.status = "done"
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = mock_task
        mock_state.db_manager.get_session.return_value = mock_session

        AgentDispatchService.mark_assigned("task-123", "agent-456")

        mock_session.commit.assert_not_called()
        assert mock_task.assigned_agent_id != "agent-456"
        assert mock_task.status == "done"

    @patch("src.core.app_context.get_app_state")
    def test_does_not_overwrite_a_failed_task_either(self, mock_get_state):
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_get_state.return_value = mock_state

        mock_task = MagicMock()
        mock_task.status = "failed"
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = mock_task
        mock_state.db_manager.get_session.return_value = mock_session

        AgentDispatchService.mark_assigned("task-123", "agent-456")

        mock_session.commit.assert_not_called()
        assert mock_task.status == "failed"

    @patch("src.core.app_context.get_app_state")
    def test_still_assigns_a_genuinely_pending_task(self, mock_get_state):
        """Confirms the fix is scoped to terminal states only -- the
        overwhelming normal case (task still pending/in_progress when
        dispatch confirms) must be unaffected."""
        from src.services.agent_dispatch_service import AgentDispatchService

        mock_state = MagicMock()
        mock_get_state.return_value = mock_state

        mock_task = MagicMock()
        mock_task.status = "pending"
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = mock_task
        mock_state.db_manager.get_session.return_value = mock_session

        AgentDispatchService.mark_assigned("task-123", "agent-456")

        assert mock_task.assigned_agent_id == "agent-456"
        assert mock_task.status == "assigned"
        mock_session.commit.assert_called_once()
