"""Regression: _build_and_start_pipeline_sdk hardcoded monitoring_interval=60
and sdk.start(timeout=60) even though it already reads get_config() for
default_cli_tool right above -- violates this project's own
no-hardcoded-timeouts convention (CLAUDE.md). Both now read from
hephaestus_config.yaml's autopilot/monitoring sections."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.autopilot.orchestrator import _build_and_start_pipeline_sdk


def test_uses_config_values_not_hardcoded_literals(tmp_path):
    fake_sdk_instance = MagicMock()
    fake_sdk_cls = MagicMock(return_value=fake_sdk_instance)

    fake_config = MagicMock()
    fake_config.agents.default_cli_tool = "claude"
    fake_config.monitoring.monitoring_interval_seconds = 123
    fake_config.autopilot.sdk_start_timeout_seconds = 456

    args = SimpleNamespace(in_process=True)
    logger = MagicMock()

    with patch(
        "src.autopilot.orchestrator.pipeline.get_config", return_value=fake_config
    ), patch("src.sdk.HephaestusSDK", fake_sdk_cls), patch(
        "src.autopilot.phases.AUTOPILOT_PHASES", []
    ), patch(
        "src.autopilot.phases.AUTOPILOT_WORKFLOW_CONFIG", {}
    ), patch(
        "src.autopilot.phases.AUTOPILOT_LAUNCH_TEMPLATE", ""
    ), patch(
        "src.workflow_registry.get_all_workflow_definitions", return_value=[]
    ):
        _build_and_start_pipeline_sdk(args, tmp_path, logger)

    assert fake_sdk_cls.call_args.kwargs["monitoring_interval"] == 123
    assert fake_sdk_instance.start.call_args.kwargs["timeout"] == 456
