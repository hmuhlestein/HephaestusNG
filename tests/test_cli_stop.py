"""Tests for `heph stop`'s port-cleanup step -- specifically that it waits
for the killed processes to actually exit before returning, instead of a
flat sleep(1) or a port-LISTEN check that can both race a backend whose
graceful shutdown keeps running in-flight background work after unbinding
its socket.

Regression coverage for two live incidents:
1. `heph restart` reported success, but the old backend process was still
   listening on the port when `start`'s own health check ran a moment
   later -- `start` concluded "already running" and never spawned a fresh
   process, so the backend (and the autopilot pipeline running inside it)
   silently never actually restarted.
2. After fixing (1) with a port-LISTEN poll, a *different* old process
   survived even longer: its socket unbound quickly (satisfying the
   port-LISTEN check) but it kept running a multi-minute pytest subprocess
   in the background, finishing ~7 minutes after the restart and
   clobbering state a freshly-restarted, unrelated agent had legitimately
   just changed. Checking actual process liveness (not just the port)
   closes that gap.
"""

import signal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.cli.commands import stop


def _args(force=False):
    return SimpleNamespace(force=force, json=False)


def _quiet_service_loop():
    """Patches for the per-service kill loop and orphan sweep so the test
    only has to reason about the port-cleanup step at the top of run().
    read_pid=None makes the per-service loop's `if pid and
    is_process_running(pid):` short-circuit without calling
    is_process_running, so it doesn't interfere with the port-cleanup
    liveness fake below."""
    return (
        patch("src.cli.commands.stop.read_pid", return_value=None),
        patch("src.cli.commands.stop.remove_pid"),
    )


def _fake_lsof_run(pid_str="12345"):
    """subprocess.run stand-in for BOTH modules that shell out here.

    get_port_listeners (src/cli/utils/ports.py) runs `lsof` and then `ps`
    to filter listeners by command name, so the `ps` reply must be
    "<pid> <comm>" pairs or nothing survives the filter and the whole
    port-cleanup block is skipped. `pgrep` (stop.py's orphan sweep)
    reports nothing.
    """

    def fake_run(cmd, **kwargs):
        if cmd[0] == "pgrep":
            return MagicMock(stdout="")
        if cmd[0] == "ps":
            return MagicMock(stdout=f"{pid_str} python\n")
        return MagicMock(stdout=f"{pid_str}\n")

    return fake_run


def _liveness_fake(dies_after: int):
    """is_process_running stand-in: reports the pid alive for the first
    `dies_after` calls, then dead."""
    calls = {"n": 0}

    def fake(pid):
        calls["n"] += 1
        return calls["n"] <= dies_after

    return fake, calls


class TestPortCleanupWaitsForProcessExit:
    def test_stops_polling_once_the_process_actually_exits(self):
        liveness, calls = _liveness_fake(dies_after=1)

        p1, p2 = _quiet_service_loop()
        with patch(
            "src.cli.commands.stop.subprocess.run", side_effect=_fake_lsof_run()
        ), patch(
            "src.cli.utils.ports.subprocess.run", side_effect=_fake_lsof_run()
        ), patch("src.cli.commands.stop.os.kill") as mock_kill, patch(
            "src.cli.commands.stop.time.sleep"
        ), patch(
            "src.cli.commands.stop.is_process_running", side_effect=liveness
        ), p1, p2:
            stop.run(_args())

        assert calls["n"] == 2, "should stop polling once the process exits"
        # Graceful SIGTERM only -- no escalation needed since it exited.
        assert all(
            call.args[1] == signal.SIGTERM for call in mock_kill.call_args_list
        )

    def test_escalates_to_sigkill_if_still_alive_after_poll_window(self):
        # Never exits within the poll window (10 checks).
        liveness, calls = _liveness_fake(dies_after=999)

        p1, p2 = _quiet_service_loop()
        with patch(
            "src.cli.commands.stop.subprocess.run", side_effect=_fake_lsof_run()
        ), patch(
            "src.cli.utils.ports.subprocess.run", side_effect=_fake_lsof_run()
        ), patch("src.cli.commands.stop.os.kill") as mock_kill, patch(
            "src.cli.commands.stop.time.sleep"
        ), patch(
            "src.cli.commands.stop.is_process_running", side_effect=liveness
        ), p1, p2:
            stop.run(_args())

        signals_sent = [call.args[1] for call in mock_kill.call_args_list]
        assert signal.SIGKILL in signals_sent, (
            "must force-kill after graceful shutdown times out, so a "
            "subsequent `start` never mistakes the old process for still "
            "alive"
        )
