"""Tests for ProcessWatchdog.check_duplicate_port_listeners.

Regression: a stale standalone `python -m src.autopilot.orchestrator` CLI
process, left running for hours, spawned a second backend process bound to
the same port as the tracked one (its own self-health-check spuriously
failed against a momentarily-busy backend, so it concluded "not running"
and spawned a competitor). Two independent AutopilotService singletons then
raced against the same DB -- one paused a workflow the other had just
started, and a task got assigned by one process's agent manager but never
picked back up. Neither the existing PID-liveness check (which only
verifies whether the *tracked* PID is alive) nor assume_backend_running
(which only covers the in-process AutopilotService path) catches a rogue
process like that.
"""

from unittest.mock import MagicMock, patch

from src.cli.commands.start import ProcessWatchdog


class TestCheckDuplicatePortListeners:
    def test_single_listener_does_nothing(self):
        watchdog = ProcessWatchdog()
        with patch("subprocess.run") as mock_run, patch(
            "src.cli.commands.start.read_pid", return_value=111
        ), patch("os.kill") as mock_kill:
            mock_run.return_value = MagicMock(stdout="111\n")
            watchdog.check_duplicate_port_listeners(8300)

        mock_kill.assert_not_called()

    def test_no_listeners_does_nothing(self):
        watchdog = ProcessWatchdog()
        with patch("subprocess.run") as mock_run, patch(
            "os.kill"
        ) as mock_kill:
            mock_run.return_value = MagicMock(stdout="")
            watchdog.check_duplicate_port_listeners(8300)

        mock_kill.assert_not_called()

    def test_kills_untracked_duplicate_keeps_tracked(self):
        watchdog = ProcessWatchdog()
        with patch("subprocess.run") as mock_run, patch(
            "src.cli.commands.start.read_pid", return_value=111
        ), patch("os.kill") as mock_kill:
            mock_run.return_value = MagicMock(stdout="111\n222\n")
            watchdog.check_duplicate_port_listeners(8300)

        mock_kill.assert_called_once()
        killed_pid = mock_kill.call_args[0][0]
        assert killed_pid == 222

    def test_keeps_lowest_pid_when_tracked_pid_not_among_listeners(self):
        """Edge case: the tracked PID isn't among the port's listeners at
        all (e.g. a stale PID file) -- keep the lowest (oldest/first-
        started) PID rather than killing everything, so the service is
        never left with zero instances."""
        watchdog = ProcessWatchdog()
        with patch("subprocess.run") as mock_run, patch(
            "src.cli.commands.start.read_pid", return_value=999
        ), patch("os.kill") as mock_kill:
            mock_run.return_value = MagicMock(stdout="111\n222\n")
            watchdog.check_duplicate_port_listeners(8300)

        mock_kill.assert_called_once()
        assert mock_kill.call_args[0][0] == 222

    def test_lsof_failure_does_not_raise(self):
        watchdog = ProcessWatchdog()
        with patch("subprocess.run", side_effect=OSError("lsof not found")):
            watchdog.check_duplicate_port_listeners(8300)  # should not raise

    def test_lsof_call_filters_to_listen_sockets_only(self):
        """Regression: plain `lsof -ti :port` (no -sTCP:LISTEN) also
        matches outbound CLIENT connections to that port -- e.g. an
        in-flight curl call, the frontend's Vite API proxy, or the
        monitor's own health polling. Without the filter, every
        short-lived client process making a request at the moment this
        check ran got misidentified as a rogue duplicate SERVER and
        killed. Observed live: a fresh, unrelated PID "duplicate"
        appearing and getting killed every single watchdog cycle,
        indefinitely, long after the actual backend-duplication bug
        (assume_backend_running) was already fixed."""
        watchdog = ProcessWatchdog()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            watchdog.check_duplicate_port_listeners(8300)

        args = mock_run.call_args[0][0]
        assert "-sTCP:LISTEN" in args

    def test_kill_failure_on_one_pid_does_not_stop_others(self):
        watchdog = ProcessWatchdog()

        def kill_side_effect(pid, sig):
            if pid == 222:
                raise OSError("no such process")

        with patch("subprocess.run") as mock_run, patch(
            "src.cli.commands.start.read_pid", return_value=111
        ), patch("os.kill", side_effect=kill_side_effect) as mock_kill:
            mock_run.return_value = MagicMock(stdout="111\n222\n333\n")
            watchdog.check_duplicate_port_listeners(8300)

        killed_pids = {call[0][0] for call in mock_kill.call_args_list}
        assert killed_pids == {222, 333}


class TestCheckDuplicateMonitorProcesses:
    """The monitor doesn't bind a port, so this uses `pgrep -f
    run_monitor.py` instead of `lsof`. Regression: observed live
    immediately after a `heph restart` -- two run_monitor.py processes
    ended up running simultaneously, most likely the CLI's own
    spawn_monitor racing the in-process AutopilotService's sdk.start()
    call through is_monitor_running()'s pgrep check before either process
    was visible to the other's check yet."""

    def test_single_monitor_does_nothing(self):
        watchdog = ProcessWatchdog()
        with patch("subprocess.run") as mock_run, patch(
            "src.cli.commands.start.read_pid", return_value=111
        ), patch("os.kill") as mock_kill:
            mock_run.return_value = MagicMock(stdout="111\n")
            watchdog.check_duplicate_monitor_processes()

        mock_kill.assert_not_called()

    def test_kills_untracked_duplicate_monitor(self):
        watchdog = ProcessWatchdog()
        with patch("subprocess.run") as mock_run, patch(
            "src.cli.commands.start.read_pid", return_value=111
        ), patch("os.kill") as mock_kill:
            mock_run.return_value = MagicMock(stdout="111\n222\n")
            watchdog.check_duplicate_monitor_processes()

        mock_kill.assert_called_once()
        assert mock_kill.call_args[0][0] == 222

    def test_pgrep_call_targets_run_monitor(self):
        watchdog = ProcessWatchdog()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            watchdog.check_duplicate_monitor_processes()

        args = mock_run.call_args[0][0]
        assert args[0] == "pgrep"
        assert "run_monitor.py" in args

    def test_pgrep_failure_does_not_raise(self):
        watchdog = ProcessWatchdog()
        with patch("subprocess.run", side_effect=OSError("pgrep not found")):
            watchdog.check_duplicate_monitor_processes()  # should not raise
