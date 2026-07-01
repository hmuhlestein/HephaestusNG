#!/usr/bin/env python3
"""
Hephaestus Process Watchdog

Runs as its own detached, long-lived process (like run_server.py/run_monitor.py)
and periodically checks whether the backend/monitor processes are still alive,
restarting them if they died unexpectedly.

H-4 fix: the watchdog previously ran as a daemon thread inside `heph start`'s
own short-lived CLI process — it died within moments of that command
returning and never actually supervised anything. Running it as its own
subprocess (spawned the same way backend/monitor/frontend already are)
makes it actually persist.
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).parent / "src"))

from src.cli.commands.start import (
    HEPHAESTUS_DIR,
    ProcessWatchdog,
    _find_python,
    _start_backend,
    _start_monitor,
)
from src.cli.utils import save_pid
from src.core.logging_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Hephaestus process watchdog")
    parser.add_argument("--port", type=int, default=8300)
    parser.add_argument("--backend-only", action="store_true")
    parser.add_argument("--no-monitor", action="store_true")
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--check-interval", type=int, default=30)
    args = parser.parse_args()

    python = _find_python(HEPHAESTUS_DIR)

    watchdog = ProcessWatchdog(check_interval=args.check_interval)
    watchdog.register_service(
        "backend", lambda: _start_backend(python, args.port, args.reload)
    )
    if not args.backend_only and not args.no_monitor:
        watchdog.register_service("monitor", lambda: _start_monitor(python))

    logger.info(
        f"Watchdog running (PID {os.getpid()}), checking every {args.check_interval}s"
    )
    watchdog.running = True
    try:
        while watchdog.running:
            time.sleep(watchdog.check_interval)
            watchdog._check_services()
    except KeyboardInterrupt:
        logger.info("Watchdog interrupted, shutting down")


if __name__ == "__main__":
    save_pid("watchdog", os.getpid())
    main()
