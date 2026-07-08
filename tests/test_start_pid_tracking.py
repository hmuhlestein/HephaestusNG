"""Tests for _start_backend/_start_monitor's PID-tracking safety.

Regression: save_pid was called immediately after subprocess.Popen(),
before the spawned process's own startup singleton guard (run_server.py's
_exit_if_port_in_use / run_monitor.py's _exit_if_already_running) had a
chance to fire and self-terminate if another instance already owned the
port/was already running. That unconditionally overwrote the PID file with
the about-to-die PID, orphaning the tracking of the REAL, still-alive
process. The watchdog's next _check_services cycle then saw the
(already-dead) tracked PID, concluded the service "died", and spawned
another attempt -- which hit the exact same guard and died the same way.
Observed live: a fresh duplicate backend process appearing every single
watchdog cycle (~30s), indefinitely, once the PID file got poisoned by one
bad spawn.
"""

from unittest.mock import MagicMock, patch

from src.cli.commands.start import _start_backend, _start_monitor


class TestStartBackendPidTracking:
    def test_saves_pid_when_process_survives(self):
        mock_proc = MagicMock(pid=12345)
        mock_proc.poll.return_value = None  # still running

        with patch("subprocess.Popen", return_value=mock_proc), patch(
            "src.cli.commands.start.save_pid"
        ) as mock_save, patch("src.cli.commands.start.time.sleep"), patch(
            "builtins.open"
        ):
            result = _start_backend("python3", 8300, False)

        assert result is True
        mock_save.assert_called_once_with("backend", 12345)

    def test_does_not_save_pid_when_process_exits_immediately(self):
        """The spawned process's own singleton guard made it self-terminate
        (e.g. another backend already owns the port) -- must not overwrite
        the tracked PID with this dead one."""
        mock_proc = MagicMock(pid=12345)
        mock_proc.poll.return_value = 1  # already exited, code 1
        mock_proc.returncode = 1

        with patch("subprocess.Popen", return_value=mock_proc), patch(
            "src.cli.commands.start.save_pid"
        ) as mock_save, patch("src.cli.commands.start.time.sleep"), patch(
            "builtins.open"
        ):
            result = _start_backend("python3", 8300, False)

        assert result is False
        mock_save.assert_not_called()


class TestStartMonitorPidTracking:
    def test_saves_pid_when_process_survives(self):
        mock_proc = MagicMock(pid=54321)
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc), patch(
            "src.cli.commands.start.save_pid"
        ) as mock_save, patch("src.cli.commands.start.time.sleep"), patch(
            "builtins.open"
        ):
            result = _start_monitor("python3")

        assert result is True
        mock_save.assert_called_once_with("monitor", 54321)

    def test_does_not_save_pid_when_process_exits_immediately(self):
        mock_proc = MagicMock(pid=54321)
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1

        with patch("subprocess.Popen", return_value=mock_proc), patch(
            "src.cli.commands.start.save_pid"
        ) as mock_save, patch("src.cli.commands.start.time.sleep"), patch(
            "builtins.open"
        ):
            result = _start_monitor("python3")

        assert result is False
        mock_save.assert_not_called()
