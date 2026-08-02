"""Safe port-kill helper shared by stop, start, and watchdog."""

import subprocess
import signal
import os
import logging

logger = logging.getLogger(__name__)


def get_port_listeners(port, allowed_comm_names):
    """Return PIDs listening on `port` whose command name matches
    `allowed_comm_names` (set of strings, e.g. {"python", "python3").

    Filters to LISTEN sockets only (-sTCP:LISTEN) and by command name
    so VS Code Remote SSH port-forwarding proxies (also LISTEN sockets,
    also `node`) are never killed.
    """
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return []  # no lsof

    raw_pids = [p for p in result.stdout.strip().split("\n") if p.strip()]
    if not raw_pids:
        return []

    # Query command names for all PIDs in one call.
    try:
        ps = subprocess.run(
            ["ps", "-o", "pid=,comm=", "-p", ",".join(raw_pids)],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return []  # no ps

    kept = []
    for line in ps.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        pid_str, comm = parts[0].strip(), parts[1].strip()
        # comm is truncated (15 chars on Linux); use startswith so
        # "python3.12" still matches "python".
        if any(comm.startswith(name) for name in allowed_comm_names):
            try:
                kept.append(int(pid_str))
            except ValueError:
                pass
    return kept


def kill_port_listeners(port, allowed_comm_names):
    """Kill all LISTENers on `port` whose command name is in
    `allowed_comm_names`. Returns list of killed PIDs."""
    pids = get_port_listeners(port, allowed_comm_names)
    if not pids:
        return []

    killed = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except OSError:
            pass
    if killed:
        import time
        time.sleep(1)  # wait for port to free
    return killed