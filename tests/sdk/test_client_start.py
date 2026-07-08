"""Regression: HephaestusSDK.start()'s self-health-check race spawned a
second backend process.

run_continuous_pipeline (the autopilot orchestrator's main loop) always
executes from inside AutopilotService's background pipeline task, which is
itself part of the already-running backend process. But sdk.start() didn't
know that -- it always ran _check_backend_health(), a single 2-second-timeout
self-referential HTTP call to this same process's own /health endpoint.
Under load (many concurrent unrelated requests queued on the same event
loop) that call could spuriously time out even though the backend was
obviously running, causing sdk.start() to spawn a second run_server.py.
Both processes then bound port 8300 and drove independent AutopilotService
singletons against the same DB -- observed live as one process pausing a
workflow the other had just launched.

assume_backend_running lets the in-process caller skip that self-check
entirely instead of racing it.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.sdk.client import HephaestusSDK
from src.sdk.models import Phase as SDKPhase


def _make_sdk():
    phases = [
        SDKPhase(
            id=1,
            name="Test Phase",
            description="d",
            done_definitions=["done"],
            working_directory="/tmp",
        ),
    ]
    return HephaestusSDK(phases=phases, llm_provider="openrouter", openrouter_api_key="fake")


@pytest.fixture
def sdk():
    return _make_sdk()


class TestAssumeBackendRunning:
    def test_assume_backend_running_skips_health_precheck_and_spawn(self, sdk, tmp_path):
        with patch.object(sdk, "_check_qdrant_health", return_value=True), patch.object(
            sdk, "_check_backend_health", return_value=True
        ), patch("src.sdk.client.ProcessManager") as MockPM:
            mock_pm = MockPM.return_value
            mock_pm.is_process_alive.return_value = True

            sdk._start_headless(timeout=5, assume_backend_running=True)

            # Never spawned a competing backend process, regardless of
            # whatever the readiness-poll loop's own _check_backend_health
            # calls return -- that loop only waits, it never spawns.
            mock_pm.spawn_backend.assert_not_called()

    def test_assume_backend_running_skips_monitor_spawn_and_watchdog_thread(
        self, sdk, tmp_path
    ):
        """Regression: every pipeline run constructs a fresh HephaestusSDK
        (and therefore a fresh ProcessManager). Before this fix, each one
        called spawn_monitor()/start_watchdog() regardless of
        assume_backend_running -- the resulting per-instance watchdog
        THREAD (ProcessManager.start_watchdog, distinct from the external
        run_watchdog.py process) has no cleanup path for the in-process
        case, so they silently accumulated across every "play" click /
        design processed for the life of the backend. Each one
        independently polled its OWN local, incomplete ProcessManager.
        processes view and called spawn_backend()/spawn_monitor() again on
        a false "died" reading -- observed live as a fresh duplicate
        backend or monitor process appearing every ~10-30s, indefinitely,
        long after any single pipeline run's sdk.start() call had
        returned."""
        with patch.object(sdk, "_check_qdrant_health", return_value=True), patch.object(
            sdk, "_check_backend_health", return_value=True
        ), patch("src.sdk.client.ProcessManager") as MockPM:
            mock_pm = MockPM.return_value
            mock_pm.is_process_alive.return_value = True

            sdk._start_headless(timeout=5, assume_backend_running=True)

            mock_pm.spawn_monitor.assert_not_called()
            mock_pm.start_watchdog.assert_not_called()

    def test_default_still_spawns_when_backend_unhealthy(self, sdk):
        """Preserves existing behavior for the standalone CLI path
        (scripts/autopilot.sh), where the backend genuinely might not be
        running yet and spawning it is correct."""
        with patch.object(sdk, "_check_qdrant_health", return_value=True), patch.object(
            sdk, "_check_backend_health", return_value=False
        ), patch("src.sdk.client.ProcessManager") as MockPM, patch(
            "src.cli.utils.is_monitor_running", return_value=True
        ):
            mock_pm = MockPM.return_value
            mock_pm.is_process_alive.return_value = True

            with pytest.raises(Exception):
                # _check_backend_health always False -> polling loop times
                # out -> HephaestusStartupError. We only care that spawn_backend
                # was attempted before that.
                sdk._start_headless(timeout=1)

            mock_pm.spawn_backend.assert_called_once()

    def test_default_skips_spawn_when_backend_already_healthy(self, sdk):
        """Without assume_backend_running, a genuinely healthy backend is
        still detected via the real check and spawn is skipped -- this
        param only changes behavior when the self-check would otherwise be
        consulted, not the outcome when it succeeds."""
        with patch.object(sdk, "_check_qdrant_health", return_value=True), patch.object(
            sdk, "_check_backend_health", return_value=True
        ), patch("src.sdk.client.ProcessManager") as MockPM, patch(
            "src.cli.utils.is_monitor_running", return_value=True
        ):
            mock_pm = MockPM.return_value
            mock_pm.is_process_alive.return_value = True

            sdk._start_headless(timeout=5, assume_backend_running=False)

            mock_pm.spawn_backend.assert_not_called()

    def test_default_still_starts_watchdog(self, sdk):
        """The standalone CLI path (assume_backend_running=False) must
        still get the normal ProcessManager watchdog thread -- only the
        in-process AutopilotService path skips it."""
        with patch.object(sdk, "_check_qdrant_health", return_value=True), patch.object(
            sdk, "_check_backend_health", return_value=True
        ), patch("src.sdk.client.ProcessManager") as MockPM, patch(
            "src.cli.utils.is_monitor_running", return_value=True
        ):
            mock_pm = MockPM.return_value
            mock_pm.is_process_alive.return_value = True

            sdk._start_headless(timeout=5, assume_backend_running=False)

            mock_pm.start_watchdog.assert_called_once()


class TestRunContinuousPipelinePassesInProcessFlag:
    """service.py's args carries in_process=True; orchestrator.py must
    forward it as assume_backend_running. The standalone CLI's argparse
    Namespace has no such attribute -- getattr(..., False) must default
    correctly rather than raising.

    sdk.start is made to raise immediately so run_continuous_pipeline exits
    via its own `except Exception: sys.exit(1)` right after the call under
    test, instead of falling through into the real polling loop (which
    would sleep/loop for real and hang the test).
    """

    def _run_and_capture_start_kwargs(self, args) -> dict:
        from src.autopilot import orchestrator

        fake_sdk = MagicMock()
        fake_sdk.start.side_effect = RuntimeError("stop here — test boundary")

        with patch("src.sdk.HephaestusSDK", return_value=fake_sdk), patch(
            "src.autopilot.orchestrator.get_config"
        ) as mock_cfg, patch(
            "src.autopilot.orchestrator.PersistentPipelineState"
        ) as MockState, patch(
            "src.workflow_registry.get_all_workflow_definitions", return_value=[]
        ):
            mock_cfg.return_value.default_cli_tool = "claude"
            MockState.return_value.load.return_value = (
                orchestrator.PipelineState(),
                set(),
            )
            MockState.return_value.has_incomplete_work.return_value = False

            with pytest.raises(SystemExit):
                orchestrator.run_continuous_pipeline(args)

        assert fake_sdk.start.called
        _, kwargs = fake_sdk.start.call_args
        return kwargs

    def test_in_process_flag_forwarded_as_assume_backend_running(self, tmp_path):
        import argparse

        args = argparse.Namespace(
            project_path=str(tmp_path / "proj"),
            design_queue=str(tmp_path / "proj" / ".hephaestus" / "designs"),
            max_iterations=3,
            in_process=True,
        )

        kwargs = self._run_and_capture_start_kwargs(args)
        assert kwargs.get("assume_backend_running") is True

    def test_standalone_cli_args_default_to_false(self, tmp_path):
        """A bare argparse.Namespace without in_process (the real shape
        main()'s parser.parse_args() produces) must default assume_backend_
        running to False, not raise AttributeError."""
        import argparse

        args = argparse.Namespace(
            project_path=str(tmp_path / "proj"),
            design_queue=str(tmp_path / "proj" / ".hephaestus" / "designs"),
            max_iterations=3,
            # no in_process attribute -- matches main()'s real argparse output
        )

        kwargs = self._run_and_capture_start_kwargs(args)
        assert kwargs.get("assume_backend_running") is False
