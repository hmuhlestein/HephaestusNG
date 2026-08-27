"""Regression: three blocking-DB-query methods were called directly (or via
a raw session_scope() block) inside async def _monitoring_cycle/
_save_conductor_analysis with no run_in_executor offload -- stalling the
event loop on every monitoring cycle."""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest


@pytest.fixture
def mock_db():
    from src.core.database import DatabaseManager

    mock = Mock(spec=DatabaseManager)
    mock.session_scope = MagicMock()
    return mock


@pytest.fixture
def mock_agent_manager():
    mock = Mock()
    mock.tmux_server = Mock()
    mock.tmux_server.has_session.return_value = True
    mock.get_agent_output = Mock(return_value="Agent working on task...")
    mock.send_message_to_agent = AsyncMock()
    mock.send_recovery_keystrokes = AsyncMock(return_value=True)
    mock.get_active_agents = Mock(return_value=[])
    return mock


@pytest.fixture
def make_monitoring_loop(mock_db, mock_agent_manager):
    from unittest.mock import patch

    from src.monitoring.monitor import MonitoringLoop

    with patch("src.monitoring.monitor.get_config") as mock_cfg:
        mock_cfg.return_value = Mock(stuck_detection_minutes=10, agent_timeout_minutes=60)
        ml = MonitoringLoop(
            db_manager=mock_db, agent_manager=mock_agent_manager, llm_provider=AsyncMock(),
        )
    return ml


@pytest.mark.asyncio
async def test_monitoring_cycle_offloads_workflow_switch_and_diagnostics(
    make_monitoring_loop, mock_db, monkeypatch
):
    mock_db.get_session.return_value.query.return_value.filter_by.return_value.all.return_value = []

    make_monitoring_loop._maybe_switch_tracked_workflow = Mock()
    make_monitoring_loop._log_active_workflow_diagnostics = Mock()
    make_monitoring_loop._detect_credit_exhausted = AsyncMock(return_value=False)
    make_monitoring_loop._audit_system_health = AsyncMock()

    fake_loop = MagicMock()

    async def run_now(_executor, func, *args):
        return func(*args)

    fake_loop.run_in_executor = AsyncMock(side_effect=run_now)
    monkeypatch.setattr("asyncio.get_event_loop", lambda: fake_loop)

    await make_monitoring_loop._monitoring_cycle()

    called_funcs = [c.args[1] for c in fake_loop.run_in_executor.call_args_list]
    assert make_monitoring_loop._maybe_switch_tracked_workflow in called_funcs
    assert make_monitoring_loop._log_active_workflow_diagnostics in called_funcs


@pytest.mark.asyncio
async def test_save_conductor_analysis_offloads_session_write(
    make_monitoring_loop, monkeypatch
):
    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value=None)
    monkeypatch.setattr("asyncio.get_event_loop", lambda: fake_loop)

    analysis = {"system_status": "healthy", "duplicates": [], "decisions": []}
    await make_monitoring_loop._save_conductor_analysis(analysis)

    fake_loop.run_in_executor.assert_called_once()
    executor_arg, func_arg, analysis_arg = fake_loop.run_in_executor.call_args.args
    assert executor_arg is None
    assert func_arg == make_monitoring_loop._save_conductor_analysis_sync
    assert analysis_arg == analysis
