"""heph stop — Stop Hephaestus services."""

import os
import signal
import subprocess
import time

from src.cli.utils import is_process_running, output, read_pid, remove_pid
from src.cli.utils.ports import get_port_listeners, kill_port_listeners


def register(subparsers):
    p = subparsers.add_parser("stop", help="Stop all Hephaestus services")
    p.add_argument("--force", action="store_true", help="Force kill (SIGKILL)")
    p.set_defaults(func=run)


def run(args):
    stopped = {}

    port = getattr(args, "port", None)
    if not port:
        try:
            from src.core.simple_config import get_config

            port = get_config().server.mcp_port
        except Exception:
            port = 8300

    # First, kill ALL python processes LISTENING on the backend port to
    # prevent stale processes. Uses get_port_listeners to filter by command
    # name so VS Code Remote SSH port-forwarding proxies (also LISTEN
    # sockets, also `node`) are never killed.
    #
    # Block until the processes themselves fully exit instead of a flat
    # sleep(1) or a port-LISTEN check -- a graceful ASGI shutdown unbinds the
    # listening socket quickly but can keep the process alive much longer
    # finishing in-flight background work (e.g. a spec-gate evaluation's
    # multi-minute pytest subprocess). A port-only check declares success
    # while the OLD process is still alive underneath, sharing the same
    # database as the freshly-started one -- observed live: a stale
    # pre-restart evaluation finished ~7 minutes late and silently clobbered
    # state a legitimately-running agent (started by the NEW process after
    # the restart) had just changed. Checking actual process liveness
    # (is_process_running), not just the port, closes that gap.
    pids = get_port_listeners(port, {"python", "uvicorn"})
    try:
        if pids:
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL if args.force else signal.SIGTERM)
                    stopped[f"port-{port}-pid-{pid}"] = "killed"
                except (OSError, ValueError):
                    pass

            for _ in range(10):
                time.sleep(0.5)
                if not any(is_process_running(pid) for pid in pids):
                    break
            else:
                # Didn't shut down gracefully within 5s -- force it so
                # `start` never mistakes it for still running.
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                time.sleep(1)
    except Exception:
        pass

    # Then kill by PID file. Watchdog goes first — it respawns monitor/backend
    # on a health-check timer, so killing it last lets it resurrect whatever
    # this loop just killed in the window before its own SIGTERM lands.
    for name in ("watchdog", "backend", "monitor", "frontend", "orchestrator"):
        pid = read_pid(name)
        if pid and is_process_running(pid):
            try:
                sig = signal.SIGKILL if args.force else signal.SIGTERM
                os.kill(pid, sig)
                # Wait briefly for graceful shutdown
                for _ in range(5):
                    time.sleep(0.5)
                    if not is_process_running(pid):
                        break
                else:
                    # Force kill if still running
                    if is_process_running(pid):
                        os.kill(pid, signal.SIGKILL)
                        time.sleep(0.5)
                stopped[name] = "stopped"
            except OSError as e:
                stopped[name] = f"error: {e}"
            finally:
                remove_pid(name)
        else:
            stopped[name] = "not_running"
            if pid:
                remove_pid(name)

    # Safety-net sweep: pidfiles only track the most recently started
    # instance of each service. A watchdog-respawned monitor/backend that
    # never got its pidfile entry synced (or a process started outside
    # `heph start` entirely) would otherwise survive this command.
    # Includes vite to catch orphaned dev-server processes from start_all.sh.
    for pattern in ("run_watchdog.py", "run_server.py", "run_monitor.py", "vite"):
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern], capture_output=True, text=True
            )
            for pid_str in result.stdout.strip().split("\n"):
                if not pid_str:
                    continue
                try:
                    pid = int(pid_str)
                    sig = signal.SIGKILL if args.force else signal.SIGTERM
                    os.kill(pid, sig)
                    stopped[f"orphan-{pattern}-{pid}"] = "killed"
                except (OSError, ValueError):
                    pass
        except Exception:
            pass

    output(args, stopped, lambda d: [print(f"  {k}: {v}") for k, v in d.items()])
    return 0
