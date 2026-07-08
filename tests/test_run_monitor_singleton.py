"""Tests for run_monitor.py's startup singleton guard.

Regression: the watchdog's periodic duplicate-process cleanup (see
test_process_watchdog.py) closes a duplicate monitor within ~30s of it
appearing, but during that window two live monitors independently track
their own in-memory stuck-agent state and can send competing tmux recovery
keystrokes to the same agent, or run duplicate Guardian LLM analysis. This
checks at process startup, before run_monitor.py does anything else,
closing the window to effectively zero instead of relying solely on the
watchdog noticing after the fact.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run_monitor  # noqa: E402


class TestExitIfAlreadyRunning:
    def test_no_other_process_does_not_exit(self):
        with patch("run_monitor.subprocess.run") as mock_run, patch(
            "run_monitor.os.getpid", return_value=111
        ):
            mock_run.return_value = MagicMock(stdout="111\n")
            run_monitor._exit_if_already_running()  # should not raise/exit

    def test_other_process_running_exits(self):
        with patch("run_monitor.subprocess.run") as mock_run, patch(
            "run_monitor.os.getpid", return_value=111
        ):
            mock_run.return_value = MagicMock(stdout="111\n222\n")
            with pytest.raises(SystemExit) as exc_info:
                run_monitor._exit_if_already_running()
            assert exc_info.value.code == 1

    def test_pgrep_failure_fails_open(self):
        """If pgrep itself can't run, don't block startup over it."""
        with patch(
            "run_monitor.subprocess.run", side_effect=OSError("pgrep not found")
        ):
            run_monitor._exit_if_already_running()  # should not raise/exit

    def test_empty_pgrep_output_does_not_exit(self):
        with patch("run_monitor.subprocess.run") as mock_run, patch(
            "run_monitor.os.getpid", return_value=111
        ):
            mock_run.return_value = MagicMock(stdout="")
            run_monitor._exit_if_already_running()  # should not raise/exit
