"""heph stop — Stop Hephaestus services."""

import os
import signal
import time

from src.cli.utils import output, read_pid, remove_pid, is_process_running, PID_DIR


def register(subparsers):
    p = subparsers.add_parser("stop", help="Stop all Hephaestus services")
    p.add_argument("--force", action="store_true", help="Force kill (SIGKILL)")
    p.set_defaults(func=run)


def run(args):
    stopped = {}

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
