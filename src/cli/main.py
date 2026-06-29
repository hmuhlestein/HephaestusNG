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
import sys

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
