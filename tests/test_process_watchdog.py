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


def _lsof_then_ps(pids_stdout, comm="python"):
    """subprocess.run stand-in for get_port_listeners' two-step lookup.

    ports.py runs `lsof` to find LISTENers and then `ps -o pid=,comm=` to
    filter them by command name (so a VS Code Remote SSH `node` proxy on
    the same port is never killed). A single canned return value answers
    both calls with lsof-shaped output, and the `ps` reply then parses to
    zero "<pid> <comm>" pairs -- so every PID is filtered out and the
    function under test sees an empty list.
    """
    pids = [p for p in pids_stdout.strip().split("\n") if p.strip()]

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "ps":
            return MagicMock(stdout="".join(f"{p} {comm}\n" for p in pids))
        return MagicMock(stdout=pids_stdout)

    return fake_run


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
            mock_run.side_effect = _lsof_then_ps("111\n222\n")
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
            mock_run.side_effect = _lsof_then_ps("111\n222\n")
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
            mock_run.side_effect = _lsof_then_ps("111\n222\n333\n")
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


class TestCheckBackendHealth:
    """Regression: `heph status` reported the backend unreachable while its
    PID was still alive and a background pipeline thread was still actively
    running (py-spy dump confirmed it mid-stride) -- a hang, not a crash.
    The plain PID-liveness check in _check_services waits forever for a
    process that will never exit on its own; this is the health-check-based
    check that can actually detect and recover from that class of hang.
    """

    def test_no_pid_does_nothing(self):
        watchdog = ProcessWatchdog()
        with patch("src.cli.commands.start.read_pid", return_value=None), patch(
            "os.kill"
        ) as mock_kill:
            watchdog.check_backend_health(8300)
        mock_kill.assert_not_called()

    def test_dead_pid_does_nothing(self):
        """PID-death is _check_services' job, not this method's."""
        watchdog = ProcessWatchdog()
        with patch("src.cli.commands.start.read_pid", return_value=111), patch(
            "src.cli.commands.start.is_process_running", return_value=False
        ), patch("os.kill") as mock_kill:
            watchdog.check_backend_health(8300)
        mock_kill.assert_not_called()

    def test_healthy_response_resets_failure_count(self):
        watchdog = ProcessWatchdog()
        watchdog._backend_health_failures = 2
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"status": "healthy"}
        with patch("src.cli.commands.start.read_pid", return_value=111), patch(
            "src.cli.commands.start.is_process_running", return_value=True
        ), patch("httpx.get", return_value=mock_resp):
            watchdog.check_backend_health(8300)
        assert watchdog._backend_health_failures == 0

    def test_single_failure_does_not_restart(self):
        watchdog = ProcessWatchdog()
        with patch("src.cli.commands.start.read_pid", return_value=111), patch(
            "src.cli.commands.start.is_process_running", return_value=True
        ), patch("httpx.get", side_effect=TimeoutError("no response")), patch(
            "os.kill"
        ) as mock_kill:
            watchdog.check_backend_health(8300)
        assert watchdog._backend_health_failures == 1
        mock_kill.assert_not_called()

    def test_reaching_threshold_kills_and_restarts(self):
        watchdog = ProcessWatchdog(unresponsive_threshold=3)
        callback = MagicMock(return_value=True)
        watchdog.register_service("backend", callback)
        with patch("src.cli.commands.start.read_pid", return_value=111), patch(
            "src.cli.commands.start.is_process_running", return_value=True
        ), patch("httpx.get", side_effect=TimeoutError("no response")), patch(
            "os.kill"
        ) as mock_kill:
            watchdog.check_backend_health(8300)
            watchdog.check_backend_health(8300)
            watchdog.check_backend_health(8300)

        mock_kill.assert_called_once()
        assert mock_kill.call_args[0][0] == 111
        callback.assert_called_once()
        assert watchdog._backend_health_failures == 0

    def test_non_200_status_counts_as_unhealthy(self):
        watchdog = ProcessWatchdog()
        mock_resp = MagicMock(status_code=500)
        with patch("src.cli.commands.start.read_pid", return_value=111), patch(
            "src.cli.commands.start.is_process_running", return_value=True
        ), patch("httpx.get", return_value=mock_resp):
            watchdog.check_backend_health(8300)
        assert watchdog._backend_health_failures == 1


