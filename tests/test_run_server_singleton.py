"""Tests for run_server.py's startup singleton guard.

Regression: two backend processes can end up bound to the "same" port
without an OS-level "address already in use" error if they bind different
addresses (observed live: one on `*:8300`, another on `localhost:8300`).
Each then drives its own independent AutopilotService singleton against the
same DB. The watchdog's periodic duplicate-port cleanup (see
test_process_watchdog.py) closes this within ~30s, but this checks at
process startup -- before uvicorn ever binds -- closing the window to
effectively zero. Mirrors run_monitor.py's _exit_if_already_running.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run_server  # noqa: E402


class TestExitIfPortInUse:
    def test_port_free_does_not_exit(self):
        with patch("run_server.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            run_server._exit_if_port_in_use(8300)  # should not raise/exit

    def test_only_self_bound_does_not_exit(self):
        with patch("run_server.subprocess.run") as mock_run, patch(
            "run_server.os.getpid", return_value=111
        ):
            mock_run.return_value = MagicMock(stdout="111\n")
            run_server._exit_if_port_in_use(8300)  # should not raise/exit

    def test_other_process_bound_exits(self):
        with patch("run_server.subprocess.run") as mock_run, patch(
            "run_server.os.getpid", return_value=111
        ):
            mock_run.return_value = MagicMock(stdout="111\n222\n")
            with pytest.raises(SystemExit) as exc_info:
                run_server._exit_if_port_in_use(8300)
            assert exc_info.value.code == 1

    def test_lsof_failure_fails_open(self):
        with patch("run_server.subprocess.run", side_effect=OSError("lsof not found")):
            run_server._exit_if_port_in_use(8300)  # should not raise/exit

    def test_lsof_call_filters_to_listen_sockets_only(self):
        """Regression: without -sTCP:LISTEN, this check could see a
        legitimate in-flight client request (curl, Vite's API proxy, the
        monitor's health polling) and conclude "another backend already
        owns this port" when no server is running there at all -- refusing
        to start a legitimate restart over nothing."""
        with patch("run_server.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            run_server._exit_if_port_in_use(8300)

        args = mock_run.call_args[0][0]
        assert "-sTCP:LISTEN" in args
