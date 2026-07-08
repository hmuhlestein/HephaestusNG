#!/usr/bin/env python3
"""Main entry point for running the Hephaestus MCP server."""

import logging
import os
import subprocess
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

import uvicorn

from src.core.logging_config import configure_logging
from src.core.simple_config import get_config

# Configure logging using shared helper (L-2 fix)
# Only log to stdout — the process is launched with stdout
# redirected to ~/.hephaestus/logs/backend.log (see src/cli/commands/start.py),
# so a second FileHandler here would just duplicate every line into a stray
# hephaestus_server.log at the repo root.
configure_logging()

logger = logging.getLogger(__name__)


def _exit_if_port_in_use(port: int) -> None:
    """Refuse to start if another process is already bound to `port`.

    Two backend processes can coexist on the "same" port without an
    "address already in use" error if they end up bound to different
    addresses (observed live: one on `*:8300`, another on `localhost:8300`)
    -- the OS doesn't reject the second bind in that case. Each would then
    drive its own independent AutopilotService singleton against the same
    DB, racing each other (one can pause a workflow the other just
    started). The watchdog's periodic duplicate-port check (see
    src/cli/commands/start.py's check_duplicate_port_listeners) cleans up
    an extra instance after the fact, but that leaves a window where both
    are live. Checking here, before uvicorn ever binds, closes it to
    effectively zero -- mirrors run_monitor.py's _exit_if_already_running.
    """
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
    except Exception:
        return  # can't check (no lsof, etc.) -- fail open rather than block startup

    other_pids = [p for p in pids if p != os.getpid()]
    if other_pids:
        logger.error(
            f"Another process is already bound to port {port} "
            f"(PID(s) {other_pids}) -- refusing to start a second backend. Exiting."
        )
        sys.exit(1)


def main():
    """Run the MCP server."""
    config = get_config()
    _exit_if_port_in_use(config.mcp_port)

    logger.info("Starting Hephaestus MCP Server")
    logger.info(f"Server will run on {config.mcp_host}:{config.mcp_port}")
    logger.info(f"Using LLM provider: {config.llm_provider}")
    logger.info(f"Using model: {config.llm_model}")

    # Validate configuration
    try:
        config.validate()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)

    # Run the server
    try:
        # Log to file so we can trace API events (tmux scrollback is limited)
        log_dir = Path(__file__).parent / "logs"
        log_dir.mkdir(exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "server.log")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
        )
        logging.getLogger().addHandler(file_handler)

        uvicorn.run(
            "src.mcp.server:app",
            host=config.mcp_host,
            port=config.mcp_port,
            reload=False,
            workers=1,
            log_level="info" if not config.debug else "debug",
        )
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