class TestBackendRestartGraceSeeding:
    """Regression: every ProcessWatchdog is a freshly constructed instance
    (run_watchdog.py is its own subprocess, spawned by `heph start`/`heph
    restart` AFTER the backend already exists), so _backend_last_restart
    defaulting to 0.0 made the post-restart grace period inert for the
    backend instance a fresh watchdog was actually handed -- it only ever
    applied to a restart the watchdog later triggered itself. In practice
    this meant every `heph restart` produced a watchdog that started
    counting /health failures against the brand-new, still-warming-up
    backend immediately, killing it ~90-135s in and repeating the cycle on
    every subsequent restart. initial_backend_start_time closes that gap by
    letting the caller (run_watchdog.py, via --backend-started-at) seed the
    real start time."""

    def test_recent_start_time_skips_health_check_during_grace_period(self):
        import time

        watchdog = ProcessWatchdog(initial_backend_start_time=time.time())
        with patch("src.cli.commands.start.read_pid", return_value=111), patch(
            "src.cli.commands.start.is_process_running", return_value=True
        ), patch("httpx.get", side_effect=TimeoutError("no response")) as mock_get:
            watchdog.check_backend_health(8300)
        mock_get.assert_not_called()
        assert watchdog._backend_health_failures == 0

    def test_old_start_time_does_not_skip_health_check(self):
        import time

        watchdog = ProcessWatchdog(initial_backend_start_time=time.time() - 300)
        with patch("src.cli.commands.start.read_pid", return_value=111), patch(
            "src.cli.commands.start.is_process_running", return_value=True
        ), patch("httpx.get", side_effect=TimeoutError("no response")) as mock_get:
            watchdog.check_backend_health(8300)
        mock_get.assert_called_once()
        assert watchdog._backend_health_failures == 1

    def test_default_construction_does_not_skip_health_check(self):
        """No timestamp given (e.g. backend was already running, so
        _start_watchdog passes none) -- must behave exactly like before
        this fix: no grace period, checks run immediately."""
        watchdog = ProcessWatchdog()
        with patch("src.cli.commands.start.read_pid", return_value=111), patch(
            "src.cli.commands.start.is_process_running", return_value=True
        ), patch("httpx.get", side_effect=TimeoutError("no response")) as mock_get:
            watchdog.check_backend_health(8300)
        mock_get.assert_called_once()
        assert watchdog._backend_health_failures == 1


class TestStartWatchdogPassesBackendStartTime:
    """_start_watchdog is the CLI-side half of the same fix -- it must
    actually forward the timestamp _start_backend recorded into the
    subprocess command line, or run_watchdog.py's --backend-started-at
    default of 0.0 makes the grace-period seed above a no-op in practice."""

    def test_includes_backend_started_at_when_given(self):
        from src.cli.commands.start import _start_watchdog

        with patch("src.cli.commands.start._find_python", return_value="python3"), patch(
            "subprocess.Popen"
        ) as mock_popen, patch("src.cli.commands.start.save_pid"):
            mock_popen.return_value = MagicMock(pid=222)
            _start_watchdog(8300, MagicMock(backend_only=False, no_monitor=False, reload=False), 1234.5)

        cmd = mock_popen.call_args[0][0]
        assert "--backend-started-at" in cmd
        assert cmd[cmd.index("--backend-started-at") + 1] == "1234.5"

    def test_omits_backend_started_at_when_none(self):
        """Backend was "already running" -- run() passes None, nothing to seed."""
        from src.cli.commands.start import _start_watchdog

        with patch("src.cli.commands.start._find_python", return_value="python3"), patch(
            "subprocess.Popen"
        ) as mock_popen, patch("src.cli.commands.start.save_pid"):
            mock_popen.return_value = MagicMock(pid=222)
            _start_watchdog(8300, MagicMock(backend_only=False, no_monitor=False, reload=False), None)

        cmd = mock_popen.call_args[0][0]
        assert "--backend-started-at" not in cmd
