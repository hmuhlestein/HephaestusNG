"""heph restart — Restart services."""

import time
from src.cli.commands.stop import run as stop_run
from src.cli.commands.start import run as start_run


def register(subparsers):
    p = subparsers.add_parser("restart", help="Restart all services")
    p.add_argument("--no-frontend", action="store_true", help="Skip frontend")
    p.add_argument("--reload", action="store_true", help="Enable auto-reload")
    p.set_defaults(func=run)


def run(args):
    if not hasattr(args, "force"):
        args.force = False
    if not hasattr(args, "backend_only"):
        args.backend_only = False
    if not hasattr(args, "no_monitor"):
        args.no_monitor = False
    print("Stopping...")
    stop_run(args)
    print("Starting...")
    return start_run(args)
