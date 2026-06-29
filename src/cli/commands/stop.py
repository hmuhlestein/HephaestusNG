"""heph stop — Stop Hephaestus services."""

import os
import signal
import subprocess
import time

from src.cli.utils import is_process_running, output, read_pid, remove_pid


def register(subparsers):
    p = subparsers.add_parser("stop", help="Stop all Hephaestus services")
    p.add_argument("--force", action="store_true", help="Force kill (SIGKILL)")
    p.set_defaults(func=run)


def run(args):
    stopped = {}

    # Read port from config
    try:
        from src.core.simple_config import Config

        config = Config()
        port = config.mcp_port or 8300
    except Exception:
        port = 8300

    # First, kill ALL processes on the backend port to prevent stale processes
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"], capture_output=True, text=True
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            for pid_str in pids:
                try:
                    pid = int(pid_str)
                    sig = signal.SIGKILL if args.force else signal.SIGTERM
                    os.kill(pid, sig)
                    stopped[f"port-{port}-pid-{pid}"] = "killed"
                except (OSError, ValueError):
                    pass
            time.sleep(1)
    except Exception:
        pass

    # Then kill by PID file
    for name in ("backend", "monitor", "frontend", "orchestrator"):
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

    output(args, stopped, lambda d: [print(f"  {k}: {v}") for k, v in d.items()])
    return 0
