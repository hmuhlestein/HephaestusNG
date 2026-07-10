"""heph — Hephaestus CLI entry point.

Usage:
    heph <command> [subcommand] [options]

Commands:
    status      System health and status
    start       Start services (backend, monitor, frontend, qdrant)
    stop        Stop services
    restart     Restart services
    init        Initialize database and vector store
    workflow    Workflow management (list, launch, status, stop)
    agent       Agent management (list, logs, terminate)
    task        Task management (list, create, inspect)
    autopilot   Autopilot pipeline (start, stop, status, queue)
    project     Project management (list, create, activate, current, delete)
    memory      Knowledge base (search, save)
    exec        Execute commands and interact with services (run, ping, tool, endpoints, raw)
    config      Show and edit configuration
"""

import argparse
import logging
import sys
from pathlib import Path

from src.core.constants import HEPHAESTUS_LOGS_DIR

from src.cli.commands import (
    agent,
    autopilot,
    config,
    exec_cmd,
    init,
    memory,
    project,
    restart,
    start,
    status,
    stop,
    task,
    workflow,
)

BANNER = r"""
  _   _ _____ ____  _   _ ______   __
 | | | |  ___|  _ \| | | |  _ \ \ / /
 | |_| | |__ | |_) | |_| | |_\ V /
 |  _  |  __||  __/|  _  |  _| > <
 | | | | |___| |   | | | | |  / . \
 \_| |_/\____/\_|   \_| |_|_/_/ \_\

 Multi-Agent Workflow Engine
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heph",
        description="Hephaestus — Multi-Agent Workflow Engine CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run 'heph <command> --help' for details on a specific command.",
    )
    parser.add_argument("-v", "--version", action="version", version="heph 0.1.0")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Backend host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=8300, help="Backend port (default: 8300)"
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # --- Core lifecycle ---
    status.register(sub)
    start.register(sub)
    stop.register(sub)
    restart.register(sub)
    init.register(sub)

    # --- Resource management ---
    workflow.register(sub)
    agent.register(sub)
    task.register(sub)

    # --- Autopilot ---
    autopilot.register(sub)

    # --- Project ---
    project.register(sub)

    # --- Knowledge ---
    memory.register(sub)

    # --- Exec ---
    exec_cmd.register(sub)

    # --- Config ---
    config.register(sub)

    return parser


def _log_command(command: str, argv: list) -> None:
    """Record a manually-run heph command to cli.log.

    Lets a later investigation (e.g. "why did the backend restart?") tell a
    manual `heph restart` apart from the watchdog's own restarts, which log
    to watchdog.log instead and only fire on an actually-dead PID.

    Uses its own logger/handler rather than logging_config.configure_logging
    -- that helper always adds a stdout StreamHandler, which would print a
    log line into every heph command's normal terminal output.
    """
    try:
        log_dir = Path(HEPHAESTUS_LOGS_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("heph.cli")
        if not logger.handlers:
            handler = logging.FileHandler(log_dir / "cli.log")
            handler.setFormatter(
                logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            )
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        logger.info(f"heph {command} - argv={argv}")
    except OSError:
        pass


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        print(BANNER)
        parser.print_help()
        return 0

    # Inject API base URL into args
    args.api_base = f"http://{args.host}:{args.port}"

    try:
        handler = args.func
    except AttributeError:
        parser.print_help()
        return 1

    _log_command(args.command, argv if argv is not None else sys.argv[1:])

    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as e:
        if args.json:
            import json

            print(json.dumps({"error": str(e)}))
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
